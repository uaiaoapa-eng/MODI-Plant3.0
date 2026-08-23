"""#67 T1: _llm_call 의 모드별 system 프롬프트 캐시 처리 검증.

벤치마크 결론(2026-07-06): CLI 모드에서 동적 컨텍스트를 메시지 채널로 빼는 것은
오히려 cache_read 를 무너뜨려(50%→9%, 비용 +18%) 폐기했다. Claude Code 가
--system-prompt 전체를 캐시해 에이전트 루프 라운드마다 재사용하므로, CLI 는 원본
프롬프트를 그대로(경계 토큰만 제거) 넘긴다. cache_control 분리는 우리가 브레이크포인트를
제어하는 API 모드에서만 적용한다.
"""
import agent.orchestrator_stream as OS
from agent.claude_client import Message
from agent.prompt_cache import CACHE_BOUNDARY


class _FakeStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter([])

    def get_final_message(self):
        return Message(content=[], stop_reason="end_turn",
                       usage={"input_tokens": 1, "output_tokens": 1})


class _FakeMessages:
    def __init__(self):
        self.last_kw = None

    def stream(self, **kw):
        self.last_kw = kw
        return _FakeStream()


def _run_llm_call(monkeypatch, use_cli: bool):
    monkeypatch.setattr(OS, "_use_local_cli", lambda: use_cli)
    orch = OS.StreamOrchestrator(api_key="")
    fake = _FakeMessages()
    orch.client.messages = fake
    orch.state.add_user_message("체크박스 색을 바꿔줘")
    system = "정적 규칙(안전+빌드)" + CACHE_BOUNDARY + "## 현재 코드\n<큰 코드 블록>"
    list(orch._llm_call(system, tools=[]))
    return fake.last_kw


def test_cli_mode_passes_full_prompt_boundary_stripped(monkeypatch):
    kw = _run_llm_call(monkeypatch, use_cli=True)
    # CLI: 원본 전체(정적+동적)를 system 으로, 경계 토큰만 제거. 동적은 그대로 system 에 남는다
    # (Claude Code 가 통째로 캐시·재사용하는 게 실측상 더 효율적).
    assert isinstance(kw["system"], str)
    assert CACHE_BOUNDARY not in kw["system"]
    assert "정적 규칙(안전+빌드)" in kw["system"]
    assert "## 현재 코드" in kw["system"]
    # 메시지 채널엔 동적 컨텍스트를 주입하지 않는다(선두는 실제 유저 발화).
    assert kw["messages"][0]["role"] == "user"
    assert "체크박스 색" in str(kw["messages"][0]["content"])
    assert not any("## 현재 코드" in str(m.get("content")) for m in kw["messages"])


def test_api_mode_splits_static_and_dynamic_blocks(monkeypatch):
    kw = _run_llm_call(monkeypatch, use_cli=False)
    # API: system 을 cache_control 블록 리스트로 — 정적(캐시)+동적(무캐시) 2블록.
    sys_blocks = kw["system"]
    assert isinstance(sys_blocks, list) and len(sys_blocks) == 2
    assert "cache_control" in sys_blocks[0] and "cache_control" not in sys_blocks[1]
    assert "## 현재 코드" in sys_blocks[1]["text"]
    assert all(CACHE_BOUNDARY not in b["text"] for b in sys_blocks)
    # 동적 컨텍스트를 메시지로 옮기지 않는다.
    assert "체크박스 색" in str(kw["messages"][0]["content"])
