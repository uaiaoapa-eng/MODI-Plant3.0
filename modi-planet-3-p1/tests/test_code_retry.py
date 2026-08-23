"""구현 '툴콜 미호출/산출물 없음 → 침묵 종료' 방어 계층 테스트.

가짜 _llm_call(또는 가짜 client)을 주입해 실제 LLM 호출 없이 검증한다:
- _agent_loop_stream: 복구(salvage) → 폐기(void)+재시도 → 사과, 내부 메시지 정리(drop)
- blockly 산출물 판정(blockly_xml)과 nudge 툴명
- 도구는 썼지만 산출물 없이 라운드 소진된 턴의 재시도
- _llm_call: 도구 강제 시 텍스트 덤프의 채팅 유출 억제
- 직렬화 은닉·요약/텍스트 히스토리의 내부 메시지 필터·리마인더 위치
"""
import pytest

from agent.models import Phase, GeneratedFile

# 펜스 없는 장문 텍스트 덤프 (복구 불가 → 폐기+재시도 경로)
DUMP_NO_FENCE = "포트폴리오 앱을 만들게요.\n" + "여기에 코드를 설명으로 길게 풀어씁니다. " * 30

# 펜스 있는 덤프 (복구 가능 → salvage 경로)
FENCED_DUMP = (
    "포트폴리오 앱을 만들게요.\n\n"
    "**App.tsx**\n"
    "```tsx\n"
    "import React from 'react'\n"
    "export default function App() {\n"
    "  return <div className='p-4'>hello portfolio page content</div>\n"
    "}\n"
    "```\n\n"
    "**components/Header.tsx**\n"
    "```tsx\n"
    "export default function Header() {\n"
    "  return <header className='h-12 flex items-center'>Header Title</header>\n"
    "}\n"
    "```\n"
)

CODE_INPUT = {
    "file_path": "App.tsx",
    "code": "import React from 'react'\nexport default function App(){return <div>ok</div>}\n",
    "description": "테스트 코드",
}


def _make_orch(coding_type="react", phase=Phase.IMPLEMENT):
    try:
        from agent.orchestrator_stream import StreamOrchestrator
    except Exception as e:  # 의존성 미설치 환경
        pytest.skip(f"StreamOrchestrator import 불가: {e}")
    orch = StreamOrchestrator(api_key="", session_id="t")
    orch._coding_type = coding_type
    orch._current_mode = "quick"
    orch.state.coding_type = coding_type
    orch.state.project.phase = phase
    # chat_stream이 세팅하는 턴 카운터들
    orch._turn_tool_calls = 0
    orch._turn_tool_errors = 0
    orch._turn_build_attempts = 0
    orch._turn_build_errors = 0
    orch._turn_code_retries = 0
    orch._turn_code_salvaged = 0
    orch._turn_code_failed = False
    orch._turn_modify_failed = False
    orch._turn_modify_clarified = False
    orch._turn_artifact_rejected = False
    orch._turn_user_message_index = None
    orch._step_count = 0
    orch._turn_steps = []
    orch._turn_start_time = 0.0
    return orch


def _artifact_intent(intent, confidence=0.9, target=None, change_request="변경", needs_clarification=False):
    from agent.intent import ArtifactFollowupIntent
    return ArtifactFollowupIntent(
        intent=intent,
        confidence=confidence,
        target=tuple(target or []),
        change_request=change_request,
        needs_clarification=needs_clarification,
    )


def _inject_llm(orch, specs):
    """가짜 _llm_call 주입. spec 하나가 호출 1회의 응답을 기술한다.

    {"text": ...} 텍스트-only / {"code": input} generate_code 산출
    {"blockly": True} XML 산출 / {"tool": name} 코드 아닌 도구
    {"early_fail": True} rate-limit류 조기 실패 / {"cancel": True} 취소
    반환: 호출 기록 리스트(호출 시점의 tool_choice·메시지 스냅샷).
    """
    calls = []

    def fake(system_prompt, tools, compact=False, tool_choice=None, defer_text=False,
             max_tokens=None):
        spec = specs[len(calls)] if len(calls) < len(specs) else {"text": "여분 응답"}
        calls.append({
            "tool_choice": tool_choice,
            "defer_text": defer_text,
            "max_tokens": max_tokens,
            "tools": [t.get("name") for t in tools],
            "messages": [dict(m) for m in orch.state._messages],  # 호출 시점 스냅샷
        })
        orch._last_call_failed = False
        orch._last_had_tools = False
        orch._last_text_suppressed = False
        orch._last_text_deferred = False
        orch._last_model_text = ""
        if spec.get("cancel"):
            orch._cancelled = True
            yield {"type": "cancelled"}
            return False
        if spec.get("early_fail"):
            orch._last_call_failed = True
            orch._last_model_text = "요청 한도를 초과했어요. 잠시 후 다시 시도해주세요."
            yield {"type": "token", "text": "요청 한도를 초과했어요. 잠시 후 다시 시도해주세요."}
            return False
        if "text" in spec:
            orch._last_model_text = spec["text"]
            orch.state.add_assistant_message([{"type": "text", "text": spec["text"]}])
            # 실제 _llm_call 의미론 미러링: 도구 강제 하의 텍스트 응답은 채팅에서 억제됨
            orch._last_text_suppressed = tool_choice is not None
            orch._last_text_deferred = bool(defer_text and not orch._last_text_suppressed)
            return True
        if "code" in spec:
            from agent.tools import handle_tool_call
            orch.state.add_assistant_message([
                {"type": "tool_use", "id": "tu_code", "name": "generate_code", "input": spec["code"]}])
            handle_tool_call("generate_code", spec["code"], orch.state)
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_code", "content": "ok"}])
            orch._last_had_tools = True
            return False
        if spec.get("blockly"):
            orch.state.add_assistant_message([
                {"type": "tool_use", "id": "tu_bx", "name": "generate_blockly_xml", "input": {}}])
            orch.state.blockly_xml = "<xml><block/></xml>"
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_bx", "content": "ok"}])
            orch._last_had_tools = True
            return False
        if spec.get("tool"):
            orch.state.add_assistant_message([
                {"type": "tool_use", "id": "tu_t", "name": spec["tool"], "input": {}}])
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_t", "content": "태스크 생성됨"}])
            orch._last_had_tools = True
            return False
        if spec.get("mixed_dump"):
            # 실사고 패턴: 코드는 텍스트 덤프 + 싸구려 도구(update_diagram) 호출이 한 응답에
            orch._last_model_text = FENCED_DUMP
            orch._last_text_deferred = bool(defer_text)
            orch.state.add_assistant_message([
                {"type": "text", "text": FENCED_DUMP},
                {"type": "tool_use", "id": "tu_d", "name": "update_diagram",
                 "input": {"mermaid_code": "graph TD"}}])
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_d", "content": "다이어그램 업데이트"}])
            orch._last_had_tools = True
            return True
        if spec.get("real_tool"):
            # 실제 handle_tool_call로 도구를 실행하는 라운드 (phase 전환 가드 등 검증용)
            name, tin = spec["real_tool"]
            from agent.tools import handle_tool_call
            orch.state.add_assistant_message([
                {"type": "tool_use", "id": "tu_r", "name": name, "input": tin}])
            result = handle_tool_call(name, tin, orch.state)
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_r", "content": result}])
            orch._last_had_tools = True
            return False
        if spec.get("edit"):
            blocks = []
            if spec.get("with_text"):
                # 실사고 패턴: 완료 멘트 + 도구 호출이 한 응답에 (haiku 기본 동작)
                orch._last_model_text = spec["with_text"]
                orch._last_text_deferred = bool(defer_text)
                blocks.append({"type": "text", "text": spec["with_text"]})
            blocks.append({"type": "tool_use", "id": "tu_edit", "name": "edit_code",
                           "input": {"file_path": "App.tsx", "old_code": "x", "new_code": "y"}})
            orch.state.add_assistant_message(blocks)
            orch.state.generated_code_map["App.tsx"] = "edited code version"
            orch.state.mark_code_dirty()
            orch.state.add_tool_results([
                {"type": "tool_result", "tool_use_id": "tu_edit", "content": "수정 완료"}])
            if spec.get("tool_error"):
                # 같은 라운드의 다른 도구 호출이 실패한 경우(edit_code 불일치 등) 집계 모사
                orch._turn_tool_errors += 1
            orch._last_had_tools = True
            return bool(spec.get("with_text"))
        raise AssertionError(f"알 수 없는 spec: {spec}")

    orch._llm_call = fake
    return calls


