"""#131: `/chat` 쿼터 게이트 + `GET /quota` 스모크 테스트 (TestClient, LLM/서브프로세스 없음).

- QUOTA_ENABLED=false(기본)일 때 판정·기록이 완전 no-op — 기존 /chat 동작 불변.
- 한도 소진 subject 는 quota_exceeded 로 거절되고 done 을 받는다.
- 쿼터 스토어가 예외를 던져도 /chat 은 정상 진행한다(fail-open).
- 차단 목록(QUOTA_DENY_SUBJECTS) 히트 시 blocked 로 거절된다.
- GET /quota 가 used/remaining/resets_at 을 반환한다.
"""
import pytest

try:
    from fastapi.testclient import TestClient
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)

from agent.quota import InMemoryQuotaStore, TokenUsage


class _FakeOrch:
    """토큰 2개 내고 끝나는 가짜 오케스트레이터(LLM 호출 없음). pop_turn_usage 로 턴 사용량 회수."""
    _user_id = ""

    def __init__(self, usage=None):
        self._usage = usage if usage is not None else TokenUsage(input=10, output=10)

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}

    def pop_turn_usage(self):
        u, self._usage = self._usage, TokenUsage()
        return u


class _BoomOrch(_FakeOrch):
    def pop_turn_usage(self):
        raise RuntimeError("boom")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    # 매 테스트가 독립된 카운터를 쓰도록 리셋(모듈 전역 quota_store 공유 방지).
    monkeypatch.setattr(server, "quota_store", InMemoryQuotaStore())
    return TestClient(server.app)


def _enable_quota(monkeypatch, **overrides):
    monkeypatch.setattr(server, "QUOTA_ENABLED", True)
    monkeypatch.setattr(server, "QUOTA_SCOPE", overrides.get("scope", "user"))
    monkeypatch.setattr(server, "QUOTA_DAILY_WEIGHTED_TOKENS", overrides.get("limit", 2_000_000))
    monkeypatch.setattr(server, "QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP", overrides.get("ip_limit", 0))
    monkeypatch.setattr(server, "QUOTA_DENY_SUBJECTS", overrides.get("deny", set()))


# ── QUOTA_ENABLED=false(기본) — 완전 no-op ─────────────────────────
def test_quota_disabled_by_default():
    assert server.QUOTA_ENABLED is False


def test_chat_unaffected_when_quota_disabled(client):
    """킬스위치 꺼짐 — 기존 /chat 스모크(토큰 수신)가 그대로 통과해야 한다."""
    with client.stream("POST", "/chat", json={"session_id": "s-off", "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text
    assert "quota_exceeded" not in text


def test_quota_store_untouched_when_disabled(client):
    """비활성 상태에서는 quota_store.add 가 아예 호출되지 않는다(진짜 no-op)."""
    with client.stream("POST", "/chat", json={"session_id": "s-off2", "message": "x", }) as resp:
        "".join(resp.iter_lines())
    used = server.quota_store.used(server.quota_subject("", "s-off2"))
    assert used == TokenUsage()


# ── 한도 소진 → quota_exceeded 거절 ────────────────────────────────
def test_chat_rejected_when_quota_exceeded(client, monkeypatch):
    _enable_quota(monkeypatch, limit=1)  # 사실상 0에 가까운 한도 — 첫 턴 후 바로 소진
    sid = "quota-exceeded-sess"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(input=1_000_000))  # 이미 한도 초과 상태로 세팅

    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())

    assert "quota_exceeded" in text
    assert '"done"' in text
    assert "hi" not in text  # 락 진입 전 거절이라 실제 챗 스트림은 돌지 않았다


def test_quota_exceeded_event_has_retry_after_and_retryable_false(client, monkeypatch):
    _enable_quota(monkeypatch, limit=1)
    sid = "quota-fields-sess"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(input=1_000_000))

    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
    import json
    events = [json.loads(ln[len("data: "):]) for ln in lines]
    err = next(e for e in events if e.get("type") == "error")
    assert err["code"] == "quota_exceeded"
    assert err["retryable"] is False
    assert isinstance(err["retry_after"], int) and err["retry_after"] >= 0


