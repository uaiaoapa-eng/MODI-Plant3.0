"""retry 모듈 단위 테스트 — sleep 주입으로 실제 대기 없이 검증."""
import pytest

from agent.retry import (
    backoff_delay,
    is_auth_login_error,
    is_quota_limit_error,
    is_retryable_error,
    run_with_retry,
)


@pytest.mark.parametrize("msg", [
    "Claude CLI 실패 (code 1): 429 too many requests",
    "rate limit exceeded",
    "Error: Overloaded",
    "server returned 529",
    "Command 'claude' timed out after 180 seconds",
    "connection reset by peer",
    "503 Service Unavailable",
    "please try again later",
])
def test_retryable_messages(msg):
    assert is_retryable_error(msg) is True


@pytest.mark.parametrize("msg", [
    "[Errno 2] No such file or directory: 'claude'",
    "authentication failed",
    "401 Unauthorized",
    "invalid api key",
    "Claude CLI 실패 (code 1): {\"api_error_status\":429,\"result\":\"You've hit your session limit · resets 1:40pm (Asia/Seoul)\"}",
    "429 rate limit: usage limit resets 1:40pm",
    "some logic error in generate_code",  # 'rate' 부분문자열 오탐 방지(gene-RATE)
    "",
])
def test_non_retryable_messages(msg):
    assert is_retryable_error(msg) is False


def test_non_retryable_wins_over_retryable():
    # 429(일시적) 마커와 401(영구) 마커가 함께 있으면 영구 실패가 우선.
    assert is_retryable_error("429 received but 401 unauthorized") is False


def test_quota_limit_is_not_retryable_even_with_429():
    msg = "Claude CLI 실패 (code 1): {\"api_error_status\":429,\"result\":\"You've hit your session limit · resets 1:40pm (Asia/Seoul)\"}"
    assert is_quota_limit_error(msg) is True
    assert is_retryable_error(msg) is False


@pytest.mark.parametrize("msg", [
    "Claude CLI 오류: Not logged in · Please run /login",
    "not logged in",
    "Please run /login to continue",
])
def test_auth_login_error_detected_and_not_retryable(msg):
    # 미로그인은 재시도해도 안 풀리므로 non-retryable + 전용 분류로 잡혀야 한다.
    assert is_auth_login_error(msg) is True
    assert is_retryable_error(msg) is False


@pytest.mark.parametrize("msg", [
    "429 rate limit",
    "You've hit your session limit · resets 1:40pm",
    "some logic error in generate_code",
    "",
])
def test_non_login_errors_not_flagged_as_login(msg):
    assert is_auth_login_error(msg) is False


def test_backoff_is_monotonic_and_capped():
    assert backoff_delay(0) == 2.0
    assert backoff_delay(1) == 4.0
    assert backoff_delay(2) == 8.0
    assert backoff_delay(3) == 16.0
    assert backoff_delay(100) == 30.0  # cap
    assert backoff_delay(-5) == 2.0     # 음수 방어


def test_succeeds_after_transient_failures():
    calls = {"n": 0}
    slept: list[float] = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 rate limit")
        return "ok"

    out = run_with_retry(fn, max_attempts=3, sleep=slept.append)
    assert out == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2  # 2번 재시도 → 2번 sleep


def test_gives_up_after_max_attempts():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("overloaded")

    with pytest.raises(RuntimeError):
        run_with_retry(fn, max_attempts=3, sleep=lambda _d: None)
    assert calls["n"] == 3


def test_no_retry_on_permanent_failure():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("authentication failed")

    with pytest.raises(RuntimeError):
        run_with_retry(fn, max_attempts=5, sleep=lambda _d: None)
    assert calls["n"] == 1  # 영구 실패는 즉시 포기


def test_on_retry_callback_invoked():
    events: list[tuple[int, float, str]] = []

    def fn():
        if len(events) < 1:
            raise RuntimeError("timeout")
        return 42

    out = run_with_retry(
        fn,
        sleep=lambda _d: None,
        on_retry=lambda a, d, e: events.append((a, d, str(e))),
    )
    assert out == 42
    assert len(events) == 1
    assert events[0][0] == 0  # 첫 재시도 attempt=0


def test_success_first_try_no_sleep():
    slept: list[float] = []
    out = run_with_retry(lambda: "done", sleep=slept.append)
    assert out == "done"
    assert slept == []