def _run(orch, **kw):
    return list(orch._agent_loop_stream("sys", **kw))


def _tokens(events):
    return "".join(e.get("text", "") for e in events if e.get("type") == "token")


# ── 폐기+재시도 ──

def test_dump_then_retry_success():
    orch = _make_orch()
    orch.state.add_user_message("포트폴리오 만들어줘")
    calls = _inject_llm(orch, [{"text": DUMP_NO_FENCE}, {"code": CODE_INPUT}])
    events = _run(orch)

    assert len(calls) == 2
    # 산출물 도구를 '지목'해 강제 (any면 싸구려 도구로 때움 — 실사고)
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "generate_code"}
    assert "App.tsx" in orch.state.generated_code_map
    assert orch._turn_code_retries == 1
    assert orch._turn_code_failed is False
    # 재시도 콜의 컨텍스트에는 nudge(내부 user)가 있었다
    assert any(m.get("_internal") and m["role"] == "user" for m in calls[1]["messages"])
    # 턴이 끝나면 내부 잔재(마커·nudge)는 히스토리에서 사라진다
    assert not any(m.get("_internal") for m in orch.state._messages)
    assert "죄송해요" not in _tokens(events)


def test_retry_exhausted_apology_recorded_in_history():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    _inject_llm(orch, [{"text": DUMP_NO_FENCE}, {"text": DUMP_NO_FENCE}])
    events = _run(orch)

    assert "죄송해요" in _tokens(events)
    assert orch._turn_code_failed is True
    msgs = orch.state._messages
    assert not any(m.get("_internal") for m in msgs)
    # [user(요청), assistant(사과)] — 복원 채팅에서도 무응답이 아니라 사과가 보인다
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "죄송해요" in msgs[-1]["content"][0]["text"]


def test_early_fail_no_retry_no_apology():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    calls = _inject_llm(orch, [{"early_fail": True}])
    events = _run(orch)

    assert len(calls) == 1  # 조기 실패는 재시도해도 같은 이유로 실패 → 즉시 종료
    assert orch._turn_code_retries == 0
    assert "죄송해요" not in _tokens(events)  # 자체 안내만, 사과 중복 없음
    assert orch._turn_code_failed is True


def test_dump_then_early_fail_no_double_message_no_trailing_nudge():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    _inject_llm(orch, [{"text": DUMP_NO_FENCE}, {"early_fail": True}])
    events = _run(orch)

    assert "죄송해요" not in _tokens(events)  # rate-limit 안내 뒤 사과가 겹치지 않는다
    # 꼬리 nudge가 남지 않는다 → 다음 턴 add_user_message와 user-user 연속이 안 생김
    assert not any(m.get("_internal") for m in orch.state._messages)
    assert orch.state._messages[-1] == {"role": "user", "content": "만들어줘"}


def test_cancel_during_retry_cleans_internals():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    _inject_llm(orch, [{"text": DUMP_NO_FENCE}, {"cancel": True}])
    _run(orch)
    assert not any(m.get("_internal") for m in orch.state._messages)


# ── 복구(salvage) ──

def test_salvage_recovers_code_without_second_llm_call():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    calls = _inject_llm(orch, [{"text": FENCED_DUMP}])
    events = _run(orch)

    assert len(calls) == 1  # LLM 재호출 없이 복구
    assert set(orch.state.generated_code_map) == {"App.tsx", "components/Header.tsx"}
    assert orch._turn_code_salvaged == 2
    assert orch._turn_code_retries == 0
    assert orch._turn_code_failed is False
    # 덤프 메시지가 '원래 냈어야 할 형태'(tool_use)로 재작성되고 tool_result가 붙는다
    assert [m["role"] for m in orch.state._messages] == ["user", "assistant", "user"]
    asst_blocks = orch.state._messages[1]["content"]
    assert any(b.get("type") == "tool_use" and b["name"] == "generate_code" for b in asst_blocks)
    # 수만 토큰 덤프는 히스토리에서 사라진다
    assert FENCED_DUMP.splitlines()[2] not in str(asst_blocks)  # ```tsx 라인
    # 앞머리 안내 문장은 채팅으로 나간다
    assert "만들게요" in _tokens(events)


# ── 도구는 썼지만 산출물 없음 (예전 '침묵 종료' 재현 경로) ──

def test_tool_but_no_code_retries_then_succeeds():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    calls = _inject_llm(orch, [{"tool": "plan_tasks"}, {"code": CODE_INPUT}])
    _run(orch, max_loops=1)

    assert len(calls) == 2
    assert orch._turn_code_retries == 1
    assert "App.tsx" in orch.state.generated_code_map
    assert orch._turn_code_failed is False
    # nudge는 tool_result에 덧붙는다 (유저에게 안 보이는 채널)
    snap = calls[1]["messages"]
    tool_results = [m for m in snap if m["role"] == "user" and isinstance(m["content"], list)]
    assert any("generate_code" in str(m["content"]) for m in tool_results)


# ── blockly: 산출물 판정과 nudge 툴명 ──

def test_blockly_success_not_flagged_as_failure():
    orch = _make_orch(coding_type="blockly")
    orch.state.add_user_message("버튼 누르면 LED 켜줘")
    calls = _inject_llm(orch, [{"blockly": True}])
    events = _run(orch)

    assert len(calls) == 1
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "generate_blockly_xml"}
    assert orch._turn_code_failed is False  # 예전엔 코드맵 판정 탓에 매 턴 오탐
    assert "죄송해요" not in _tokens(events)


