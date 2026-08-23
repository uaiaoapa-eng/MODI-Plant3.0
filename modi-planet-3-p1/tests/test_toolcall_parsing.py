"""tool-call 태그 파싱/필터 — 변종 태그(<function_calls>·</tool_function_calls> 등) 관용 처리.

실사고: haiku가 네이티브 학습 포맷 계열 태그로 흘리면 ① 태그가 채팅에 그대로 새고
② 안의 도구 호출이 실행되지 않아 "말로는 변경했다는데 실제 변경 없음"이 됐다.
_parse_response(비스트리밍)와 _ToolCallFilter(스트리밍) 두 파서의 계약('{'/'[' 가드,
stray-close 처리)이 일치하는지 함께 검증한다.
"""
from agent.claude_client import _parse_response, _ToolCallFilter

TOOL_JSON = '{"name": "generate_code", "input": {"file_path": "App.tsx", "code": "x"}}'


# ── _parse_response (비스트리밍/최종 메시지 경로) ──

def test_parse_standard_tag():
    text = f"만들게요.\n<tool_call>\n{TOOL_JSON}\n</tool_call>\n끝."
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code"]
    assert "tool_call" not in clean and "만들게요." in clean and "끝." in clean


def test_parse_native_wrapper_variant():
    """실사고 형태: <function_calls> 래퍼 안에 <tool_call>, 닫기는 </tool_function_calls>."""
    text = ("수정할게요.\n<function_calls>\n<tool_call>\n" + TOOL_JSON +
            "\n</tool_call>\n</tool_function_calls>\n적용했어요.")
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code"]
    assert "<" not in clean  # 태그 잔해 없음
    assert "수정할게요." in clean and "적용했어요." in clean


def test_parse_empty_wrapper_hidden():
    """실사고 화면 그대로: 빈 래퍼 태그 쌍이 채팅에 노출되던 케이스."""
    text = "<function_calls>\n</tool_function_calls>\n실제 사진으로 업그레이드했어!"
    clean, tools = _parse_response(text, has_tools=True)
    assert tools == []
    assert clean == "실제 사진으로 업그레이드했어!"


def test_parse_direct_function_calls_tag():
    text = f"<function_calls>{TOOL_JSON}</function_calls>"
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code"]
    assert clean == ""


def test_parse_preserves_prose_and_generic_tags():
    # '{' 가드: 본문이 JSON이 아니면 평문 보존, 일반어 태그(<toolbar>·<function>)는 불변
    text = "<tool>이 도구는 설명입니다</tool> 그리고 <toolbar> 와 <function> 은 평문."
    clean, tools = _parse_response(text, has_tools=True)
    assert tools == []
    assert "<tool>이 도구는 설명입니다</tool>" in clean
    assert "<toolbar>" in clean and "<function>" in clean


def test_parse_multiple_sequential_calls():
    a = '{"name": "generate_code", "input": {"file_path": "A.tsx", "code": "a"}}'
    b = '{"name": "edit_code", "input": {"file_path": "B.tsx"}}'
    text = f"<tool_call>{a}</tool_call>\n<tool_call>{b}</tool_call>"
    _, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code", "edit_code"]


# ── _ToolCallFilter (스트리밍 경로) — 청크 경계에 강해야 한다 ──

def _feed_chunks(text, size=7):
    filt = _ToolCallFilter()
    visible = ""
    for i in range(0, len(text), size):
        visible += filt.feed(text[i:i + size])
    return visible + filt.flush(), filt


def test_filter_wrapper_variant_streaming():
    text = ("수정할게요.\n<function_calls>\n<tool_call>\n" + TOOL_JSON +
            "\n</tool_call>\n</tool_function_calls>\n적용했어요.")
    visible, filt = _feed_chunks(text)
    assert "<" not in visible
    assert "수정할게요." in visible and "적용했어요." in visible
    assert filt.peek_tool_name() == "generate_code"  # 안쪽 도구가 실제로 인식됨


def test_filter_empty_wrapper_and_stray_close():
    visible, _ = _feed_chunks("<function_calls>\n</tool_function_calls>\n업그레이드했어!")
    assert visible.strip() == "업그레이드했어!"


