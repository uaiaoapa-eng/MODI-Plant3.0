"""턴 상한(QUOTA_DAILY_MAX_TURNS) — "한 사용자당 하루 N번" 테스트.

토큰 한도와 병행하는 별도 차원. TokenUsage.turns 누적(인메모리/Redis)과
/chat 게이트 거절·GET /quota 노출을 검증한다. LLM/서브프로세스 없음.
"""
import json

import pytest

try:
    from fastapi.testclient import TestClient
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)

from agent.errors import CATALOG, ErrorCode
from agent.quota import InMemoryQuotaStore, RedisQuotaStore, TokenUsage


# ── TokenUsage.turns 단위 ────────────────────────────────────────────
def test_tokenusage_turns_accumulate_on_add():
    s = TokenUsage(input=5, turns=1) + TokenUsage(output=3, turns=1)
    assert s.turns == 2
    assert s.input == 5 and s.output == 3


def test_tokenusage_weighted_ignores_turns():
    """turns 는 비용 지표가 아니므로 가중 토큰 계산에 들어가면 안 된다."""
    assert TokenUsage(input=10, turns=100).weighted() == 10


# ── InMemory / Redis turns 누적 ──────────────────────────────────────
def test_inmemory_accumulates_turns():
    store = InMemoryQuotaStore()
    store.add("u:x", TokenUsage(input=1, turns=1))
    store.add("u:x", TokenUsage(input=1, turns=1))
    assert store.used("u:x").turns == 2


def test_redis_accumulates_turns():
    fakeredis = pytest.importorskip("fakeredis")
    store = RedisQuotaStore(fakeredis.FakeStrictRedis(decode_responses=True))
    store.add("u:y", TokenUsage(output=2, turns=1))
    store.add("u:y", TokenUsage(output=2, turns=1))
    used = store.used("u:y")
    assert used.turns == 2 and used.output == 4


def test_redis_records_zero_token_turn():
    """토큰 0인 턴(재사용 직접서브)도 turns=1 이면 기록돼 턴 카운트에 잡힌다."""
    fakeredis = pytest.importorskip("fakeredis")
    store = RedisQuotaStore(fakeredis.FakeStrictRedis(decode_responses=True))
    store.add("u:z", TokenUsage(turns=1))  # 토큰 전부 0, 턴만 1
    assert store.used("u:z").turns == 1


# ── /chat 게이트 (TestClient) ────────────────────────────────────────
class _FakeOrch:
    """토큰 2개 내고 끝나는 가짜 오케스트레이터(LLM 호출 없음)."""
    _user_id = ""

    def __init__(self, usage=None):
        self._usage = usage if usage is not None else TokenUsage(input=10, output=10)

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}

    def pop_turn_usage(self):
        u, self._usage = self._usage, TokenUsage()
        return u


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    monkeypatch.setattr(server, "quota_store", InMemoryQuotaStore())
    return TestClient(server.app)


def _enable(monkeypatch, *, max_turns=0, limit=2_000_000):
    monkeypatch.setattr(server, "QUOTA_ENABLED", True)
    monkeypatch.setattr(server, "QUOTA_SCOPE", "user")
    monkeypatch.setattr(server, "QUOTA_DAILY_WEIGHTED_TOKENS", limit)
    monkeypatch.setattr(server, "QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP", 0)
    monkeypatch.setattr(server, "QUOTA_DENY_SUBJECTS", set())
    monkeypatch.setattr(server, "QUOTA_DAILY_MAX_TURNS", max_turns)


def test_turn_incremented_after_successful_turn(client, monkeypatch):
    """정상 턴 1회 후 사용자 subject 의 turns 가 1 올라간다."""
    _enable(monkeypatch, max_turns=30)
    sid = "turn-inc"
    subject = server.quota_subject("", sid, scope="user")
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        "".join(resp.iter_lines())
    assert server.quota_store.used(subject).turns == 1


def test_chat_rejected_when_turns_exhausted(client, monkeypatch):
    """턴 소진 subject 는 quota_exceeded 로 거절되고 챗 스트림은 안 돈다."""
    _enable(monkeypatch, max_turns=5)
    sid = "turn-exhausted"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(turns=5))  # 이미 5턴 소진
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "quota_exceeded" in text
    assert "hi" not in text  # 락 진입 전 거절


def test_turn_cap_independent_of_token_cap(client, monkeypatch):
    """토큰은 넉넉해도 턴이 소진되면 차단된다(병행 — 둘 중 하나라도 걸리면 차단)."""
    _enable(monkeypatch, max_turns=3, limit=10_000_000)
    sid = "turn-only"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(input=1, turns=3))  # 토큰 미미, 턴 소진
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "quota_exceeded" in text


def test_turn_cap_off_when_zero(client, monkeypatch):
    """QUOTA_DAILY_MAX_TURNS=0 이면 턴이 아무리 쌓여도 턴 차원은 미적용."""
    _enable(monkeypatch, max_turns=0, limit=10_000_000)
    sid = "turn-off"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(turns=999))
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text and "quota_exceeded" not in text