def test_blockly_dump_nudge_names_blockly_tool():
    orch = _make_orch(coding_type="blockly")
    orch.state.add_user_message("LED 켜줘")
    calls = _inject_llm(orch, [{"text": DUMP_NO_FENCE}, {"blockly": True}])
    _run(orch)

    assert len(calls) == 2
    nudges = [m for m in calls[1]["messages"] if m.get("_internal") and m["role"] == "user"]
    assert nudges and "generate_blockly_xml" in nudges[0]["content"]
    assert orch._turn_code_failed is False


VALID_HYBRID_APP = """
const { useEffect } = React;
function App() {
  const button = useButton(1);
  useEffect(() => {
    if (button.clicked) MODI.led(1).setColor(0, 180, 90);
  }, [button.clicked]);
  return <button>{button.clicked ? 'on' : 'off'}</button>;
}
export default App;
"""


def test_hybrid_emits_code_validated_before_post_agents(monkeypatch):
    """미리보기(code_validated)는 빌드 통과 직후, 후처리(설계/노트/주석) LLM 호출 전에
    나간다 — 후처리를 빌드와 한 풀로 묶으면 풀 join 때문에 미리보기가 늦어진다."""
    orch = _make_orch(coding_type="hybrid")
    orch.state.generated_code_map = {"App.tsx": VALID_HYBRID_APP}
    calls = []

    def fake_run_post_agents(run_build=False, run_post=False):
        calls.append((run_build, run_post))
        if run_build:
            yield {"type": "step", "step": "코드 검증", "action": "verify", "status": "success"}
            return True, []
        if run_post:
            yield {"type": "step", "step": "후처리", "action": "note", "status": "success"}
            return True, []
        return True, []

    monkeypatch.setattr(orch, "_run_post_agents", fake_run_post_agents)

    events = list(orch._post_impl_react("implement_request", "sys"))
    code_idx = next(i for i, event in enumerate(events) if event.get("type") == "code_validated")
    post_idx = next(i for i, event in enumerate(events) if event.get("step") == "후처리")

    assert code_idx < post_idx
    assert calls == [(True, False), (False, True)]


def test_hybrid_validation_failure_rolls_back_to_last_validated(monkeypatch):
    """검증 실패 산출물은 state에서 턴 시작 스냅샷으로 롤백된다 — done 억제 플래그만으로는
    다음 턴 done 이벤트·세션 저장/복원으로 실패물이 미리보기에 새어 나갔다."""
    orch = _make_orch(coding_type="hybrid")
    old_map = {"App.tsx": VALID_HYBRID_APP}
    old_modules = {"modules": [{"key": "network"}], "title": "이전 작품"}
    old_files = [GeneratedFile(path="App.tsx", description="이전 파일", language="tsx")]
    orch.state.generated_code_map = dict(old_map)
    orch.state.modi_modules = dict(old_modules)
    orch.state.project.generated_files = list(old_files)
    orch.state.app_type = "desktop"
    orch.state.begin_turn()
    orch.state.add_user_message("수정해줘")
    orch._turn_user_message_index = len(orch.state._messages) - 1

    # 이번 턴: 검증 불가능한 코드로 교체 (import + MODI 상호작용 없음)
    bad_code = "import React from 'react';\nfunction App() { return <div/>; }\nexport default App;"
    orch.state.add_assistant_message([
        {"type": "tool_use", "id": "bad_tool", "name": "generate_code",
         "input": {"file_path": "App.tsx", "code": bad_code}},
    ])
    orch.state.add_tool_results([
        {"type": "tool_result", "tool_use_id": "bad_tool", "content": "파일이 생성되었습니다"},
    ])
    orch.state.generated_code_map = {"App.tsx": bad_code}
    orch.state.project.generated_files.append(
        GeneratedFile(path="Broken.tsx", description="실패 파일", language="tsx"))
    orch.state.app_type = "mobile"
    orch.state.mark_code_dirty()
    monkeypatch.setattr(orch, "_fix_code", lambda errors, sp: iter(()))  # 수정 라운드 무효화

    events = list(orch._post_impl_react("modify_request", "sys"))

    assert orch.state.generated_code_map == old_map      # 실패물이 state에 남지 않음
    assert orch.state.modi_modules == old_modules        # 이전 준비물 문서 보존 (와이프 금지)
    assert orch.state.project.generated_files == old_files
    assert orch.state.app_type == "desktop"
    assert orch.state.code_changed_this_turn() is False
    assert orch._turn_artifact_rejected is True
    done = orch._done_event()
    assert done["generated_code"] == old_map             # 이후 어떤 done에도 실패물 없음
    assert "반영하지 않았어요" in _tokens(events)
    assert [m["role"] for m in orch.state._messages] == ["user", "assistant"]
    assert "반영하지 않았어요" in orch.state._messages[-1]["content"][0]["text"]
    assert "bad_tool" not in str(orch.state._messages)
    assert "Broken.tsx" not in str(orch.state._messages)


def test_blockly_validation_failure_rolls_back_xml(monkeypatch):
    orch = _make_orch(coding_type="blockly")
    old_xml = "<xml>이전에 검증 통과한 XML</xml>"
    old_flowchart = [{"type": "start", "label": "이전 시작"}]
    old_code_langs = {"python": "old python"}
    old_grid = [["network"], ["led"]]
    old_rotations = {"led": 90}
    old_attachments = {"motor_a": "wheel"}
    old_modules = {"modules": [{"key": "network"}]}
    orch.state.blockly_xml = old_xml
    orch.state.blockly_flowchart = list(old_flowchart)
    orch.state.blockly_detail = "이전 설명"
    orch.state.blockly_code_langs = dict(old_code_langs)
    orch.state.modi_grid = [list(row) for row in old_grid]
    orch.state.modi_rotations = dict(old_rotations)
    orch.state.modi_attachments = dict(old_attachments)
    orch.state.modi_modules = dict(old_modules)
    orch.state.begin_turn()

    orch.state.blockly_xml = "<xml><block type='없는_블록'/></xml>"
    orch.state.blockly_flowchart = [{"type": "action", "label": "실패 산출물"}]
    orch.state.blockly_detail = "실패 설명"
    orch.state.blockly_code_langs = {"python": "bad"}
    orch.state.modi_grid = [["network"], ["speaker"]]
    orch.state.modi_rotations = {"speaker": 180}
    orch.state.modi_attachments = {"motor_b": "wheel"}
    monkeypatch.setattr(orch, "_fix_blockly", lambda errors, sp: iter(()))

    events = list(orch._post_impl_blockly("sys"))

    assert orch.state.blockly_xml == old_xml
    assert orch.state.blockly_flowchart == old_flowchart
    assert orch.state.blockly_detail == "이전 설명"
    assert orch.state.blockly_code_langs == old_code_langs
    assert orch.state.modi_grid == old_grid
    assert orch.state.modi_rotations == old_rotations
    assert orch.state.modi_attachments == old_attachments
    assert orch.state.modi_modules == old_modules
    done = orch._done_event()
    assert done["blockly_xml"] == old_xml
    assert done["blockly_flowchart"] == old_flowchart
    assert done["blockly_code_langs"] == old_code_langs
    assert not any(e.get("type") == "blockly_ready" for e in events)


