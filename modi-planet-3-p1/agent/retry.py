"""재시도/백오프 — LLM(CLI·API) 호출의 일시적 실패를 흡수한다.

문제(S4): claude CLI 서브프로세스가 레이트리밋(429)·과부하(529)·네트워크/타임아웃으로
0이 아닌 코드로 죽으면 RuntimeError 가 그대로 올라가 프론트에 "서버와 연결할 수 없어요"로
보인다. 기존엔 재시도가 전혀 없었다(claude_client._call_cli / _CliStream). 또
orchestrator_stream._llm_call 의 재시도는 "529/Overloaded"만 일시적으로 인정해서
CLI 의 일반 RuntimeError(429 등)는 재시도되지 않았다. 이 모듈로 분류를 통일한다.

설계 의도(테스트 용이성):
- is_retryable_error / backoff_delay 는 부수효과 없는 순수 함수 → 단독 단위 테스트.
- run_with_retry 는 sleep 을 주입받음 → 테스트에서 실제 대기 없이 재시도 횟수/지연을 검증.

주의(오탐 방지): 마커는 "rate limit" 처럼 구문 단위로 둔다. "rate" 단독은
generate_code 같은 평문(gene-RATE)을 오탐하므로 절대 넣지 않는다.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

# 일시적(재시도 가치 있음)으로 판단할 에러 메시지 마커. 소문자 비교.
RETRYABLE_MARKERS: tuple[str, ...] = (
    "rate limit", "rate_limit", "ratelimit", "too many requests", "429",
    "overloaded", "529",
    "timeout", "timed out",
    "connection refused", "connection reset", "connection error",
    "econnreset", "broken pipe", "reset by peer",
    "502", "503", "504",
    "temporarily unavailable", "try again",
)

# Claude CLI 구독/세션 한도. 메시지에 429가 같이 들어오지만 reset 시각 전까지
# 즉시 재시도해도 성공하지 않는다. (예: "You've hit your session limit · resets 1:40pm")
QUOTA_LIMIT_MARKERS: tuple[str, ...] = (
    "session limit", "usage limit", "hit your limit", "resets ",
)

# Claude CLI 미로그인(구독 세션 만료/미인증). 재시도·API키로도 안 풀리고 운영자가
# `claude` 로그인(예: `/login`)을 해야 복구된다. 프론트엔 재시도 대신 명확히 안내한다.
# (예: "Claude CLI 오류: Not logged in · Please run /login")
# "/login" 단독은 웹앱 라우트 문자열 등에 흔해 오탐 위험 → 구문 단위로만 둔다.
AUTH_LOGIN_MARKERS: tuple[str, ...] = (
    "not logged in", "please run /login",
)

# 영구 실패(재시도해도 안 됨) 마커. 일시적 마커보다 우선 적용해 무의미한 재시도를 막는다.
NON_RETRYABLE_MARKERS: tuple[str, ...] = (
    "no such file", "command not found", "not found in path",
    "authentication", "unauthorized", "401", "403", "invalid api key",
    *QUOTA_LIMIT_MARKERS,
    *AUTH_LOGIN_MARKERS,
)


def is_quota_limit_error(message: str) -> bool:
    """Claude CLI/API 사용량 한도처럼 즉시 재시도해도 의미 없는 제한인지 판정."""
    low = (message or "").lower()
    return any(m in low for m in QUOTA_LIMIT_MARKERS)


def is_auth_login_error(message: str) -> bool:
    """Claude CLI 미로그인(세션 만료 등) — 운영자 재로그인이 필요한 상태인지 판정."""
    low = (message or "").lower()
    return any(m in low for m in AUTH_LOGIN_MARKERS)


# API(SDK) 경로의 인증 실패 마커 — 키가 없거나/틀리거나/만료된 경우.
# CLI 미로그인(AUTH_LOGIN_MARKERS)과 분리한다: 이쪽은 **CLI 로 폴백하면 살아난다**.
#
# "could not resolve authentication method" 는 anthropic SDK 가 키를 아예 못 찾을 때
# 내는 문구다(2026-08-21 실측: USE_LOCAL_CLAUDE=false 인데 ANTHROPIC_API_KEY 가 비어
# 서버 전체가 INTERNAL 로 죽었다). 401 만 보고 있으면 이 케이스를 놓친다.
API_AUTH_MARKERS: tuple[str, ...] = (
    "could not resolve authentication method",
    "authentication_error", "authentication error",
    "invalid api key", "invalid x-api-key",
    "unauthorized", "401",
    "permission_error", "403",
    "credit balance is too low",   # 크레딧 소진 — API 로는 더 못 쓰나 CLI 는 살아있다
)


def is_api_auth_error(message: str) -> bool:
    """API 키 문제(부재·무효·만료·크레딧 소진)인지 — 참이면 CLI 로 폴백할 가치가 있다."""
    low = (message or "").lower()
    return any(m in low for m in API_AUTH_MARKERS)


def is_retryable_error(message: str) -> bool:
    """에러 메시지가 '일시적'이라 재시도할 가치가 있는지 판정."""
    low = (message or "").lower()
    if any(m in low for m in NON_RETRYABLE_MARKERS):
        return False
    return any(m in low for m in RETRYABLE_MARKERS)


def backoff_delay(attempt: int, *, base: float = 2.0, factor: float = 2.0, cap: float = 30.0) -> float:
    """attempt(0부터)에 대한 지수 백오프 지연(초). base * factor**attempt, cap 으로 상한.

    예: base=2, factor=2 → 2, 4, 8, 16, ... (cap 30 에서 포화)
    """
    if attempt < 0:
        attempt = 0
    return min(cap, base * (factor ** attempt))


def run_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    is_retryable: Callable[[str], bool] = is_retryable_error,
) -> T:
    """fn() 을 호출하되, 일시적 실패면 백오프 후 max_attempts 까지 재시도한다.

    - 마지막 시도이거나 영구 실패면 예외를 그대로 올린다.
    - on_retry(attempt, delay, exc) 콜백으로 재시도 로깅/관측을 붙일 수 있다.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 재시도 여부는 is_retryable 가 판단
            last_exc = e
            if attempt >= max_attempts - 1 or not is_retryable(str(e)):
                raise
            delay = backoff_delay(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, e)
            sleep(delay)
    # 도달 불가(루프에서 return/raise) — 타입 체커 안심용.
    assert last_exc is not None
    raise last_exc
