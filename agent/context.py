from __future__ import annotations

import copy
from typing import List, Dict, Any
from langfuse import get_client
from agent.models import ProjectState
from agent.diagram import DiagramManager
from agent.usage import update_generation as _update_generation
from agent.llm_config import HAIKU


def _is_tool_use(block) -> bool:
    """content block이 tool_use인지 판별 (dict / SDK 객체 공용)"""
    if isinstance(block, dict):
        return block.get("type") == "tool_use"
    return getattr(block, "type", None) == "tool_use"


def _extract_text(block) -> str | None:
    """content block에서 텍스트 추출. 없으면 None."""
    if isinstance(block, dict):
        if block.get("type") == "text" and block.get("text"):
            return block["text"]
    elif hasattr(block, "text") and block.text:
        return block.text
    return None


class SessionState:
    def __init__(self):
        self.project = ProjectState()
        self.diagram_manager = DiagramManager()
        self._messages: List[Dict[str, Any]] = []
        self.coding_type: str = ""                      # "react" | "blockly"
        self.title: str = ""                              # 프로젝트 제목
        self.description: str = ""                        # 프로젝트 한 줄 설명
        self.generated_code_map: Dict[str, str] = {}  # {filepath: code}
        self.blockly_xml: str = ""                      # MODI Blockly XML
        self.blockly_flowchart: list = []               # 흐름도 노드
        self.blockly_detail: str = ""                    # 흐름도 탭용 — 코드가 무슨 동작을 하는지 상세 설명
        self.blockly_code_langs: dict = {}              # {python, javascript, c}
        self.modi_grid: list = []                        # MODI 모듈 격자 배치 (블록·하이브리드 공용; 매 턴 재생성, 미저장)
        self.modi_rotations: dict = {}                   # {모듈키: 회전각(0/90/180/270)} — 배치 회전
        self.modi_attachments: dict = {}                 # {모터키: 'wheel'/'i_horn'} — 축 부착물
        self.design_turns: int = 0                       # 설계 모드 핑퐁 횟수 (충분히 협의 후 구현 전환용)
        self.modi_modules: dict = {}                    # {modules: [...], assembly: [...]} — 준비물/조립 가이드
        self.learning_notes: List[Dict[str, str]] = []  # [{title, what, why, where}]
        self.code_annotations: List[Dict[str, Any]] = []  # [{file, line, title, explanation}]
        self.app_type: str = ""  # "mobile" | "desktop"
        # 턴별 델타 추적: _done_event에서 새로 생긴 것만 보내기 위함
        self._notes_snapshot: int = 0
        self._annotations_snapshot: int = 0
        self._code_dirty: bool = False            # 이번 턴에 코드 변경 여부
        self._blockly_snapshot: str = ""       # begin_turn 시점의 blockly XML
        self._code_map_snapshot: Dict[str, str] = {}  # begin_turn 시점의 코드맵 (검증 실패 롤백용)
        self._generated_files_snapshot: list = []      # begin_turn 시점의 파일 메타데이터
        self._blockly_flowchart_snapshot: list = []
        self._blockly_detail_snapshot: str = ""
        self._blockly_code_langs_snapshot: dict = {}
        self._modi_grid_snapshot: list = []
        self._modi_rotations_snapshot: dict = {}
        self._modi_attachments_snapshot: dict = {}
        self._modi_modules_snapshot: dict = {}        # begin_turn 시점의 모디 준비물 (검증 실패 롤백용)
        self._app_type_snapshot: str = ""
        self._title_snapshot: str = ""
        self._description_snapshot: str = ""

    def begin_turn(self):
        """매 턴 시작 시 호출 — 현재 상태의 스냅샷 저장"""
        self._notes_snapshot = len(self.learning_notes)
        self._annotations_snapshot = len(self.code_annotations)
        self._code_dirty = False
        self._blockly_snapshot = self.blockly_xml
        self._code_map_snapshot = dict(self.generated_code_map)
        self._generated_files_snapshot = copy.deepcopy(self.project.generated_files)
        self._blockly_flowchart_snapshot = copy.deepcopy(self.blockly_flowchart)
        self._blockly_detail_snapshot = self.blockly_detail
        self._blockly_code_langs_snapshot = copy.deepcopy(self.blockly_code_langs)
        self._modi_grid_snapshot = copy.deepcopy(self.modi_grid)
        self._modi_rotations_snapshot = copy.deepcopy(self.modi_rotations)
        self._modi_attachments_snapshot = copy.deepcopy(self.modi_attachments)
        self._modi_modules_snapshot = copy.deepcopy(self.modi_modules)
        self._app_type_snapshot = self.app_type
        self._title_snapshot = self.title
        self._description_snapshot = self.description

    def rollback_turn_artifacts(self):
        """검증에 실패한 턴의 산출물을 턴 시작 시점으로 되돌린다.

        state에는 항상 '검증을 통과한' 산출물만 남는다 — 실패물을 state에 두고 턴 한정
        플래그로만 done 이벤트를 억제하면, 플래그가 리셋되는 다음 턴의 done 이벤트와
        세션 저장/복원 경로로 실패물이 그대로 미리보기에 새어 나간다.
        실패 턴에 만들어진 학습 노트·코드 주석도 함께 버린다(되돌린 코드와 안 맞으므로).
        """
        self.generated_code_map = dict(self._code_map_snapshot)
        self.project.generated_files = copy.deepcopy(self._generated_files_snapshot)
        self.blockly_xml = self._blockly_snapshot
        self.blockly_flowchart = copy.deepcopy(self._blockly_flowchart_snapshot)
        self.blockly_detail = self._blockly_detail_snapshot
        self.blockly_code_langs = copy.deepcopy(self._blockly_code_langs_snapshot)
        self.modi_grid = copy.deepcopy(self._modi_grid_snapshot)
        self.modi_rotations = copy.deepcopy(self._modi_rotations_snapshot)
        self.modi_attachments = copy.deepcopy(self._modi_attachments_snapshot)
        self.modi_modules = copy.deepcopy(self._modi_modules_snapshot)
        self.app_type = self._app_type_snapshot
        self.title = self._title_snapshot
        self.description = self._description_snapshot
        self.learning_notes = self.learning_notes[:self._notes_snapshot]
        self.code_annotations = self.code_annotations[:self._annotations_snapshot]
        self._code_dirty = False

    def mark_code_dirty(self):
        """generate_code 또는 edit_code가 호출되면 표시"""
        self._code_dirty = True

    def code_changed_this_turn(self) -> bool:
        """이번 턴에 코드가 생성/수정되었는지"""
        return self._code_dirty

    def blockly_changed_this_turn(self) -> bool:
        """이번 턴에 blockly XML이 변경되었는지"""
        return self.blockly_xml != self._blockly_snapshot

    def get_new_learning_notes(self) -> List[Dict[str, str]]:
        """이번 턴에 새로 추가된 학습 노트만 반환"""
        return self.learning_notes[self._notes_snapshot:]

    def get_new_code_annotations(self) -> List[Dict[str, Any]]:
        """이번 턴에 새로 추가된 코드 주석만 반환"""
        return self.code_annotations[self._annotations_snapshot:]

    def add_user_message(self, content: str):
        self._messages.append({"role": "user", "content": content})

    def add_internal_user_message(self, content: str):
        """오케스트레이터가 주입하는 내부 지시(예: 재시도 nudge). LLM 컨텍스트에는 포함되지만
        _internal 플래그로 표시해 세션 저장/복원 채팅(_serialize_messages)에는 노출하지 않는다."""
        self._messages.append({"role": "user", "content": content, "_internal": True})

    def drop_internal_messages(self):
        """재시도 기계가 남긴 내부 메시지(폐기 마커·nudge)를 히스토리에서 제거.

        턴이 끝나면 재시도 과정은 결과(코드 또는 사과)로만 남긴다 — 내부 메시지를 남겨두면
        이후 턴의 LLM 컨텍스트·요약·컴팩트 히스토리를 오염시키고("지금 바로 도구로..." 지시가
        눌러앉음), 조기 실패 시 user(nudge)로 끝난 히스토리가 다음 턴 add_user_message와
        만나 user-user 연속(role 교대 위반 소지)을 만든다."""
        self._messages = [m for m in self._messages if not m.get("_internal")]

    def rollback_turn_messages_to_user(self, user_index: int | None, assistant_text: str = "") -> bool:
        """이번 턴 user 메시지만 남기고 이후 LLM/tool 히스토리를 폐기한다.

        검증 실패 시 산출물 state만 되돌리면, 실패한 generate_code/edit_code 입력이
        _messages에 남아 다음 턴 LLM 컨텍스트와 토큰 비용을 오염시킨다. 실패 턴의 도구
        흔적은 버리고, 유저가 본 실패 안내만 정식 assistant 메시지로 남긴다.
        """
        if user_index is None or user_index < 0 or user_index >= len(self._messages):
            return False
        if self._messages[user_index].get("role") != "user":
            return False
        self._messages = self._messages[:user_index + 1]
        if assistant_text:
            self.add_assistant_message([{"type": "text", "text": assistant_text}])
        return True

    def replace_last_assistant_with_tool_uses(self, intro: str, tool_uses: list) -> bool:
        """마지막 assistant 텍스트 응답을 실제 tool_use 블록들로 재작성 (텍스트 덤프 복구용).

        코드를 도구 대신 텍스트로 쏟은 응답을 '원래 냈어야 할 형태'(도구 호출)로 바꿔,
        이어 붙는 tool_result와 함께 유효한 히스토리가 되게 한다. 수만 토큰 덤프도 사라진다."""
        if not self._messages or self._messages[-1]["role"] != "assistant":
            return False
        blocks: list = []
        if intro:
            blocks.append({"type": "text", "text": intro})
        blocks += [
            {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
            for tu in tool_uses
        ]
        last = self._messages[-1]
        last["content"] = blocks
        last.pop("_internal", None)
        return True

    def void_last_assistant_text(self, marker: str) -> bool:
        """마지막 메시지가 '도구 호출 없는 assistant 텍스트'면 내용을 marker 한 줄로 치환하고 True.

        구현 재시도용: 코드를 도구 대신 텍스트로 쏟은 실패 응답(수만 토큰)을 컨텍스트에 그대로
        끌고 가면 재시도마다 입력 비용이 배가되고 모델이 '이미 위에 썼다'고 앵커링한다.
        role 교대는 유지한 채 내용만 폐기한다. 마지막이 assistant가 아니거나(조기 실패 등)
        tool_use가 있으면 아무것도 하지 않고 False — 호출부는 이걸로 재시도 여부를 판단한다."""
        if not self._messages:
            return False
        last = self._messages[-1]
        if last["role"] != "assistant" or not isinstance(last.get("content"), list):
            return False
        if any(_is_tool_use(b) for b in last["content"]):
            return False
        last["content"] = [{"type": "text", "text": marker}]
        last["_internal"] = True
        return True

    def drop_text_from_last_assistant_with_tools(self) -> bool:
        """마지막 assistant 도구 호출 메시지에서 텍스트 블록만 제거.

        모델이 도구를 하나 호출하면서 "완료했어요" 같은 거짓 텍스트를 함께 낸 경우,
        tool_use ↔ tool_result 페어링은 보존하되 유저에게 보이지 않은 텍스트만 히스토리에서
        제거한다. 말미에 tool_result(user)가 붙은 상태도 고려해 뒤에서부터 assistant를 찾는다.
        """
        for msg in reversed(self._messages):
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            if not any(_is_tool_use(b) for b in msg["content"]):
                return False
            before = len(msg["content"])
            msg["content"] = [b for b in msg["content"] if not _extract_text(b)]
            return len(msg["content"]) != before
        return False

    def add_assistant_message(self, content_blocks: list):
        # SDK 객체를 API 호환 dict로 변환 (필요한 필드만)
        serialized = []
        for block in content_blocks:
            if isinstance(block, dict):
                serialized.append(block)
            elif _is_tool_use(block):
                serialized.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif (text := _extract_text(block)) is not None:
                serialized.append({"type": "text", "text": text})
            else:
                serialized.append({"type": "text", "text": str(block)})
        self._messages.append({"role": "assistant", "content": serialized})

    def add_tool_results(self, tool_results: List[Dict[str, Any]]):
        self._messages.append({"role": "user", "content": tool_results})

    @staticmethod
    def _llm_block(block) -> Dict[str, Any] | None:
        """Anthropic SDK/CLI에 보낼 content block만 추린다.

        _internal, _agent_steps 같은 서버 전용 메타데이터가 API 모드에서 400을 만들지
        않도록 송신 직전에 제거한다. content 자체는 유지해 재시도 nudge는 LLM이 본다.
        """
        if not isinstance(block, dict):
            if getattr(block, "type", None) == "text":
                return {"type": "text", "text": block.text}
            if getattr(block, "type", None) == "tool_use":
                return {"type": "tool_use", "id": block.id, "name": block.name, "input": copy.deepcopy(block.input)}
            return {"type": "text", "text": str(block)}

        btype = block.get("type")
        if btype == "text":
            return {"type": "text", "text": block.get("text", "")}
        if btype == "tool_use":
            return {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": copy.deepcopy(block.get("input", {})),
            }
        if btype == "tool_result":
            out = {
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": copy.deepcopy(block.get("content", "")),
            }
            if "is_error" in block:
                out["is_error"] = block["is_error"]
            return out
        return None

    @classmethod
    def _llm_message(cls, msg: Dict[str, Any]) -> Dict[str, Any]:
        content = msg.get("content", "")
        if isinstance(content, list):
            blocks = [b for block in content if (b := cls._llm_block(block)) is not None]
            return {"role": msg["role"], "content": blocks}
        return {"role": msg["role"], "content": content}

    def get_api_messages(self) -> List[Dict[str, Any]]:
        return [self._llm_message(msg) for msg in self._messages]

    def get_compact_messages(self) -> List[Dict[str, Any]]:
        """수정 요청 시 사용: tool 호출/결과를 제거하고 텍스트만 남긴 경량 히스토리.

        단, 도구를 쓴 턴은 '무엇을 실행했는지' 한 줄로 남긴다 — 흔적을 전부 지우면
        모델이 보는 대화가 순수 채팅이 되어, 자기가 도구로 작업해 온 전례 없이
        "수정할게요" 같은 말만 하고 끝내는 채팅 관성에 빠진다(특히 소형 모델).
        전체 tool JSON/결과가 아니라 이름 요약 한 줄이라 설계 Q&A 노이즈는 없다."""
        compact = []
        for msg in self._messages:
            if msg["role"] == "user":
                if isinstance(msg["content"], str):
                    compact.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                texts = [t for b in msg["content"] if (t := _extract_text(b))]
                tool_names = [
                    (b.get("name", "") if isinstance(b, dict) else getattr(b, "name", ""))
                    for b in msg["content"] if _is_tool_use(b)
                ]
                if tool_names:
                    counts: Dict[str, int] = {}
                    for name in tool_names:
                        counts[name] = counts.get(name, 0) + 1
                    summary = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in counts.items())
                    texts.append(f"(도구 실행: {summary})")
                if texts:
                    compact.append({"role": "assistant", "content": "\n".join(texts)})
        return compact

    def get_current_code_context(self) -> str:
        """현재 생성된 코드를 요약 형태로 반환 (시스템 프롬프트에 주입용)"""
        if not self.generated_code_map:
            return ""
        parts = ["## 현재 생성된 코드"]
        for path, code in self.generated_code_map.items():
            parts.append(f"\n### {path}\n```\n{code}\n```")
        return "\n".join(parts)

    def get_text_history(self) -> str:
        """유저에게 보여진 대화만 추출 (tool_use 포함 중간 턴 제외)"""
        parts = []
        for msg in self._messages:
            if msg.get("_internal"):
                continue  # 폐기 마커·nudge — 유저에게 보여진 적 없는 내부 메시지
            if msg["role"] == "user" and isinstance(msg["content"], str):
                parts.append(f"학습자: {msg['content']}")
            elif msg["role"] == "assistant":
                if any(_is_tool_use(b) for b in msg["content"]):
                    continue
                for block in msg["content"]:
                    if (text := _extract_text(block)):
                        parts.append(f"튜터: {text}")
        return "\n".join(parts)

    def get_files_summary(self) -> str:
        if not self.project.generated_files:
            return "아직 생성된 파일이 없습니다."
        lines = []
        for f in self.project.generated_files:
            lines.append(f"- {f.path} ({f.language}): {f.description}")
        return "\n".join(lines)

    def summarize_if_needed(self, client, keep_recent: int = 6):
        """대화가 길어지면 오래된 부분을 요약하고, 최근 턴은 원본 유지"""
        user_turns = sum(
            1 for m in self._messages
            if m["role"] == "user" and isinstance(m["content"], str)
        )
        if user_turns <= 20:
            return

        # 최근 keep_recent개 메시지는 원본 유지, 나머지를 요약
        old_messages = self._messages[:-keep_recent] if keep_recent < len(self._messages) else []
        recent_messages = self._messages[-keep_recent:] if keep_recent < len(self._messages) else self._messages

        if not old_messages:
            return

        # 오래된 부분만 텍스트로 변환 후 요약
        old_parts = []
        for msg in old_messages:
            if msg.get("_internal"):
                continue  # 내부 메시지(폐기 마커·nudge)가 요약에 눌러앉는 것 방지
            if msg["role"] == "user" and isinstance(msg["content"], str):
                old_parts.append(f"학습자: {msg['content']}")
            elif msg["role"] == "assistant":
                for block in msg["content"]:
                    if (text := _extract_text(block)):
                        old_parts.append(f"튜터: {text}")

        if not old_parts:
            return

        with get_client().start_as_current_observation(
                name="대화 요약 (summarize)", as_type="generation",
                input={"text": "\n".join(old_parts)}) as _gen:
            response = client.messages.create(
                model=HAIKU,
                max_tokens=1024,
                system="아래 대화를 핵심 결정사항 위주로 요약하세요. 설계 결정, 컴포넌트 구조, 기술 선택, 사용자 요구사항 등을 보존하세요.",
                messages=[{"role": "user", "content": "\n".join(old_parts)}]
            )
            _update_generation(_gen, HAIKU, response=response,
                               output=response.content[0].text,
                               step="summarize")
        summary = response.content[0].text

        self._messages = [
            {"role": "user", "content": f"[이전 대화 요약]\n{summary}"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "네, 이전 대화 내용을 확인했습니다. 계속 진행할게요."}
            ]},
            *recent_messages,
        ]

    def reset(self):
        self.project = ProjectState()
        self.diagram_manager = DiagramManager()
        self._messages = []
        self.coding_type = ""
        self.title = ""
        self.description = ""
        self.generated_code_map = {}
        self.blockly_xml = ""
        self.blockly_flowchart = []
        self.blockly_detail = ""
        self.blockly_code_langs = {}
        self.modi_grid = []
        self.modi_rotations = {}
        self.modi_attachments = {}
        self.design_turns = 0
        self.modi_modules = {}
        self.learning_notes = []
        self.code_annotations = []
        self.app_type = ""
        self._code_dirty = False
        self._blockly_snapshot = ""
        self._code_map_snapshot = {}
        self._generated_files_snapshot = []
        self._blockly_flowchart_snapshot = []
        self._blockly_detail_snapshot = ""
        self._blockly_code_langs_snapshot = {}
        self._modi_grid_snapshot = []
        self._modi_rotations_snapshot = {}
        self._modi_attachments_snapshot = {}
        self._modi_modules_snapshot = {}
        self._app_type_snapshot = ""
        self._title_snapshot = ""
        self._description_snapshot = ""