def test_hybrid_legacy_multifile_modify_not_blocked_by_single_file_contract(monkeypatch):
    """단일 파일 계약 도입 전에 여러 파일로 저장된 hybrid 프로젝트의 수정 턴은
    구조 계약을 소급받지 않는다 — 소급하면 모든 수정이 검증 실패가 된다."""
    orch = _make_orch(coding_type="hybrid")
    legacy_map = {
        "App.tsx": VALID_HYBRID_APP,
        "components/Hud.tsx": "export default function Hud() { return <div>HUD</div>; }",
    }
    orch.state.generated_code_map = dict(legacy_map)
    orch.state.begin_turn()
    orch.state.mark_code_dirty()

    def fake_run_post_agents(run_build=False, run_post=False):
        yield {"type": "step", "step": "코드 검증", "action": "verify", "status": "success"}
        return True, []

    monkeypatch.setattr(orch, "_run_post_agents", fake_run_post_agents)

    events = list(orch._post_impl_react("modify_request", "sys"))

    assert any(e.get("type") == "code_validated" for e in events)
    assert orch.state.generated_code_map == legacy_map


# ── 설계 phase 무회귀 ──

def test_design_text_only_unchanged():
    orch = _make_orch(phase=Phase.DESIGN)
    orch.state.add_user_message("퀴즈 앱 만들고 싶어")
    calls = _inject_llm(orch, [{"text": "좋아요! 어떤 과목 퀴즈인가요?"}])
    _run(orch, max_loops=3)

    assert len(calls) == 1
    assert calls[0]["tool_choice"] is None  # 설계엔 강제 없음
    last = orch.state._messages[-1]
    assert last["role"] == "assistant" and "퀴즈" in last["content"][0]["text"]  # 폐기 안 됨


# ── _llm_call: 덤프 flush 억제 ──

def test_llm_call_suppresses_dump_only_when_tool_forced():
    from types import SimpleNamespace
    from agent.claude_client import (
        TextBlock, Message, _ContentBlockStart, _ContentBlockDelta, _TextDelta,
    )

    class FakeStream:
        def __init__(self, text):
            self._t = text
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            yield _ContentBlockStart(content_block=TextBlock(text=""))
            yield _ContentBlockDelta(delta=_TextDelta(text=self._t))
        def get_final_message(self):
            return Message(content=[TextBlock(text=self._t)], stop_reason="end_turn")

    tools = [{"name": "generate_code", "description": "d", "input_schema": {}}]

    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    orch.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream("코드 덤프 텍스트")))
    forced = list(orch._llm_call("sys", tools, tool_choice={"type": "any"}))
    assert not any(e.get("type") == "token" for e in forced)  # 실패 덤프는 채팅에 안 나감

    orch2 = _make_orch()
    orch2.state.add_user_message("설명해줘")
    orch2.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream("일반 답변")))
    free = list(orch2._llm_call("sys", tools, tool_choice=None))
    assert any(e.get("type") == "token" for e in free)  # 강제 아닐 땐 그대로 방출


def test_llm_call_quota_limit_returns_user_message_without_retry():
    from types import SimpleNamespace

    class QuotaStream:
        def __enter__(self):
            raise RuntimeError("Claude CLI 오류: You've hit your session limit · resets 1:40pm (Asia/Seoul)")
        def __exit__(self, *a):
            return False

    calls = {"n": 0}

    def stream(**kw):
        calls["n"] += 1
        return QuotaStream()

    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    orch.client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    events = list(orch._llm_call("sys", [{"name": "generate_code", "description": "d", "input_schema": {}}]))

    assert calls["n"] == 1
    assert "Claude 사용 한도" in _tokens(events)
    assert "1:40pm" in _tokens(events)
    assert orch._last_call_failed is True


# ── 누수 방지: 직렬화·요약·텍스트 히스토리·리마인더 ──

def test_serialize_skips_internal_messages():
    from server import _serialize_messages
    msgs = [
        {"role": "user", "content": "만들어줘"},
        {"role": "assistant", "content": [{"type": "text", "text": "(무효 처리됨)"}], "_internal": True},
        {"role": "user", "content": "재시도 지시", "_internal": True},
        {"role": "assistant", "content": [{"type": "text", "text": "다 만들었어요"}]},
    ]
    out = _serialize_messages(msgs)
    assert [m["content"] for m in out] == ["만들어줘", "다 만들었어요"]


def test_extract_summary_and_text_history_skip_internal():
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    orch.state.add_assistant_message([{"type": "text", "text": "진짜 요약 문장입니다."}])
    orch.state._messages.append(
        {"role": "assistant", "content": [{"type": "text", "text": "(무효 처리됨: 폐기)"}], "_internal": True})
    orch._turn_steps = [{"type": "agent_step", "step": 1, "description": "x", "action": "y", "status": "success"}]
    assert "무효" not in orch._extract_summary()
    assert "무효" not in orch.state.get_text_history()


def test_llm_message_views_strip_internal_metadata_but_keep_content():
    orch = _make_orch()
    orch.state._messages = [
        {"role": "user", "content": "만들어줘", "_internal": True},
        {"role": "assistant", "content": [
            {"type": "text", "text": "(무효 처리됨)"},
            {"type": "tool_use", "id": "t1", "name": "generate_code", "input": {"file_path": "App.tsx"}},
        ], "_internal": True, "_agent_steps": [{"step": 1}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "_internal": True},
        ]},
    ]

    api_messages = orch.state.get_api_messages()
    compact = orch.state.get_compact_messages()

    assert "만들어줘" in str(api_messages)       # 내부 nudge/마커 내용은 LLM 컨텍스트에 유지
    assert "_internal" not in str(api_messages)  # SDK로 보내는 dict에는 서버 전용 키 제거
    assert "_agent_steps" not in str(api_messages)
    assert "_internal" not in str(compact)
    assert api_messages[1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "generate_code",
        "input": {"file_path": "App.tsx"},
    }


def test_reminder_allows_one_sentence_and_sits_before_assistant_cue():
    from agent.claude_client import _LocalMessages
    prompt = _LocalMessages()._build_prompt(
        [{"role": "user", "content": "만들어줘"}],
        tools=[{"name": "generate_code", "description": "d", "input_schema": {}}],
        tool_choice={"type": "any"},
    )
    idx = prompt.index("[SYSTEM REMINDER]")
    assert prompt.index("만들어줘") < idx < prompt.rindex("[ASSISTANT]")  # 대화 뒤·응답 큐 직전
    assert "ONE short sentence" in prompt  # 시스템 프롬프트의 "한 문장 안내"와 정합
    assert "Begin your response with" not in prompt  # 상충 지시 제거됨


# ── salvage 파서 단위 ──