def test_turn_exhausted_event_fields(client, monkeypatch):
    _enable(monkeypatch, max_turns=1)
    sid = "turn-fields"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(turns=1))
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
    events = [json.loads(ln[len("data: "):]) for ln in lines]
    err = next(e for e in events if e.get("type") == "error")
    assert err["code"] == "quota_exceeded"
    assert err["retryable"] is False
    assert isinstance(err["retry_after"], int)


# ── #147: 차단 스트림의 렌더 가능한 token 병행 전송 (SSE_ERROR_AS_TOKEN) ─────
def _events(resp):
    """SSE data 라인들을 파싱해 이벤트 dict 리스트로."""
    lines = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
    return [json.loads(ln[len("data: "):]) for ln in lines]


def _stream_events(client, sid):
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        return _events(resp)


def test_quota_stream_emits_token_before_error_when_on(client, monkeypatch):
    """SSE_ERROR_AS_TOKEN=true: quota 차단 시 카탈로그 user_message 를 담은
    type:"token" 이 type:"error" 보다 먼저 방출된다(순서 token→error→done)."""
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", True)
    _enable(monkeypatch, max_turns=1)
    sid = "sse-token-quota"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(turns=1))
    events = _stream_events(client, sid)
    types = [e.get("type") for e in events]
    assert types == ["token", "error", "done"]
    assert events[0]["text"] == CATALOG[ErrorCode.QUOTA_EXCEEDED].user_message
    assert events[1]["code"] == "quota_exceeded"
    # retry_after 는 error 이벤트에만 실린다(token 텍스트엔 없음).
    assert "retry_after" in events[1] and "retry_after" not in events[0]


def test_quota_stream_no_token_when_off(client, monkeypatch):
    """SSE_ERROR_AS_TOKEN=false: token 없이 error→done 만(현행 회귀 없음)."""
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", False)
    _enable(monkeypatch, max_turns=1)
    sid = "sse-notoken-quota"
    subject = server.quota_subject("", sid, scope="user")
    server.quota_store.add(subject, TokenUsage(turns=1))
    events = _stream_events(client, sid)
    assert [e.get("type") for e in events] == ["error", "done"]


def test_blocked_stream_emits_token_before_error_when_on(client, monkeypatch):
    """SSE_ERROR_AS_TOKEN=true: 운영자 차단(BLOCKED)도 token 선행."""
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", True)
    _enable(monkeypatch, max_turns=0)
    sid = "sse-token-blocked"
    subject = server.quota_subject("", sid, scope="user")
    monkeypatch.setattr(server, "QUOTA_DENY_SUBJECTS", {subject})
    events = _stream_events(client, sid)
    assert [e.get("type") for e in events] == ["token", "error", "done"]
    assert events[0]["text"] == CATALOG[ErrorCode.BLOCKED].user_message
    assert events[1]["code"] == "blocked"


def test_blocked_stream_no_token_when_off(client, monkeypatch):
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", False)
    _enable(monkeypatch, max_turns=0)
    sid = "sse-notoken-blocked"
    subject = server.quota_subject("", sid, scope="user")
    monkeypatch.setattr(server, "QUOTA_DENY_SUBJECTS", {subject})
    events = _stream_events(client, sid)
    assert [e.get("type") for e in events] == ["error", "done"]


def test_busy_stream_emits_token_before_error_when_on(client, monkeypatch):
    """SSE_ERROR_AS_TOKEN=true: 세션 점유 중(SESSION_BUSY)도 token 선행."""
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", True)
    monkeypatch.setattr(server.session_locks, "acquire", lambda sid: False)
    sid = "sse-token-busy"
    events = _stream_events(client, sid)
    assert [e.get("type") for e in events] == ["token", "error", "done"]
    assert events[0]["text"] == CATALOG[ErrorCode.SESSION_BUSY].user_message
    assert events[1]["code"] == "session_busy"


def test_busy_stream_no_token_when_off(client, monkeypatch):
    monkeypatch.setattr(server, "SSE_ERROR_AS_TOKEN", False)
    monkeypatch.setattr(server.session_locks, "acquire", lambda sid: False)
    sid = "sse-notoken-busy"
    events = _stream_events(client, sid)
    assert [e.get("type") for e in events] == ["error", "done"]


# ── GET /quota 턴 노출 ───────────────────────────────────────────────
def test_get_quota_reports_turns(client, monkeypatch):
    _enable(monkeypatch, max_turns=30)
    subject = server.quota_subject("u1", "s1", scope="user")
    server.quota_store.add(subject, TokenUsage(turns=4))
    body = client.get("/quota", params={"session_id": "s1", "user_id": "u1"}).json()
    assert body["max_turns"] == 30
    assert body["turns_used"] == 4
    assert body["turns_remaining"] == 26


def test_get_quota_turns_remaining_null_when_off(client, monkeypatch):
    _enable(monkeypatch, max_turns=0)
    body = client.get("/quota", params={"session_id": "s2", "user_id": "u2"}).json()
    assert body["max_turns"] == 0
    assert body["turns_remaining"] is None
