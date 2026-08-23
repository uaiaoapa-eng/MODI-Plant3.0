"""API 인증 실패 시 CLI 자동 폴백 회귀 테스트 — 네트워크/토큰 미사용.

배경 (2026-08-21 실서비스 사고):
  `USE_LOCAL_CLAUDE=false` 로 API 모드를 켰는데 `ANTHROPIC_API_KEY` 가 비어 있어
  **모든 /chat 이 INTERNAL 로 죽었다.** Langfuse 실제 에러:

      "Could not resolve authentication method. Expected one of api_key,
       auth_token, or credentials to be set."

  API 는 키 만료·크레딧 소진으로 언제든 끊길 수 있고, 그때 **서비스가 통째로 멈추는
  것이 가장 위험하다.** CLI 구독 경로는 그대로 살아있으므로 자동으로 넘긴다.

  여기서는 실제 API 를 부르지 않는다. 가짜 클라이언트로 인증 실패를 주입해
  폴백이 일어나는지, 그리고 **인증과 무관한 에러는 폴백하지 않고 그대로 올라가는지**
  (진짜 버그를 CLI 로 숨기면 안 된다) 검증한다.
"""
import pytest

try:
    from agent import claude_client as cc
    from agent.claude_client import LocalClaudeClient, create_client
    from agent.retry import is_api_auth_error
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"claude_client import 불가: {e}", allow_module_level=True)


@pytest.fixture(autouse=True)
def _clean_latch():
    """테스트 간 폴백 래치가 새지 않도록 매번 초기화."""
    cc.reset_api_fallback()
    yield
    cc.reset_api_fallback()


# ──────────────────────────────────────────────────────────────────────────────
# 분류기 — 무엇을 "API 인증 실패" 로 볼 것인가
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "Could not resolve authentication method. Expected one of api_key, auth_token…",  # 실제 사고 문구
    "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error'}}",
    "invalid api key",
    "Invalid x-api-key",
    "unauthorized",
    "Error code: 403 permission_error",
    "Your credit balance is too low to access the Anthropic API",
])
def test_api_auth_errors_detected(msg):
    assert is_api_auth_error(msg) is True


@pytest.mark.parametrize("msg", [
    "rate limit exceeded", "529 overloaded", "connection reset",
    "timed out", "500 internal server error",
    "generate_code 도구 호출 실패",     # 평문 오탐 방지
])
def test_non_auth_errors_not_misclassified(msg):
    """일시적/기능적 에러를 인증 실패로 오판하면 멀쩡한 API 를 버리고 CLI 로 내려간다."""
    assert is_api_auth_error(msg) is False


# ──────────────────────────────────────────────────────────────────────────────
# create_client — 키가 없으면 네트워크 왕복 없이 즉시 CLI
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_key_falls_back_to_cli_immediately(monkeypatch):
    """★ 이번 사고의 정확한 재현: API 모드인데 키가 비었다 → 죽지 말고 CLI 로."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(create_client(""), LocalClaudeClient)


def test_blank_key_falls_back_to_cli(monkeypatch):
    """공백만 있는 값도 '없음' 으로 취급해야 한다(.env 편집 실수 흡수)."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert isinstance(create_client("   "), LocalClaudeClient)


def test_valid_key_uses_api_with_fallback_wrapper(monkeypatch):
    """키가 있으면 API 를 쓰되, 폴백 래퍼로 감싸 나와야 한다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    client = create_client("sk-ant-test-key")
    assert not isinstance(client, LocalClaudeClient)
    assert isinstance(client, cc._ApiWithCliFallback)
    assert client.api_key == "sk-ant-test-key"
    assert hasattr(client.messages, "create") and hasattr(client.messages, "stream")


def test_cli_mode_unaffected(monkeypatch):
    """기본(CLI) 경로는 폴백 도입과 무관하게 그대로여야 한다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "true")
    assert isinstance(create_client("sk-ant-test-key"), LocalClaudeClient)


# ──────────────────────────────────────────────────────────────────────────────
# 호출 중 인증 실패 → 같은 호출을 CLI 로 재시도
# ──────────────────────────────────────────────────────────────────────────────

class _FakeApiMessages:
    """create/stream 이 지정한 예외를 던지는 가짜 SDK messages."""

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return "API-결과"

    def stream(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        raise AssertionError("이 테스트에서는 성공 경로를 쓰지 않는다")


def _wrap(exc, monkeypatch, cli_result="CLI-결과"):
    """폴백 래퍼를 만들고 CLI 쪽을 가짜로 바꾼다."""
    fake_api = _FakeApiMessages(exc)
    msgs = cc._FallbackMessages(fake_api)

    class _FakeCli:
        def create(self, **kw): return cli_result
        def stream(self, **kw): return cli_result
    msgs._cli = _FakeCli()
    return msgs, fake_api


def test_create_falls_back_to_cli_on_auth_error(monkeypatch):
    msgs, fake = _wrap(RuntimeError("Error code: 401 authentication_error"), monkeypatch)
    assert msgs.create(model="m", max_tokens=8, system="", messages=[]) == "CLI-결과"
    assert fake.calls == 1, "API 를 한 번은 시도해야 한다"


def test_stream_falls_back_to_cli_on_auth_error(monkeypatch):
    msgs, fake = _wrap(RuntimeError("Could not resolve authentication method"), monkeypatch)
    assert msgs.stream(model="m", max_tokens=8, system="", messages=[]) == "CLI-결과"
    assert fake.calls == 1


def test_non_auth_error_propagates_not_masked(monkeypatch):
    """★ 인증과 무관한 에러는 CLI 로 숨기지 않고 그대로 올려야 한다.

    여기서 폴백해 버리면 실제 버그(도구 실패·레이트리밋 등)가 조용히 가려진다.
    """
    msgs, fake = _wrap(RuntimeError("rate limit exceeded"), monkeypatch)
    with pytest.raises(RuntimeError, match="rate limit"):
        msgs.create(model="m", max_tokens=8, system="", messages=[])
    assert fake.calls == 1


# ──────────────────────────────────────────────────────────────────────────────
# 래치 — 한 번 실패하면 죽은 API 를 계속 두드리지 않는다
# ──────────────────────────────────────────────────────────────────────────────

def test_latch_routes_subsequent_clients_to_cli(monkeypatch):
    """인증 실패 기록 후에는 create_client 가 곧바로 CLI 를 준다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    assert isinstance(create_client("sk-ant-test-key"), cc._ApiWithCliFallback)

    cc.note_api_auth_failure("Error code: 401 authentication_error")
    assert isinstance(create_client("sk-ant-test-key"), LocalClaudeClient)


def test_latch_expires_after_cooldown(monkeypatch):
    """쿨다운이 지나면 API 를 다시 시도한다 — 키를 고치면 재배포 없이 복귀."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("API_FALLBACK_COOLDOWN_SECONDS", "600")

    cc.note_api_auth_failure("401")
    assert cc._api_is_latched_down(now=cc._api_failed_at + 599) is True
    assert cc._api_is_latched_down(now=cc._api_failed_at + 601) is False


def test_reset_clears_latch():
    cc.note_api_auth_failure("401")
    assert cc._api_is_latched_down() is True
    cc.reset_api_fallback()
    assert cc._api_is_latched_down() is False