def test_extract_code_files_paths_dedup_and_fallback():
    from agent.salvage import extract_code_files

    files = dict(extract_code_files(FENCED_DUMP))
    assert set(files) == {"App.tsx", "components/Header.tsx"}
    assert "hello portfolio" in files["App.tsx"]

    # 같은 경로 중복 → 마지막(수정본)이 이긴다
    dup = FENCED_DUMP + "\n수정된 App.tsx:\n```tsx\n" + \
        "import React from 'react'\nexport default function App(){return <b>v2 final version</b>}\n" + "```\n"
    assert "v2 final" in dict(extract_code_files(dup))["App.tsx"]

    # 경로 없는 단일 컴포넌트 → App.tsx 폴백
    single = ("설명입니다\n```tsx\nexport default function App(){\n"
              "  return <div>abcdef ghijkl mnopqr stuvwx</div>\n}\n// filler line\n// filler line\n```")
    assert list(dict(extract_code_files(single))) == ["App.tsx"]

    # App 엔트리 없는 복수 펜스 → 복구 포기 (재시도가 낫다)
    no_app = FENCED_DUMP.replace("**App.tsx**", "**components/Foo.tsx**")
    assert extract_code_files(no_app) == []

    # 짧은 조각·bash 펜스는 파일이 아니다
    assert extract_code_files("```bash\nnpm install\n```") == []
    assert extract_code_files("펜스 없음 " * 50) == []


# ── 턴 계약(TurnContract): 수정 턴 ──

def _setup_modify_orch():
    """코드가 이미 있는 세션 + 새 턴 시작(수정 턴) 셋업."""
    orch = _make_orch()
    from agent.tools import handle_tool_call
    handle_tool_call("generate_code", CODE_INPUT, orch.state)
    orch.state.begin_turn()  # 새 턴 — code_changed_this_turn()이 False로 리셋
    return orch


def test_modify_promise_only_retries_with_force_then_edits():
    """관측된 실사고: 수정 요청에 '그럼 바로 수정할 수 있어!'라고 말만 하고 0 tool로 종료."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("버튼 색 바꿔줘")
    calls = _inject_llm(orch, [{"text": "그럼 바로 수정할 수 있어!"}, {"edit": True}])
    contract = orch._implement_contract("modify_request")
    list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    # 약속(1) → 강제 재시도로 edit(2) → 이행+도구 성공이면 즉시 완결(마무리 라운드 없음 —
    # 소형 모델이 마무리 라운드마다 전체 파일을 재생성해 비용이 배로 들던 실사고 방지)
    assert len(calls) == 2
    assert calls[0]["tool_choice"] is None             # 첫 시도는 강제 없음(답변일 수 있음)
    assert calls[1]["tool_choice"] == {"type": "any"}  # 재시도부터 강제
    assert orch.state.code_changed_this_turn()
    assert orch._turn_code_retries == 1
    assert orch._turn_modify_failed is False
    # 모델의 첫 응답(약속)은 유저에게 보류됐다가 실제 수정 전에는 저장되지 않는다.
    texts = [str(m.get("content")) for m in orch.state._messages if m["role"] == "assistant"]
    assert not any("바로 수정할 수 있어" in t for t in texts)
    assert not any(m.get("_internal") for m in orch.state._messages)


def test_modify_action_offer_question_retries_instead_of_escaping_contract():
    """진행 제안 질문은 다음 턴 라우팅 신호일 뿐, 현재 수정 계약을 완결하면 안 된다."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("에셋들이 전혀 없는데")
    calls = _inject_llm(orch, [
        {"text": "지금 바로 그려 넣어서 만들어 줄게! 다음으로 넘어갈까?"},
        {"edit": True},
    ])
    contract = orch._implement_contract("modify_request")
    list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert len(calls) == 2
    assert calls[1]["tool_choice"] == {"type": "any"}
    assert orch.state.code_changed_this_turn()
    assert orch._turn_code_retries == 1
    assert orch._turn_modify_clarified is False
    assert orch._turn_modify_failed is False


def test_modify_fulfilled_round_stops_loop_without_wrapup_regeneration():
    """실사고(Langfuse): 수정 1턴에 haiku가 마무리 라운드마다 전체 App.tsx를 재생성 —
    '완벽해!' 멘트와 함께 구현 3회, $0.17·2m11s. 계약이 이행되고 이번 라운드 도구가
    전부 성공했으면 남은 라운드 예산(에러 복구용)을 쓰지 않고 즉시 완결해야 한다."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("게임 화면 키워줘")
    calls = _inject_llm(orch, [
        {"edit": True, "with_text": "게임 화면을 900x800으로 키웠어."},
        {"edit": True, "with_text": "완벽해! 더 키웠어."},  # 호출되면 안 됨(중복 재생성)
    ])
    contract = orch._implement_contract("modify_request")
    events = list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert len(calls) == 1                       # 이행 라운드에서 즉시 완결
    assert orch._turn_modify_failed is False
    # 계약 미이행 상태에서 보류(defer)됐던 완료 설명은 완결 시점에 방출된다
    assert "900x800으로 키웠어" in _tokens(events)


def test_modify_round_with_tool_error_keeps_recovery_budget():
    """계약이 이행됐어도 이번 라운드에 도구 에러(edit_code 불일치 등)가 있으면
    복구 라운드를 이어간다 — 즉시 완결은 '전부 성공' 라운드에만 적용."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("버튼 두 개 고쳐줘")
    calls = _inject_llm(orch, [
        {"edit": True, "tool_error": True},  # 일부 파일 성공 + 일부 에러
        {"edit": True},                      # 복구 라운드 (전부 성공 → 완결)
    ])
    contract = orch._implement_contract("modify_request")
    list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert len(calls) == 2
    assert orch._turn_modify_failed is False


def test_modify_no_change_honest_notice_and_false_claim_dropped():
    """실사고(이미지): "완벽하게 업그레이드 완료!"라고 말만 하고 무변경 — 거짓 주장만 남고
    아무 안내도 없던 케이스. 이제 강제 재시도의 미표시 텍스트는 폐기되고 정직한 안내가 남는다."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("사진 실제 이미지로 바꿔줘")
    _inject_llm(orch, [{"text": "실제 사진으로 업그레이드했어!"},
                       {"text": "실제 사진으로 완벽하게 업그레이드 완료!"}])
    contract = orch._implement_contract("modify_request")
    events = list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert orch._turn_modify_failed is True
    assert "아직 적용되지 않았어요" in _tokens(events)  # 침묵 대신 정직한 안내
    msgs = orch.state._messages
    texts = [str(m.get("content")) for m in msgs if m["role"] == "assistant"]
    assert not any("업그레이드했어" in t for t in texts)     # 보류된 거짓 약속은 저장하지 않음
    assert not any("완벽하게" in t for t in texts)          # 강제 재시도의 거짓 주장은 폐기(라이브에서도 억제됨)
    assert "적용되지 않았어요" in str(msgs[-1]["content"])  # 안내가 히스토리에도 기록 → 복원 일치
    assert not any(m.get("_internal") for m in msgs)


def test_modify_clarifying_question_is_normal_turn_not_retry():
    """실사고: 모호한 피드백에 모델이 요구사항을 물었는데 수정 실패 계약이 재시도 비용을 태움."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("디자인이 좀 별로야. 텍스트 에디터 느낌도 많이 나야 하고 기능이 너무 없어")
    question = (
        "텍스트 에디터 느낌의 블록 기반 문서 편집기로 재설계해볼게요.\n\n"
        "구체적으로 어떤 기능들이 가장 필요한지 말씀해 주시면, 바로 만들어드릴게요. "
        "예를 들어 굵게, 제목 스타일, 리스트, 코드 블록 중 어떤 걸 먼저 만들까요?"
    )
    calls = _inject_llm(orch, [{"text": question}])
    contract = orch._implement_contract("implement_request")
    list(orch._agent_loop_stream("sys", max_loops=1, contract=contract))

    assert len(calls) == 1
    assert orch._turn_code_retries == 0
    assert orch._turn_modify_failed is False
    assert orch.state.code_changed_this_turn() is False
    assert "어떤 기능" in str(orch.state._messages[-1]["content"])


