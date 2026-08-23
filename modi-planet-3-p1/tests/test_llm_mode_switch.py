"""LLM 호출 방식 스위치(USE_LOCAL_CLAUDE) 회귀 테스트 — 네트워크/토큰 미사용.

배경:
  운영은 CLI 구독 모드(`USE_LOCAL_CLAUDE=true`)로 돌지만, 전 사용자가 **구독 1계정**을
  공유해 5시간 윈도 쿼터를 함께 태운다. 2026-08-21 부하 테스트에서 실제로 소진돼
  "Claude 사용 한도에 도달했어요" 가 전 사용자에게 나갔고, 레플리카를 3대로 늘려도
  같은 계정을 쓰므로 해결되지 않았다. 탈출구가 API 모드 전환인데 `tests/` 에 이 분기를
  덮는 테스트가 **하나도 없었다** — 즉 전환이 실제로 되는지 아무도 보증하지 못했다.

  게다가 docker-compose.yml 이 `USE_LOCAL_CLAUDE: "true"` 를 하드코딩하고 있어
  (compose 의 environment 는 env_file 을 덮어쓴다) 서버 .env 를 고쳐도 전환이 되지
  않는 상태였다. 이 파일은 **코드 쪽 분기**를 고정하고, compose 쪽은 같은 PR 에서
  `${USE_LOCAL_CLAUDE:-true}` 로 오버라이드 가능하게 바꾼다.

여기서는 실제 API 를 부르지 않는다. `create_client` 가 어떤 구현체를 돌려주는지,
환경변수 파싱이 어떤 값에서 참/거짓인지만 검증한다.
"""
import pytest

try:
    from agent import claude_client
    from agent.claude_client import LocalClaudeClient, create_client, _use_local_cli
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"claude_client import 불가: {e}", allow_module_level=True)


# ──────────────────────────────────────────────────────────────────────────────
# 환경변수 파싱 — 어떤 값이 CLI 모드인가
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_api_fallback_latch():
    """API 폴백 래치가 테스트 간 새면 create_client 가 엉뚱하게 CLI 를 준다."""
    claude_client.reset_api_fallback()
    yield
    claude_client.reset_api_fallback()


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", " true ", "YES"])
def test_cli_mode_for_truthy_values(monkeypatch, value):
    """참으로 취급되는 값들 — 대소문자·공백·1·yes 를 모두 받아야 한다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", value)
    assert _use_local_cli() is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off", ""])
def test_api_mode_for_falsy_values(monkeypatch, value):
    """거짓으로 취급되는 값들 — 여기 하나라도 참으로 새면 API 전환이 조용히 무시된다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", value)
    assert _use_local_cli() is False


def test_default_is_cli_when_unset(monkeypatch):
    """미설정이면 기존 동작(CLI) 유지 — 배포에서 값이 빠져도 갑자기 API 로 새지 않는다."""
    monkeypatch.delenv("USE_LOCAL_CLAUDE", raising=False)
    assert _use_local_cli() is True


# ──────────────────────────────────────────────────────────────────────────────
# create_client 분기 — 어떤 구현체가 나오는가
# ──────────────────────────────────────────────────────────────────────────────

def test_cli_mode_returns_local_client(monkeypatch):
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "true")
    assert isinstance(create_client("sk-ant-무시됨"), LocalClaudeClient)


def test_api_mode_returns_anthropic_backed_client(monkeypatch):
    """API 모드는 anthropic SDK 로 가는 클라이언트를 돌려줘야 한다(네트워크 호출 없음).

    ⚠ 구현체 타입을 직접 단언하지 않는다 — API 인증 실패 시 CLI 로 넘기는 폴백 래퍼
    (_ApiWithCliFallback)가 감싸고 있기 때문이다. 여기서 봐야 할 것은 "CLI 가 아니고,
    호출부가 기대하는 표면이 있고, 실제로 anthropic SDK 를 물고 있다" 이다.
    """
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    client = create_client("sk-ant-test-key")
    assert not isinstance(client, LocalClaudeClient)
    # 호출부(orchestrator)가 기대하는 표면이 있는지 — 없으면 전환 즉시 AttributeError.
    assert hasattr(client, "messages")
    assert hasattr(client.messages, "create")
    assert hasattr(client.messages, "stream")
    # 래퍼가 감싸고 있더라도 밑단은 anthropic SDK 여야 한다.
    inner = getattr(client, "_api_client", client)
    assert type(inner).__module__.startswith("anthropic")


def test_api_mode_passes_key_through(monkeypatch):
    """전달한 키가 SDK 클라이언트에 실제로 실려야 한다 — 안 실리면 401 로만 드러난다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel-12345")
    client = create_client("sk-ant-sentinel-12345")
    assert client.api_key == "sk-ant-sentinel-12345"


def test_api_mode_does_not_spawn_subprocess(monkeypatch):
    """API 모드는 서브프로세스를 띄우지 않아야 한다.

    CLI 모드가 LLM 호출마다 node 프로세스를 띄우는 것이 동접 40 에서 16코어 load 161 을
    만든 직접 원인이었다(2026-08-21 실측, SSH 조차 막힘). API 전환의 핵심 이득이 이
    서브프로세스 제거이므로, 클라이언트 생성 경로에 Popen 이 끼어들지 않는지 고정한다.
    """
    calls = []
    monkeypatch.setattr(claude_client.subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("API 모드에서 subprocess.Popen 이 호출됐다")))
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    create_client("sk-ant-test-key")
    assert calls == []


# ──────────────────────────────────────────────────────────────────────────────
# 전환 가능성 자체 — compose 하드코딩 회귀 방지
# ──────────────────────────────────────────────────────────────────────────────

def test_compose_allows_overriding_use_local_claude():
    """docker-compose.yml 이 USE_LOCAL_CLAUDE 를 하드코딩하면 안 된다.

    compose 의 `environment:` 는 `env_file:` 을 덮어쓴다. 예전엔 여기가 "true" 로
    고정돼 있어, 서버 .env 에 USE_LOCAL_CLAUDE=false 를 넣어도 API 모드로 전환되지
    않았다(값을 바꿔도 아무 일이 안 일어나 원인 파악이 어려운 종류의 버그).
    같은 함정을 RAG_UPSTREAM 주석이 이미 실측으로 경고하고 있다.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'USE_LOCAL_CLAUDE: "${USE_LOCAL_CLAUDE:-true}"' in text, (
        "compose 가 USE_LOCAL_CLAUDE 를 오버라이드 가능하게 두어야 한다 "
        "(하드코딩하면 서버 .env 로 API 전환이 불가능)"
    )
    assert 'USE_LOCAL_CLAUDE: "true"' not in text, "하드코딩된 true 가 남아 있다"