def test_filter_preserves_prose_tool_pair_and_generic_tags():
    visible, _ = _feed_chunks("<tool>이 도구는 설명입니다</tool> <div>내용</div> <toolbar>메뉴</toolbar>")
    assert "<tool>이 도구는 설명입니다</tool>" in visible  # 평문 쌍 보존 (stray-drop과 비충돌)
    assert "<div>내용</div>" in visible and "<toolbar>메뉴</toolbar>" in visible


# ── 근접 실패(near-miss) JSON 복구 — 실사고(2026-07-02, 당근마켓 수정 턴) ──
# Haiku가 인자를 "input" 래퍼 없이 최상위에 펼치면서 닫는 괄호는 중첩 포맷 습관대로
# 두 개(`…}}`)로 닫음 → strict json.loads 'Extra data' → 블록 10개 전부 조용히 폐기
# → 도구 호출 0 → "텍스트 응답" 계약 미이행으로 턴 실패. 재시도도 같은 포맷 실수라
# 프롬프트 강화가 아닌 파서 관용이 해법.

FLAT_EXTRA_BRACE = (
    '{"name": "edit_code", "file_path": "pages/Home.tsx", '
    '"old_code": "image: \'📱\'", "new_code": "image: \'https://x/1.jpg\'", '
    '"description": "이미지 교체"}}'
)


def test_parse_recovers_flat_args_with_extra_brace():
    """트레이스 원문 그대로: input 래퍼 누락 + 꼬리 `}` 하나 초과."""
    text = f"실제 사진으로 바꿀게!\n<tool_call>\n{FLAT_EXTRA_BRACE}\n</tool_call>\n완성!"
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["edit_code"]
    assert tools[0].input == {
        "file_path": "pages/Home.tsx",
        "old_code": "image: '📱'",
        "new_code": "image: 'https://x/1.jpg'",
        "description": "이미지 교체",
    }
    assert "실제 사진으로 바꿀게!" in clean and "완성!" in clean


def test_parse_recovers_nested_with_extra_brace():
    text = f"<tool_call>{TOOL_JSON}}}</tool_call>"  # 정상 중첩 + 꼬리 `}` 초과
    _, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code"]
    assert tools[0].input == {"file_path": "App.tsx", "code": "x"}


def test_parse_recovers_flat_args_wellformed():
    flat = '{"name": "edit_code", "file_path": "A.tsx", "old_code": "a", "new_code": "b"}'
    _, tools = _parse_response(f"<tool_call>{flat}</tool_call>", has_tools=True)
    assert tools[0].input == {"file_path": "A.tsx", "old_code": "a", "new_code": "b"}


def test_parse_flat_extras_do_not_override_nested_input():
    mixed = '{"name": "edit_code", "input": {"file_path": "A.tsx"}, "junk": 1}'
    _, tools = _parse_response(f"<tool_call>{mixed}</tool_call>", has_tools=True)
    assert tools[0].input == {"file_path": "A.tsx"}  # 정상 input이 있으면 그대로


def test_parse_truly_broken_json_still_dropped():
    broken = '{"name": "edit_code", "old_code": "끝나지 않는 문자열'
    clean, tools = _parse_response(f"고칠게요.<tool_call>{broken}</tool_call>", has_tools=True)
    assert tools == []
    assert clean == "고칠게요."  # 숨기되 실행은 안 함 (기존 계약 유지)


def test_parse_ten_flat_blocks_all_recovered():
    """실사고 규모 재현: 연속 10블록 전부 복구되는지."""
    text = "바꿀게!\n" + "\n".join(
        f'<tool_call>\n{{"name": "edit_code", "file_path": "f{i}.tsx", '
        f'"old_code": "a", "new_code": "b"}}}}\n</tool_call>' for i in range(10)
    ) + "\n완성됐어!"
    clean, tools = _parse_response(text, has_tools=True)
    assert len(tools) == 10
    assert all(t.name == "edit_code" for t in tools)
    assert clean.startswith("바꿀게!") and clean.endswith("완성됐어!")