def test_modify_false_done_question_still_fails_contract():
    """질문 예외가 '수정 완료! 마음에 들어?' 같은 거짓 완료 주장을 면제하면 안 된다."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("사진 실제 이미지로 바꿔줘")
    calls = _inject_llm(orch, [
        {"text": "실제 사진으로 업그레이드 완료했어요! 마음에 드나요?"},
        {"text": "실제 사진으로 완벽하게 업그레이드 완료!"},
    ])
    contract = orch._implement_contract("modify_request")
    list(orch._agent_loop_stream("sys", max_loops=1, contract=contract))

    assert len(calls) == 2
    assert orch._turn_code_retries == 1
    assert orch._turn_modify_failed is True


def test_modify_dump_salvaged_with_known_paths():
    orch = _setup_modify_orch()
    orch.state.add_user_message("헤더 고쳐줘")
    dump = ("헤더를 고칠게요.\n\n**App.tsx**\n```tsx\n"
            "import React from 'react'\n"
            "export default function App(){return <div>fixed header version</div>}\n```")
    calls = _inject_llm(orch, [{"text": dump}])
    contract = orch._implement_contract("modify_request")
    list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert len(calls) == 1  # LLM 재호출 없이 복구
    assert "fixed header" in orch.state.generated_code_map["App.tsx"]
    assert orch.state.code_changed_this_turn()
    assert orch._turn_modify_failed is False


def test_contract_factory_by_state_and_intent():
    # 산출물 있음: modify/implement만 '변경' 계약, question·None(기본값)은 대화 턴
    orch = _setup_modify_orch()
    assert orch._implement_contract("question") is None
    assert orch._implement_contract(None) is None
    modify = orch._implement_contract("modify_request")
    # 수정 계약: 재시도부터 강제, 실패 안내는 조건부 문구(질문 오분류에도 어색하지 않게)
    assert modify is not None and modify.force_from_start is False
    assert modify.question_exempt is True
    assert "적용되지 않았어요" in modify.apology and "죄송해요" not in modify.apology
    assert orch._implement_contract("implement_request") is not None  # quick 모드 커버
    # 산출물 없음: 생성 intent일 때만 '생성' 계약(강제+사과). 질문/잡담은 대화 턴.
    fresh = _make_orch()
    assert fresh._implement_contract("question") is None
    produce = fresh._implement_contract("implement_request")
    assert produce is not None and produce.force_from_start is True and produce.question_exempt is False and produce.apology
    # 설계 phase: 계약 없음
    assert _make_orch(phase=Phase.DESIGN)._implement_contract("modify_request") is None


def test_quick_existing_artifact_uses_router_instead_of_forcing_implement():
    orch = _setup_modify_orch()

    assert orch._classify_turn_intent("이 앱은 뭐야?", mode="quick") == "question"
    assert orch._classify_turn_intent("버튼 색 바꿔줘", mode="quick") == "modify_request"

    fresh = _make_orch()
    fresh.state.project.phase = Phase.DESIGN
    assert fresh._classify_turn_intent("이 앱은 뭐야?", mode="quick") == "question"


def test_quick_initial_ambiguous_defaults_to_implement_without_intent_router():
    orch = _make_orch(phase=Phase.DESIGN)

    assert orch._classify_turn_intent("텍스트 에디터 느낌의 블록 문서 편집기", mode="quick") == "implement_request"


def test_quick_initial_question_does_not_create_generation_contract():
    orch = _make_orch(phase=Phase.DESIGN)
    intent = orch._classify_turn_intent("뭐 할 수 있어?", mode="quick")

    assert intent == "question"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_implement_fallback_does_not_default_to_code_action():
    from agent.router import Router

    router = Router()
    assert router.classify("고마워", Phase.IMPLEMENT) == "chat"
    assert router.classify("음...", Phase.IMPLEMENT) == "chat"
    assert router.classify("아하", Phase.IMPLEMENT) == "chat"


def test_ambiguous_existing_artifact_uses_classifier_gate_for_clarification(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.62, target=[], change_request="", needs_clarification=True),
    )

    intent = orch._classify_turn_intent("디자인이 좀 별로야", mode="quick")

    assert intent == "clarify_request"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_concrete_artifact_feedback_uses_classifier_gate_for_modify(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.88, target=["editor style"], change_request="텍스트 에디터 느낌 강화"),
    )

    assert orch._classify_turn_intent("텍스트 에디터 느낌이 안 나", mode="quick") == "modify_request"


def test_artifact_defect_report_maps_to_modify_without_llm():
    orch = _setup_modify_orch()

    assert orch._classify_turn_intent("배경만 있고 우주선은 잘 안 보이는 것 같아", mode="quick") == "modify_request"
    assert orch._classify_turn_intent("버튼이 안 돼", mode="quick") == "modify_request"
    assert orch._classify_turn_intent("남색만 있는데", mode="quick") == "modify_request"


def test_artifact_defect_report_does_not_catch_positive_or_plain_only_phrases(monkeypatch):
    from agent.intent import looks_like_artifact_defect_report

    assert not looks_like_artifact_defect_report("이제 에러 없어!")
    assert not looks_like_artifact_defect_report("고마워 이제 버그 없네")
    assert not looks_like_artifact_defect_report("궁금한 게 하나만 있어")

    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent("chat", confidence=0.91, change_request=""),
    )

    assert _setup_modify_orch()._classify_turn_intent("이제 에러 없어!", mode="quick") == "chat"
    assert _setup_modify_orch()._classify_turn_intent("고마워 이제 버그 없네", mode="quick") == "chat"
    assert _setup_modify_orch()._classify_turn_intent("궁금한 게 하나만 있어", mode="quick") == "chat"


def test_positive_no_problem_feedback_does_not_modify(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "chat", confidence=0.91, change_request=""),
    )
    intent = orch._classify_turn_intent("문제 없어", mode="quick")

    assert intent == "chat"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_question_shaped_concrete_change_can_modify_via_classifier_gate(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.86, target=["button"], change_request="버튼 크기 확대"),
    )

    assert orch._classify_turn_intent("버튼을 좀 더 크게 할 수 있어?", mode="quick") == "modify_request"


def test_positive_word_inside_change_request_does_not_become_chat(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.86, target=["visual polish"], change_request="더 좋아 보이게 개선"),
    )

    assert orch._classify_turn_intent("더 좋아 보이게 할 수 있어?", mode="quick") == "modify_request"


def test_question_shaped_vague_change_stays_no_code(monkeypatch):
    orch = _setup_modify_orch()
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.66, target=[], change_request="", needs_clarification=True),
    )
    intent = orch._classify_turn_intent("버튼을 좀 더 크게 할 수 있어?", mode="quick")

    assert intent == "clarify_request"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_question_shaped_followup_after_requirement_question_still_uses_classifier_gate(monkeypatch):
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "어떤 기능들이 가장 필요한지 알려줄 수 있을까요?"}
    ])
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "modify_request", confidence=0.66, target=[], change_request="", needs_clarification=True),
    )

    intent = orch._classify_turn_intent("버튼을 좀 더 크게 할 수 있어?", mode="quick")

    assert intent == "clarify_request"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_artifact_followup_policy_requires_confidence_and_detail():
    from agent.intent import route_artifact_followup_decision

    assert route_artifact_followup_decision(
        _artifact_intent("modify_request", confidence=0.74, target=["button"], change_request="크게")
    ) == "clarify_request"
    assert route_artifact_followup_decision(
        _artifact_intent("modify_request", confidence=0.9, target=[], change_request="")
    ) == "clarify_request"
    assert route_artifact_followup_decision(
        _artifact_intent("modify_request", confidence=0.9, target=["button"], change_request="크게")
    ) == "modify_request"


def test_artifact_followup_json_normalization():
    from agent.intent import artifact_followup_intent_from_json

    decision = artifact_followup_intent_from_json({
        "intent": "modify_request",
        "confidence": "1.4",
        "target": "button",
        "change_request": "크게",
        "needs_clarification": "false",
    })

    assert decision.confidence == 1.0
    assert decision.target == ("button",)
    assert decision.intent == "modify_request"
    assert decision.needs_clarification is False


def test_short_confirmation_after_action_suggestion_maps_to_modify():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "다음으로 넘어갈까?"}
    ])

    assert orch._classify_turn_intent("ㅇㅇ", mode="quick") == "modify_request"


def test_delay_request_after_action_suggestion_stays_no_code():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "준비됐어?"}
    ])

    intent = orch._classify_turn_intent("아직 하지마", mode="quick")

    assert intent == "chat"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_urgency_after_promised_action_maps_to_modify():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "멋지게 그려 줄게! 지금 바로 시작!"}
    ])

    assert orch._classify_turn_intent("빨리 만들어봐", mode="quick") == "modify_request"


def test_short_confirmation_after_requirement_question_stays_no_code():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "어떤 기능들이 가장 필요한지 알려줄 수 있을까요?"}
    ])

    intent = orch._classify_turn_intent("응", mode="quick")

    assert intent == "chat"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_explicit_question_after_assistant_question_stays_question():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "어떤 부분을 설명해 줄까요?"}
    ])

    intent = orch._classify_turn_intent("로직 설명해줘", mode="quick")

    assert intent == "question"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_clarification_answer_maps_to_modify_request_without_intent_router():
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "어떤 기능들이 가장 필요한지 알려줄 수 있을까요?"}
    ])

    assert orch._classify_turn_intent("굵게, 리스트, 코드블록", mode="quick") == "modify_request"


def test_clarification_non_answer_stays_no_code(monkeypatch):
    orch = _setup_modify_orch()
    orch.state.add_assistant_message([
        {"type": "text", "text": "어떤 기능들이 가장 필요한지 알려줄 수 있을까요?"}
    ])
    monkeypatch.setattr(
        "agent.intent.classify_artifact_followup_intent",
        lambda client, model, payload, known_intent, phase: _artifact_intent(
            "clarify_request", confidence=0.9, needs_clarification=True),
    )

    intent = orch._classify_turn_intent("몰라", mode="quick")

    assert intent == "clarify_request"
    assert orch._tools_override_for_intent(intent) == []
    assert orch._implement_contract(intent) is None


def test_non_code_intent_gets_no_tools_override():
    orch = _setup_modify_orch()

    assert orch._tools_override_for_intent("chat") == []
    assert orch._tools_override_for_intent("question") == []
    assert orch._tools_override_for_intent("clarify_request") == []
    assert orch._tools_override_for_intent("modify_request") is None


def test_empty_tools_override_is_preserved():
    orch = _setup_modify_orch()
    calls = _inject_llm(orch, [{"text": "고마워요!"}])

    list(orch._agent_loop_stream("sys", max_loops=1, tools_override=[], contract=None))

    assert calls[0]["tools"] == []
    assert calls[0]["tool_choice"] is None


# ── 컨텍스트/판정 근본 수정 ──

def test_compact_history_keeps_tool_trace():
    """compact가 도구 흔적을 전부 지우면 모델이 '말만 하는' 채팅 관성에 빠진다 — 한 줄 요약 보존."""
    orch = _make_orch()
    orch.state.add_user_message("만들어줘")
    orch.state.add_assistant_message([
        {"type": "text", "text": "포트폴리오를 만들게요."},
        {"type": "tool_use", "id": "t1", "name": "generate_code", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "generate_code", "input": {}},
    ])
    compact = orch.state.get_compact_messages()
    assert "generate_code×2" in compact[-1]["content"]
    assert "포트폴리오를 만들게요." in compact[-1]["content"]


def test_is_tool_error_recognizes_오류_prefix():
    from agent.orchestrator_stream import _is_tool_error
    # edit_code 명중 실패가 success로 위장되던 기존 버그
    assert _is_tool_error("오류: 'App.tsx'에서 교체할 코드를 찾을 수 없습니다. 정확한 코드를 지정해주세요.")
    assert _is_tool_error("오류: file_path와 code는 필수입니다. 다시 시도해주세요.")
    assert not _is_tool_error("파일이 생성되었습니다: App.tsx\n설명: 메인 컴포넌트")


def test_salvage_known_paths_unit():
    from agent.salvage import extract_code_files
    partial = FENCED_DUMP.replace("**App.tsx**", "**components/Foo.tsx**")  # App 엔트리 없음
    assert extract_code_files(partial) == []  # 신규 생성: 엔트리 필수
    files = dict(extract_code_files(partial, known_paths={"components/Header.tsx"}))
    assert "components/Header.tsx" in files   # 수정: 기존 경로와 겹치면 신뢰
    assert extract_code_files(partial, known_paths={"pages/Home.tsx"}) == []  # 겹침 없음 → 불신


# ── 도구 경계: 입력 오류는 턴을 못 죽이고, phase는 못 이탈한다 (Langfuse 실사고 2건) ──

def test_tool_input_errors_do_not_crash_turn():
    """실사고: update_diagram에 mermaid_code 누락 → KeyError로 채팅 턴 전체 ERROR."""
    from agent.tools import handle_tool_call
    from agent.orchestrator_stream import _is_tool_error
    orch = _make_orch()
    res = handle_tool_call("update_diagram", {}, orch.state)  # 필수 키 누락
    assert res.startswith("오류")          # 예외 대신 오류 결과 — 모델이 보고 자가수정
    assert _is_tool_error(res)             # 에러로 집계됨
    # 임의 예외도 밖으로 안 샌다
    res2 = handle_tool_call("update_diagram", {"mermaid_code": 123}, orch.state)
    assert isinstance(res2, str)


def test_transition_phase_cannot_leave_implement():
    """실사고: quick 구현 턴에서 haiku가 transition_phase 헛호출 → phase 이탈 → 코드 없이 종료."""
    from agent.tools import handle_tool_call
    orch = _make_orch()  # IMPLEMENT
    handle_tool_call("transition_phase", {"target_phase": "design", "reason": "r"}, orch.state)
    assert orch.state.project.phase == Phase.IMPLEMENT  # 구현에서 못 나감
    res = handle_tool_call("transition_phase", {"target_phase": "implement", "reason": "r"}, orch.state)
    assert orch.state.project.phase == Phase.IMPLEMENT and "이미" in res  # 동일 phase 무시
    # 정상 용도(설계→구현, 검증→구현)는 그대로
    d = _make_orch(phase=Phase.DESIGN)
    handle_tool_call("transition_phase", {"target_phase": "implement", "reason": "r"}, d.state)
    assert d.state.project.phase == Phase.IMPLEMENT
    v = _make_orch(phase=Phase.VERIFY)
    handle_tool_call("transition_phase", {"target_phase": "implement", "reason": "r"}, v.state)
    assert v.state.project.phase == Phase.IMPLEMENT


def test_spurious_transition_no_longer_ends_turn_without_code():
    """이미지 #4 재현: transition_phase 헛호출 라운드 → 가드로 phase 유지 → 재시도로 코드 생성."""
    orch = _make_orch()
    orch.state.add_user_message("스포티파이 만들어줘")
    calls = _inject_llm(orch, [
        {"real_tool": ("transition_phase", {"target_phase": "design", "reason": "설계부터 하죠"})},
        {"code": CODE_INPUT},
    ])
    _run(orch, max_loops=1)

    assert orch.state.project.phase == Phase.IMPLEMENT  # phase 이탈 없음
    assert len(calls) == 2                              # 라운드 소진 재시도가 밀어붙임
    assert "App.tsx" in orch.state.generated_code_map
    assert orch._turn_code_failed is False              # 예전엔 no_code ERROR로 침묵 종료


