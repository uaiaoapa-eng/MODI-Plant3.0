"""server.py 엔드포인트 스모크 테스트 — LLM/서브프로세스 없이 wiring 검증.

orch.chat_stream 을 가짜로 바꿔 토큰을 전혀 소모하지 않고:
  - /health, /health/llm(ping 없음) 동작
  - 같은 세션이 처리 중이면 #1 가드가 busy 로 거절
  - 정상 턴 후 락이 해제됨(acquire/release 가 핸들러↔스트림 스레드 간 짝이 맞음)
을 확인한다. (실제 LLM 부하/한계 측정은 scripts/load_test.py 가 담당.)
"""
import pytest

try:
    from fastapi.testclient import TestClient
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


class _FakeOrch:
    """토큰 2개 내고 끝나는 가짜 오케스트레이터(LLM 호출 없음)."""
    _user_id = ""

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    return TestClient(server.app)


def test_health_no_tokens(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] in ("cli", "api")


def test_health_llm_without_ping_spends_no_tokens(client):
    r = client.get("/health/llm", params={"session_id": "nope"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["session_alive"] is False  # 메모리에 없는 세션


def test_chat_rejected_when_session_busy(client):
    """다른 턴이 진행 중(락 점유)인 세션에 요청하면 busy 로 거절."""
    sid = "busy-sess"
    assert server.session_locks.acquire(sid) is True  # 진행 중 상황 모사
    try:
        with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
            text = "".join(resp.iter_lines())
        assert "처리 중" in text  # busy 거절 메시지
    finally:
        server.session_locks.release(sid)


def test_chat_proceeds_and_releases_lock_when_free(client):
    """비어 있는 세션은 정상 진행하고, 끝나면 락이 해제돼야 한다."""
    sid = "free-sess"
    assert server.session_locks.is_busy(sid) is False
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text  # 토큰 수신
    # 스트림이 끝나면 finally 에서 release → 다시 비어 있어야 함(핸들러↔스트림 스레드 간 해제 검증)
    assert server.session_locks.is_busy(sid) is False


def test_lock_released_even_when_autosave_raises(client, monkeypatch):
    """저장(auto_save)이 예외를 던져도 락은 반드시 해제돼야 한다.

    회귀 방지: 예전엔 finally 의 auto_save 가 OSError 등을 던지면 뒤의 release 가 스킵돼
    세션 락이 계속 점유됐고, 이후 같은 세션 요청이 전부 session_busy 로 거절됐다
    (Sentry: load constraint: session_busy 의 근본 원인).
    """
    sid = "save-fail-sess"

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(server, "auto_save", _boom)
    assert server.session_locks.is_busy(sid) is False
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
        "".join(resp.iter_lines())
    # auto_save 가 터졌어도 release 가 실행돼 세션이 다시 비어 있어야 한다.
    assert server.session_locks.is_busy(sid) is False
