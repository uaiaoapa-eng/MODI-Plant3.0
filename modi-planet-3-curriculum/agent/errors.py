"""에러 코드 카탈로그 — SSE error 이벤트/HTTP 에러 응답을 구조화한다.

배경: 에러가 하드코딩 문자열이라 프론트가 "쿼터 초과 vs 서버 장애"를 구분할 수 없고,
LLM 구독 한도 에러는 `type:"token"`(일반 채팅 토큰)으로 위장되어 프론트가 에러로
인식조차 못 했다. 이 모듈은 코드→메시지/재시도가능/HTTP상태 매핑을 한 곳에 모아,
SSE(`error_event`)와 HTTP(`error_response`)가 같은 카탈로그를 쓰게 한다.

설계 원칙:
- **additive**: 기존 SSE 이벤트의 `type` 값은 그대로 두고 `code`/`retryable`만 추가한다.
- **문구 이관**: user_message 는 기존 하드코딩 문구를 그대로 옮긴 것이다(변경 아님).
- **분류는 retry.py 소유**: 여기서는 분류를 하지 않고 코드→스펙 매핑만 한다.
- **정보 노출 금지**: `error_response`의 INTERNAL 은 detail 을 항상 생략한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    QUOTA_EXCEEDED = "quota_exceeded"
    SESSION_BUSY = "session_busy"
    LLM_QUOTA = "llm_quota"
    LLM_AUTH = "llm_auth"
    LLM_OVERLOADED = "llm_overloaded"
    UPSTREAM_ERROR = "upstream_error"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    INTERNAL = "internal"
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"   # 예약(발생 경로 없음)
    AUTH_EXPIRED = "auth_expired"     # 예약(발생 경로 없음)


@dataclass(frozen=True)
class ErrorSpec:
    code: ErrorCode
    user_message: str   # 한국어 기본 메시지 — 기존 하드코딩 문구를 그대로 이관
    retryable: bool
    http_status: int


CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.QUOTA_EXCEEDED: ErrorSpec(
        ErrorCode.QUOTA_EXCEEDED,
        "사용 한도에 도달했어요. 잠시 후 다시 시도해주세요.",
        retryable=False, http_status=429),
    ErrorCode.SESSION_BUSY: ErrorSpec(
        ErrorCode.SESSION_BUSY,
        # server.py 의 busy_stream 문구를 그대로 이관.
        "이 세션의 이전 요청을 아직 처리 중이에요. 잠시 후 다시 시도해주세요.",
        retryable=True, http_status=409),
    ErrorCode.LLM_QUOTA: ErrorSpec(
        ErrorCode.LLM_QUOTA,
        # orchestrator _quota_limit_message 의 리셋시각 없는 기본 문구를 이관.
        "Claude 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요.",
        retryable=False, http_status=429),
    ErrorCode.LLM_AUTH: ErrorSpec(
        ErrorCode.LLM_AUTH,
        # orchestrator auth 분기의 사과 문구를 그대로 이관.
        "지금 코딩 도우미에 연결할 수 없어요. 잠시 후 다시 시도해 주세요. "
        "문제가 계속되면 선생님께 알려주세요.",
        retryable=False, http_status=503),
    ErrorCode.LLM_OVERLOADED: ErrorSpec(
        ErrorCode.LLM_OVERLOADED,
        "서버가 잠시 붐비고 있어요. 잠시 후 다시 시도해주세요.",
        retryable=True, http_status=503),
    ErrorCode.UPSTREAM_ERROR: ErrorSpec(
        ErrorCode.UPSTREAM_ERROR,
        "연동 서비스에 일시적인 문제가 있어요. 잠시 후 다시 시도해주세요.",
        retryable=True, http_status=502),
    ErrorCode.NOT_FOUND: ErrorSpec(
        ErrorCode.NOT_FOUND,
        "요청한 대상을 찾을 수 없어요.",
        retryable=False, http_status=404),
    ErrorCode.INVALID_INPUT: ErrorSpec(
        ErrorCode.INVALID_INPUT,
        "입력이 올바르지 않아요.",
        retryable=False, http_status=400),
    ErrorCode.INTERNAL: ErrorSpec(
        ErrorCode.INTERNAL,
        # server.py 의 generic 에러 스트림 문구를 그대로 이관.
        "처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요.",
        retryable=False, http_status=500),
    ErrorCode.BLOCKED: ErrorSpec(
        ErrorCode.BLOCKED,
        "요청을 처리할 수 없어요.",
        retryable=False, http_status=403),
    ErrorCode.AUTH_REQUIRED: ErrorSpec(
        ErrorCode.AUTH_REQUIRED,
        "로그인이 필요해요.",
        retryable=False, http_status=401),
    ErrorCode.AUTH_EXPIRED: ErrorSpec(
        ErrorCode.AUTH_EXPIRED,
        "로그인이 만료되었어요. 다시 로그인해주세요.",
        retryable=False, http_status=401),
}


def error_event(code: ErrorCode, *, message: str | None = None,
                retry_after: int | None = None, **extra) -> dict:
    """SSE error 이벤트 dict 를 만든다.

    반환: {"type":"error","code","message","retryable"[,"retry_after",...extra]}
    - message 미지정 시 카탈로그 user_message 를 쓴다.
    - retry_after 는 주어졌을 때만 포함한다.
    - extra 는 그대로 병합한다(호출자가 추가 필드를 붙일 수 있게).
    """
    spec = CATALOG[code]
    event: dict = {
        "type": "error",
        "code": code.value,
        "message": message if message is not None else spec.user_message,
        "retryable": spec.retryable,
    }
    if retry_after is not None:
        event["retry_after"] = retry_after
    if extra:
        event.update(extra)
    return event


def error_response(code: ErrorCode, *, message: str | None = None,
                   detail: str | None = None) -> JSONResponse:
    """HTTP 에러 응답을 만든다.

    본문: {"ok": false, "error": {"code","message"[,"detail"]}}
    상태코드: 카탈로그의 http_status.
    - message 미지정 시 카탈로그 user_message 를 쓴다.
    - detail 은 호출자가 넘긴 값만 포함한다(카탈로그에는 없다).
    - INTERNAL 은 내부 정보 노출 금지 원칙에 따라 detail 을 항상 생략한다.
    """
    spec = CATALOG[code]
    error: dict = {
        "code": code.value,
        "message": message if message is not None else spec.user_message,
    }
    if detail is not None and code is not ErrorCode.INTERNAL:
        error["detail"] = detail
    return JSONResponse({"ok": False, "error": error}, status_code=spec.http_status)
