"""에러 카탈로그 단위 테스트 — agent/errors.py.

검증 범위:
- CATALOG 가 모든 ErrorCode 를 빠짐없이 담고, 코드가 일치한다.
- error_event: type/code/message/retryable 구조, message 오버라이드,
  retry_after 조건부 포함, extra 병합, 이관 문구 바이트 동일성.
- error_response: 본문 구조, http_status, detail 규칙, INTERNAL detail 금지.
"""
import json

import pytest

from agent.errors import CATALOG, ErrorCode, ErrorSpec, error_event, error_response


def test_catalog_covers_all_codes():
    assert set(CATALOG.keys()) == set(ErrorCode)
    for code, spec in CATALOG.items():
        assert isinstance(spec, ErrorSpec)
        assert spec.code is code
        assert spec.user_message  # 비어있지 않음
        assert isinstance(spec.retryable, bool)
        assert 400 <= spec.http_status <= 599


def test_error_event_basic_shape():
    ev = error_event(ErrorCode.SESSION_BUSY)
    assert ev["type"] == "error"
    assert ev["code"] == "session_busy"
    assert ev["retryable"] is True
    assert ev["message"] == CATALOG[ErrorCode.SESSION_BUSY].user_message
    assert "retry_after" not in ev
    # 순수 JSON 직렬화 가능(코드가 str Enum 이어도 값으로 나감)
    assert json.loads(json.dumps(ev))["code"] == "session_busy"


def test_error_event_message_override_and_retry_after_and_extra():
    ev = error_event(ErrorCode.LLM_QUOTA, message="사용자 지정", retry_after=42, foo="bar")
    assert ev["message"] == "사용자 지정"
    assert ev["retry_after"] == 42
    assert ev["foo"] == "bar"
    assert ev["code"] == "llm_quota"
    assert ev["retryable"] is CATALOG[ErrorCode.LLM_QUOTA].retryable


def test_error_event_retry_after_omitted_when_none():
    ev = error_event(ErrorCode.LLM_OVERLOADED, retry_after=None)
    assert "retry_after" not in ev


@pytest.mark.parametrize("code,expected", [
    (ErrorCode.SESSION_BUSY,
     "이 세션의 이전 요청을 아직 처리 중이에요. 잠시 후 다시 시도해주세요."),
    (ErrorCode.INTERNAL,
     "처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요."),
    (ErrorCode.LLM_AUTH,
     "지금 코딩 도우미에 연결할 수 없어요. 잠시 후 다시 시도해 주세요. "
     "문제가 계속되면 선생님께 알려주세요."),
    (ErrorCode.LLM_QUOTA,
     "Claude 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요."),
])
def test_ported_messages_byte_for_byte(code, expected):
    """기존 하드코딩 문구가 바이트 단위로 동일하게 이관됐는지."""
    assert CATALOG[code].user_message == expected
    assert error_event(code)["message"] == expected


def _body(resp):
    return json.loads(bytes(resp.body))


def test_error_response_shape_and_status():
    resp = error_response(ErrorCode.NOT_FOUND)
    assert resp.status_code == CATALOG[ErrorCode.NOT_FOUND].http_status == 404
    body = _body(resp)
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == CATALOG[ErrorCode.NOT_FOUND].user_message
    assert "detail" not in body["error"]


def test_error_response_detail_passthrough():
    resp = error_response(ErrorCode.INVALID_INPUT, detail="field 'x' 누락")
    body = _body(resp)
    assert body["error"]["detail"] == "field 'x' 누락"
    assert resp.status_code == 400


def test_error_response_internal_omits_detail():
    resp = error_response(ErrorCode.INTERNAL, detail="스택트레이스 등 내부정보")
    body = _body(resp)
    assert "detail" not in body["error"]
    assert resp.status_code == 500
    assert body["error"]["code"] == "internal"


def test_error_response_message_override():
    resp = error_response(ErrorCode.UPSTREAM_ERROR, message="RAG 서버 응답 없음")
    body = _body(resp)
    assert body["error"]["message"] == "RAG 서버 응답 없음"