# ── 쿼터 판정/기록 예외 → fail-open ─────────────────────────────────
def test_chat_proceeds_when_quota_store_used_raises(client, monkeypatch):
    _enable_quota(monkeypatch, limit=1000)

    class _BoomStore(InMemoryQuotaStore):
        def used(self, subject):
            raise RuntimeError("store down")

    monkeypatch.setattr(server, "quota_store", _BoomStore())
    captured = []
    monkeypatch.setattr(
        server, "capture_chat_exception",
        lambda exc, **tags: captured.append((exc, tags)))

    with client.stream("POST", "/chat", json={"session_id": "boom-sess", "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text  # 판정이 터져도 정상 진행(fail-open)
    assert any(tags.get("stage") == "quota" for _, tags in captured)  # fail-open 시 관측 호출


def test_chat_proceeds_when_pop_turn_usage_raises(client, monkeypatch):
    _enable_quota(monkeypatch, limit=1000)
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _BoomOrch())
    captured = []
    monkeypatch.setattr(
        server, "capture_chat_exception",
        lambda exc, **tags: captured.append((exc, tags)))

    with client.stream("POST", "/chat", json={"session_id": "boom-record-sess", "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text  # 기록이 터져도 스트림은 정상 완료되고 락도 해제된다
    assert server.session_locks.is_busy("boom-record-sess") is False
    assert any(tags.get("stage") == "quota" for _, tags in captured)


# ── IP subject 정규화 ────────────────────────────────────────────────
def test_quota_ip_subject_preserves_dots_no_collision():
    """IPv4 의 '.'을 지우면 서로 다른 IP가 같은 subject로 뭉개진다 — 회귀 방지."""
    assert server._quota_ip_subject("1.2.3.4") == "ip:1.2.3.4"
    assert server._quota_ip_subject("12.3.4") == "ip:12.3.4"
    assert server._quota_ip_subject("1.2.3.4") != server._quota_ip_subject("12.3.4")


def test_quota_ip_subject_supports_ipv6():
    assert server._quota_ip_subject("2001:db8::1") == "ip:2001:db8::1"


# ── 차단 목록 ───────────────────────────────────────────────────────
def test_chat_rejected_when_subject_blocked(client, monkeypatch):
    sid = "blocked-sess"
    subject = server.quota_subject("", sid, scope="user")
    _enable_quota(monkeypatch, deny={subject})
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "blocked" in text
    assert "hi" not in text


# ── 정상 턴 후 기록 ─────────────────────────────────────────────────
def test_quota_recorded_after_successful_turn(client, monkeypatch):
    _enable_quota(monkeypatch, limit=2_000_000)
    sid = "record-sess"
    subject = server.quota_subject("", sid, scope="user")
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        "".join(resp.iter_lines())
    used = server.quota_store.used(subject)
    assert used.input == 10 and used.output == 10  # _FakeOrch 기본 usage


# ── GET /quota ──────────────────────────────────────────────────────
def test_get_quota_returns_used_remaining_resets_at(client, monkeypatch):
    _enable_quota(monkeypatch, limit=1000)
    subject = server.quota_subject("u1", "s1", scope="user")
    server.quota_store.add(subject, TokenUsage(input=100))

    r = client.get("/quota", params={"session_id": "s1", "user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["scope"] == "user"
    assert body["limit"] == 1000
    assert body["used"] == 100
    assert body["remaining"] == 900
    assert "resets_at" in body and body["resets_at"]


def test_get_quota_works_even_when_quota_disabled(client):
    """GET /quota 는 QUOTA_ENABLED=false 여도 현재 누적을 그대로 보여준다(enabled 플래그만 false)."""
    r = client.get("/quota", params={"session_id": "s-any"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["used"] == 0
