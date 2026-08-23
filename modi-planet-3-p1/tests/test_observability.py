"""agent.observability 단위 테스트.

실제 Sentry 로 이벤트를 보내지 않는다 — DSN 없는(비활성) 경로의 안전성과
순수 함수(스크럽/파서/릴리스)만 검증한다.
"""

import pytest

import agent.observability as obs


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """각 테스트는 비활성(_initialized=False) 상태에서 시작한다."""
    monkeypatch.setattr(obs, "_initialized", False, raising=False)
    yield


# ── load_constraint 분류(순수 함수) ──

@pytest.mark.parametrize("msg,expected", [
    # 세션/사용량 한도 → quota (429 가 섞여 있어도 quota 가 우선; 재시도 무의미)
    ("Claude CLI 오류: You've hit your session limit · resets 7:20pm (Asia/Seoul)", "llm_quota_limit"),
    ('{"is_error":true,"api_error_status":429,"result":"You\'ve hit your session limit · resets"}', "llm_quota_limit"),
    ("usage limit reached", "llm_quota_limit"),
    # 순수 레이트리밋/과부하 → rate
    ("rate limit exceeded", "llm_rate_limit"),
    ("Error 429: too many requests", "llm_rate_limit"),
    ("Overloaded (529)", "llm_rate_limit"),
    # 그 외 → None (일반 에러는 error 이슈로 남긴다)
    ("JSONDecodeError: Expecting value", None),
    ("", None),
])
def test_classify_load_constraint(msg, expected):
    assert obs.classify_load_constraint(msg) == expected


# ── 비활성(no-op) 안전성 ──

def test_init_returns_false_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert obs.init_sentry("server") is False
    assert obs.is_enabled() is False


def test_init_returns_false_with_blank_dsn(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "   ")
    assert obs.init_sentry("server") is False


def test_helpers_are_noops_when_disabled():
    # 비활성일 때 어떤 헬퍼도 예외를 던지지 않아야 한다(프로덕션 코드가 무조건 호출하므로).
    obs.set_session_context("s1", "u1", mode="design", coding_type="react")
    obs.add_breadcrumb("hi", category="test")
    obs.capture_load_constraint("session_busy", session_id="s1")
    obs.capture_chat_exception(RuntimeError("boom"), session_id="s1")
    obs.note_llm_failure(RuntimeError("rate limit exceeded (429)"))
    obs.tag_llm_usage(None, {"input_tokens": 1}, 0.01)
    obs.flush()


def test_llm_span_yields_none_when_disabled():
    with obs.llm_span("ai.run.test", "model=x") as span:
        assert span is None


# ── 백그라운드 로거 소음 제외 (phantom "Traceback" 이슈 방지) ──

def test_ignore_noisy_loggers_does_not_raise():
    # sentry_sdk 미설치/미초기화 환경에서도 예외 없이 no-op 이어야 한다(프로덕션 init 경로 안전).
    obs._ignore_noisy_loggers()


def test_ignored_loggers_cover_background_telemetry():
    # self-hosted 텔레메트리/HTTP 백그라운드 로거가 목록에 있어야 phantom ERROR 이슈가 안 쌓인다.
    for name in ("langfuse", "opentelemetry", "httpx"):
        assert name in obs._IGNORED_LOGGERS


# ── PII 스크럽 ──

def test_scrub_redacts_nested_pii():
    event = {
        "message": "내 번호는 010-1234-5678 이야",
        "extra": {"email": "kid@example.com", "list": ["010-9999-8888", "ok"]},
        "level": "error",
    }
    scrubbed = obs._scrub(event)
    assert "010-1234-5678" not in scrubbed["message"]
    assert "[전화번호]" in scrubbed["message"]
    assert "kid@example.com" not in scrubbed["extra"]["email"]
    assert "010-9999-8888" not in scrubbed["extra"]["list"][0]
    assert scrubbed["extra"]["list"][1] == "ok"
    assert scrubbed["level"] == "error"


def test_scrub_depth_cap_does_not_crash():
    # 매우 깊은 구조에서도 예외 없이 반환(상한 도달 시 원본 유지).
    deep = cur = {}
    for _ in range(20):
        cur["child"] = {}
        cur = cur["child"]
    assert obs._scrub(deep) is not None


def test_before_send_scrubs_message():
    out = obs._before_send({"message": "전화 010-1234-5678"}, {})
    assert "010-1234-5678" not in out["message"]


def test_before_send_drops_noisy_submodule_logger_events():
    # EDU-AGENT-P: sentry_sdk.ignore_logger("opentelemetry")는 record.name과 완전 일치만
    # 걸러내(logging.py의 집합 조회) "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    # 같은 서브모듈 로거명은 못 잡는다. before_send에서 prefix로 이중 방어해야 한다.
    event = {
        "logger": "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "message": "Failed to export span batch code: None, reason: Read timed out",
    }
    assert obs._before_send(event, {}) is None


def test_before_send_keeps_events_from_app_loggers():
    event = {"logger": "agent.orchestrator_stream", "message": "실제 앱 에러"}
    out = obs._before_send(event, {})
    assert out is not None
    assert out["message"] == "실제 앱 에러"


def test_before_send_transaction_scrubs_only_request():
    event = {"transaction": "POST /chat", "request": {"data": "이메일 a@b.com"}}
    out = obs._before_send_transaction(event, {})
    assert "a@b.com" not in out["request"]["data"]
    assert out["transaction"] == "POST /chat"


# ── 파서/릴리스 ──

@pytest.mark.parametrize("raw,expected", [
    ("", 0.5),          # 미설정 → default
    ("0.2", 0.2),
    ("1.5", 1.0),       # 상한 클램프
    ("-0.3", 0.0),      # 하한 클램프
    ("abc", 0.5),       # 파싱 불가 → default
])
def test_sample_rate(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("X_RATE", raw)
    else:
        monkeypatch.delenv("X_RATE", raising=False)
    assert obs._sample_rate("X_RATE", 0.5) == expected


def test_release_explicit_env(monkeypatch):
    monkeypatch.setenv("SENTRY_RELEASE", "edu-agent@v9")
    assert obs._release() == "edu-agent@v9"


def test_release_fallback_has_prefix(monkeypatch):
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    rel = obs._release()
    assert rel.startswith("edu-agent@")
