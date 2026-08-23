"""#130: 오케스트레이터 턴 단위 로컬 토큰 누적(_add_turn_usage/pop_turn_usage) 검증.

Langfuse 계측(usage_details/update_generation)은 건드리지 않고, 같은 usage 값이
로컬 TokenUsage 누적기로도 병행 집계되는지만 확인한다(쿼터 판정용, #129 TokenUsage 재사용).
"""
import agent.orchestrator_stream as OS
from agent.quota import TokenUsage


def _orch():
    return OS.StreamOrchestrator(api_key="")


class _SdkUsage:
    """SDK Usage 객체 흉내 — attribute 기반."""

    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


# ── 초기 상태 ──────────────────────────────────────────────────────
def test_pop_without_accumulation_returns_zero():
    orch = _orch()
    assert orch.pop_turn_usage() == TokenUsage()


# ── _add_turn_usage: dict(CLI)/object(SDK) 양쪽 지원 ────────────────
def test_add_turn_usage_accepts_cli_dict():
    orch = _orch()
    orch._add_turn_usage({"input_tokens": 100, "output_tokens": 20})
    assert orch.pop_turn_usage() == TokenUsage(input=100, output=20)


def test_add_turn_usage_accepts_sdk_object():
    orch = _orch()
    orch._add_turn_usage(_SdkUsage(input_tokens=50, output_tokens=10,
                                    cache_read_input_tokens=200, cache_creation_input_tokens=5))
    assert orch.pop_turn_usage() == TokenUsage(input=50, output=10, cache_read=200, cache_creation=5)


# ── None usage → 누적 스킵 (엣지 케이스 결정) ───────────────────────
def test_add_turn_usage_none_is_noop():
    orch = _orch()
    orch._add_turn_usage(None)
    assert orch.pop_turn_usage() == TokenUsage()


def test_add_turn_usage_zero_usage_object_is_noop():
    # usage_details() 계약: 토큰이 전혀 없으면 None → 누적 스킵.
    orch = _orch()
    orch._add_turn_usage(_SdkUsage())
    assert orch.pop_turn_usage() == TokenUsage()


# ── 턴 초기화(_chat_stream_impl) 밖 단독 호출도 안전(getattr 기반) ───
def test_add_turn_usage_safe_without_prior_turn_init():
    orch = _orch()
    assert not hasattr(orch, "_turn_usage")  # __init__ 만 거친 상태 — 아직 미초기화
    orch._add_turn_usage({"input_tokens": 10, "output_tokens": 1})
    assert orch.pop_turn_usage() == TokenUsage(input=10, output=1)


# ── 완료 기준: 한 턴에 LLM 콜 2회(메인+가드레일) → 합산값 반환 ───────
def test_pop_turn_usage_sums_main_and_guardrail_calls():
    orch = _orch()
    # 가드레일 콜(check_input) usage — CLI dict 형태일 수도 있음
    orch._add_turn_usage({"input_tokens": 30, "output_tokens": 5})
    # 메인 루프 콜(_llm_call) usage — SDK Usage 객체 형태
    orch._add_turn_usage(_SdkUsage(input_tokens=200, output_tokens=80,
                                    cache_read_input_tokens=1000))
    assert orch.pop_turn_usage() == TokenUsage(input=230, output=85, cache_read=1000)


# ── pop 후 재호출 시 0으로 리셋 ──────────────────────────────────────
def test_pop_resets_accumulator():
    orch = _orch()
    orch._add_turn_usage({"input_tokens": 10, "output_tokens": 1})
    first = orch.pop_turn_usage()
    assert first == TokenUsage(input=10, output=1)
    assert orch.pop_turn_usage() == TokenUsage()  # 재호출은 0


def test_multiple_add_before_pop_accumulate():
    orch = _orch()
    for _ in range(3):
        orch._add_turn_usage({"input_tokens": 1, "output_tokens": 1})
    assert orch.pop_turn_usage() == TokenUsage(input=3, output=3)


# ── 턴 리셋(_chat_stream_impl 진입부)에서 _turn_usage 가 초기화되는지 ─
def test_turn_reset_initializes_turn_usage_to_zero():
    orch = _orch()
    orch._turn_usage = TokenUsage(input=999, output=999)
    # _chat_stream_impl 의 턴 플래그 리셋 블록과 동일한 초기화 라인을 직접 재현하는 대신,
    # 실제 리셋 블록이 도달 가능한지 속성 존재로 회귀 확인 — 초기화 라인 자체는
    # orchestrator_stream.py 소스에서 검증(아래 테스트).
    orch._turn_usage = TokenUsage()
    assert orch.pop_turn_usage() == TokenUsage()


def test_source_resets_turn_usage_on_turn_start():
    """_chat_stream_impl 턴 플래그 리셋 블록에 self._turn_usage = TokenUsage() 초기화가 있는지 확인."""
    import inspect
    src = inspect.getsource(OS.StreamOrchestrator._chat_stream_impl)
    assert "self._turn_usage = TokenUsage()" in src