# ── 배열 배칭 변종 — 실사고(2026-07-03, 갤러그 수정 턴) ──
# 원샷 배칭 지시("모든 호출을 한 응답에")를 따르며 Haiku가 여러 호출을 JSON 배열
# 하나로 묶음: <function_calls>[ {…}, {…} ]</function_calls>. '{' 전용 가드가 배열을
# 평문 취급 → JSON 덩어리가 채팅에 그대로 새고(닫는 태그만 stray-제거) 도구 실행 0.

ARRAY_CALLS = (
    '[ {"name": "generate_code", "input": {"file_path": "App.tsx", "code": "x"}},\n'
    '  {"name": "set_modi_layout", "input": {"grid": [[null, "network"]]}} ]'
)


def test_parse_array_batched_calls():
    text = f"<function_calls>\n{ARRAY_CALLS}\n</function_calls>\n\n완벽해! 이제 게임이 멋져졌어."
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["generate_code", "set_modi_layout"]
    assert tools[1].input == {"grid": [[None, "network"]]}
    assert clean == "완벽해! 이제 게임이 멋져졌어."


def test_filter_array_batched_calls_streaming():
    text = f"수정할게.\n<function_calls>\n{ARRAY_CALLS}\n</function_calls>\n완벽해!"
    visible, filt = _feed_chunks(text)
    assert "generate_code" not in visible and "[" not in visible
    assert "수정할게." in visible and "완벽해!" in visible
    assert filt.peek_tool_name() == "generate_code"  # 배열 안 첫 도구 인식


def test_parse_array_with_flat_items_recovered():
    # 배열 원소에도 flat-args 관용(입력 승격)이 적용된다
    flat_array = '[{"name": "edit_code", "file_path": "A.tsx", "old_code": "a", "new_code": "b"}]'
    _, tools = _parse_response(f"<tool_call>{flat_array}</tool_call>", has_tools=True)
    assert [t.name for t in tools] == ["edit_code"]
    assert tools[0].input == {"file_path": "A.tsx", "old_code": "a", "new_code": "b"}


def test_parse_broken_array_hidden_not_executed():
    broken = '[{"name": "edit_code", "old_code": "끝나지 않는'
    clean, tools = _parse_response(f"고칠게요.<tool_call>{broken}</tool_call>", has_tools=True)
    assert tools == []
    assert clean == "고칠게요."  # 숨기되 실행은 안 함 (깨진 JSON 계약과 동일)


# ── '[' 평문 가드 정밀화 — 배열 배칭('[{')만 tool-call, 대괄호 주석([예시]…)은 평문 보존 ──
# '['만 보고 tool-call로 삼키면 <tool>[예시] 설명</tool> 같은 평문이 채팅·히스토리에서
# 조용히 사라진다(도구 실행도 없음). '[' 뒤 첫 비공백이 '{'일 때만 배칭 시도로 간주한다.

def test_parse_preserves_bracket_prose():
    text = "자세한 건 <tool>[예시] 버튼을 누르면 LED가 켜져요</tool> 참고하세요."
    clean, tools = _parse_response(text, has_tools=True)
    assert tools == []
    assert "[예시] 버튼을 누르면 LED가 켜져요" in clean
    assert "참고하세요." in clean


def test_filter_preserves_bracket_prose_streaming():
    text = "자세한 건 <call>[1] 버튼을 누르면 LED가 켜져요</call> 참고."
    visible, _ = _feed_chunks(text)
    assert "[1] 버튼을 누르면 LED가 켜져요" in visible
    assert "참고." in visible


def test_parse_array_with_leading_whitespace_still_batched():
    # '['와 '{' 사이 공백/개행은 배칭으로 인정 — 평문 보존이 실사고 배열 변종을 되돌리면 안 된다
    text = '<function_calls>[\n  {"name": "edit_code", "input": {"file_path": "A.tsx"}}\n]</function_calls>'
    clean, tools = _parse_response(text, has_tools=True)
    assert [t.name for t in tools] == ["edit_code"]
    assert clean == ""