def test_mixed_round_cheap_tool_plus_dump_salvaged():
    """실사고(트레이스): tool_choice 강제를 update_diagram 하나로 때우고 코드는 텍스트로 덤프
    → 도구를 썼으니 salvage가 안 돌고, 재시도 노트만 붙은 채 코드 없이 종료되던 경로."""
    orch = _make_orch()
    orch.state.add_user_message("스포티파이 만들어줘")
    calls = _inject_llm(orch, [{"mixed_dump": True}])
    _run(orch, max_loops=1)

    assert len(calls) == 1  # LLM 재호출 없이 혼합 라운드에서 바로 복구
    assert set(orch.state.generated_code_map) == {"App.tsx", "components/Header.tsx"}
    assert orch._turn_code_failed is False
    assert orch._turn_code_salvaged == 2
    # 기존 도구(update_diagram) 페어링은 보존되고, 복구 호출은 새 assistant로 덧붙는다
    roles = [m["role"] for m in orch.state._messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    salvaged_asst = orch.state._messages[3]["content"]
    assert all(b["type"] == "tool_use" and b["name"] == "generate_code" for b in salvaged_asst)


# ── 수정 루프 컨텍스트 다이어트 ──

def test_fix_system_prompt_only_includes_error_files():
    from agent.tools import handle_tool_call
    orch = _make_orch()
    handle_tool_call("generate_code", CODE_INPUT, orch.state)  # App.tsx
    handle_tool_call("generate_code", {
        "file_path": "components/Nav.tsx",
        "code": "export default function Nav(){return <nav>UNIQUE_NAV_CODE</nav>}",
        "description": "네비"}, orch.state)

    errors = ["[components/Nav.tsx] Undefined component: '<X>'이 사용되었지만 import 되지 않았습니다."]
    prompt = orch._fix_system_prompt(errors, "FULL_FALLBACK")
    assert "UNIQUE_NAV_CODE" in prompt              # 에러 난 파일 코드는 포함
    assert CODE_INPUT["code"] not in prompt         # 무관한 파일 코드는 제외
    assert "- App.tsx" in prompt                    # 파일 목록엔 이름만
    # 파일 특정 불가 → 전체 컨텍스트 폴백
    assert orch._fix_system_prompt(["알 수 없는 에러"], "FULL_FALLBACK") == "FULL_FALLBACK"


def test_fix_code_uses_compact_and_cleans_internal_note():
    from agent.tools import handle_tool_call
    orch = _make_orch()
    handle_tool_call("generate_code", CODE_INPUT, orch.state)
    calls = _inject_llm(orch, [{"edit": True}])
    errors = ["[App.tsx] Duplicate import: 'Home'"]
    list(orch._fix_code(errors, "FULL_FALLBACK"))

    assert len(calls) == 1
    # 수정 지시(내부 메시지)는 LLM 호출 시점엔 보이고
    assert any(m.get("_internal") and "Duplicate import" in str(m.get("content"))
               for m in calls[0]["messages"])
    # 루프가 끝나면 히스토리에서 사라진다
    assert not any(m.get("_internal") for m in orch.state._messages)
    # #67 T2: 수정 라운드는 낮은 출력 상한(MAX_OUTPUT_TOKENS_FIX)으로 호출된다
    from agent.orchestrator_stream import MAX_OUTPUT_TOKENS_FIX
    assert calls[0]["max_tokens"] == MAX_OUTPUT_TOKENS_FIX


def test_modify_clarifying_question_is_normal_completion():
    """실사고: "디자인 별로야, 기능 없어" 같은 모호한 요청에 요구사항을 좁히는 질문으로
    답한 턴이 계약 미이행 ERROR + 재시도(47s/$0.05)를 타던 케이스 — 질문은 정상 완결."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("디자인이 좀 별로야. 기능이 너무 없어")
    calls = _inject_llm(orch, [{"text": "어떤 기능을 추가하고 싶은지 알려줄 수 있을까요? 예: 굵게, 리스트, 체크박스?"}])
    contract = orch._implement_contract("modify_request")
    events = list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert len(calls) == 1                       # 재시도 없음
    assert orch._turn_modify_failed is False     # 실패 아님
    assert orch._turn_modify_clarified is True   # 관측 스코어용 플래그
    assert "적용되지 않았어요" not in _tokens(events)  # 안내문도 없음
    assert orch.state._messages[-1]["role"] == "assistant"  # 질문 보존


def test_suppressed_retry_question_is_reemitted():
    """약속(무변경) → 강제 재시도가 질문으로 답한 경우: 질문 텍스트는 라이브에서 억제됐으므로
    다시 내보내야 유저가 답할 수 있다."""
    orch = _setup_modify_orch()
    orch.state.add_user_message("사진 바꿔줘")
    _inject_llm(orch, [{"text": "그럼 바로 수정할 수 있어!"},
                       {"text": "어떤 사진 스타일을 원해요? 실사, 일러스트 중에서요?"}])
    contract = orch._implement_contract("modify_request")
    events = list(orch._agent_loop_stream("sys", max_loops=3, contract=contract))

    assert orch._turn_code_retries == 1
    assert orch._turn_modify_failed is False
    assert "어떤 사진 스타일" in _tokens(events)  # 억제됐던 질문이 재방출됨
    assert "적용되지 않았어요" not in _tokens(events)
