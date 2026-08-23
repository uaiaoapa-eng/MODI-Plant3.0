"""스트리밍 버전 오케스트레이터 — 각 단계를 실시간으로 yield"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextvars import copy_context
from dataclasses import dataclass
from typing import Callable, Generator
from langfuse import observe, get_client, propagate_attributes
from agent.claude_client import create_client, _use_local_cli, ToolUseBlock
from agent.models import Phase
# 분기·프라임의 단일 소스(agent/prime_service.py). _reuse_block/_classify_turn_intent 는
# 여기에 위임하고, 오케스트레이터는 인스턴스 상태(_reuse_flag/_ontology_primed/_suggested_keys)
# 반영만 담당한다. 상수/스위치도 여기서 가져와 중복 정의를 없앤다.
from agent import prime_service
from agent import direct_serve
from agent.prime_service import (
    CODE_ACTION_INTENTS as _CODE_ACTION_INTENTS,
    NO_CODE_INTENTS as _NO_CODE_INTENTS,
)
from agent.context import SessionState
from agent.router import Router
from agent.guardrails import redact_pii, check_input, pick_redirect
from agent.usage import extract_usage as _extract_usage, update_generation as _update_generation, attribute_output as _attribute_output, usage_details as _usage_details
from agent.quota import TokenUsage, usage_from_details
from agent.llm_config import HAIKU
from agent.prompts import SAFETY_ADDENDUM, DESIGN_SYSTEM_PROMPT, IMPLEMENT_SYSTEM_PROMPT, IMPLEMENT_CONTEXT_TEMPLATE, QUICK_IMPLEMENT_PROMPT, VERIFY_SYSTEM_PROMPT, VERIFY_CONTEXT_TEMPLATE, BLOCKLY_DESIGN_PROMPT, BLOCKLY_IMPLEMENT_PROMPT, HYBRID_DESIGN_ADDENDUM, HYBRID_IMPLEMENT_PROMPT, HYBRID_QUICK_IMPLEMENT_PROMPT, CODE_TOOL_CONTRACT, MODIFY_EDIT_DIRECTIVE, REUSE_SEED_HEAD
from agent.tools import TOOL_DEFINITIONS, handle_tool_call, validate_generated_code, validate_blockly_xml, get_tools_for_phase, load_modi_core
from agent.builder import build_check
from agent.examples import is_vague_request, get_suggestion_message
from agent.salvage import extract_code_files
from agent.modi_modules import (
    SCRIPT_EXTS,
    extract_modi_module_keys,
    extract_module_keys_from_xml,
    build_modi_modules_doc,
    validate_hybrid_code_map,
)
from agent.retry import is_retryable_error, is_quota_limit_error, is_auth_login_error, backoff_delay
from agent.errors import ErrorCode, error_event
from agent.prompt_cache import cacheable_system, cacheable_tools, strip_cache_boundary, CACHE_BOUNDARY
from agent.intent import classify_existing_artifact_intent, looks_like_short_confirmation

# 턴 계약(TurnContract) 미이행 시 허용하는 추가 재시도 횟수. 방어 순서:
# ① 프롬프트 말단 도구 계약(_tool_choice_reminder / CODE_TOOL_CONTRACT)
# ② 텍스트 덤프에서 코드 복구(salvage — LLM 재호출 없음) ③ 이 재시도(최후 보험).
# 0으로 끄면 실패 시 즉시 사용자에게 알리고 끝난다. 각 재시도 = LLM 1회 비용.
MAX_CODE_RETRIES = 1

# 검증(정적/빌드) 실패 → 수정 후 '재검증'까지 도는 수정 라운드 수.
# 수정이 실제로 통했는지 확인 없이 결과를 내보내지 않기 위한 예산.
MAX_FIX_ROUNDS = 1


def _int_env(name: str, default: int) -> int:
    """정수 환경변수 파싱 — 비어있거나 잘못되면 기본값. 하한 256(너무 낮으면 첫 문장도 못 냄)."""
    try:
        return max(256, int((os.getenv(name) or "").strip() or default))
    except (ValueError, TypeError):
        return default


# #67 T2: 출력 토큰 상한. Langfuse 실측상 출력이 비용을 지배(입력 비캐시의 5.6배, Haiku
# 출력 단가 ~5×)하므로 상한을 조절 가능하게 뺐다. 기본값은 종전과 동일(8192)이라 무회귀 —
# 운영자가 truncation 스코어(출력 잘림)를 보며 안전하게 내릴 수 있다.
# 수정(fix)/재검증 라운드는 '타깃 편집'이라 첫 빌드보다 낮은 상한을 둔다(런어웨이 방지).
MAX_OUTPUT_TOKENS = _int_env("MAX_OUTPUT_TOKENS", 8192)
MAX_OUTPUT_TOKENS_FIX = _int_env("MAX_OUTPUT_TOKENS_FIX", 6144)
# #70 P2: 후처리 서브에이전트(설계추출·학습노트·코드주석) 출력 상한. 실측상 후처리 create
# 서브콜이 턴 출력의 ~48% — 여기에 #67 T2 방식 상한을 둔다.
# 기본값 4096: API 모드 실측(Haiku)상 종전 하드코딩 2048 은 학습노트(5~8개, ~2,200 tok)·설계·
# 주석 생성을 잘라 산출물이 0개가 됐다(잘림 스코어로 확인). CLI(프로덕션)는 max_tokens 를 무시해
# 무영향이지만, API 전환 시 2048 은 회귀라 잘리지 않는 4096 을 기본으로 둔다. 운영자는 "후처리 출력
# 잘림(post_output_truncated)" 스코어가 0 인지 보며 데이터에 근거해 조정한다(무작정 인하 금지).
MAX_OUTPUT_TOKENS_POST = _int_env("MAX_OUTPUT_TOKENS_POST", 4096)


def _small_int_env(name: str, default: int, lo: int, hi: int) -> int:
    """작은 정수 환경변수(개수 등) — [lo, hi] 로 클램프. (_int_env 는 하한 256이라 부적합)"""
    try:
        return max(lo, min(hi, int((os.getenv(name) or "").strip() or default)))
    except (ValueError, TypeError):
        return default


# #70 후속(속도): 학습 노트 생성 병렬 샤드 수. 노트는 add_learning_note 가 배열을 한 번에 받아
# 한 호출에서 배열 전체를 순차 출력 → 후처리 임계경로(실측 API·Haiku 25s, 7개). 총 개수는
# 유지하되 '개념 렌즈'로 나눠 병렬 호출하면 wall≈1/shards. 기본 2, 1이면 종전 단일 호출(롤백).
POST_NOTES_SHARDS = _small_int_env("POST_NOTES_SHARDS", 2, 1, 4)

_NO_CODE_CHAT_PROMPT = """\
당신은 어린 학생을 돕는 친절한 코딩 튜터입니다.
이번 턴은 실행/수정 턴이 아니라 대화 턴입니다. 도구 없이 자연어로만 짧게 답하세요.
- 질문이면 현재 상황을 쉽게 설명하세요.
- 요구가 모호하면 필요한 정보를 한두 가지 물어보세요.
- 단순 반응이면 가볍게 받아주세요.
- 실제로 코드를 만들거나 고쳤다고 말하지 마세요.
- 코드 변경이 필요해 보여도 작업을 약속하지 말고, 어떤 부분을 어떻게 바꿀지 확인하세요.
"""


@dataclass(frozen=True)
class TurnContract:
    """에이전트 루프 한 번의 완료 계약 — '이 턴이 끝났을 때 참이어야 하는 것'.

    루프는 phase/coding_type/intent 조합을 개별로 알지 못한다: 계약이 미이행이면
    공용 복구 기계(salvage → nudge 재시도 → 정직한 실패)가 돌 뿐이다. 새 턴 유형이
    생기면 루프를 고치는 게 아니라 _implement_contract 팩토리에 계약을 추가한다.
    """
    done: Callable[[], bool]   # 완료 조건 (예: 산출물 존재 / 이번 턴 코드 변경)
    tool_hint: str             # nudge에 안내할 도구 이름
    question_exempt: bool      # 유저에게 '질문으로 턴을 넘긴' 응답을 정상 완결로 인정할지.
                               # 수정 계약 True: 모호한 요청에 요구사항을 좁히는 질문은 정상
                               # 대화다 — 막아야 할 건 "수정했어!" 선언 후 무변경뿐.
                               # 생성 계약 False: 1차 산출이 목표라 질문도 재시도로 민다.
    force_choice: dict         # 강제 시 사용할 tool_choice. 산출물 도구가 하나면 그 도구를
                               # 지목({"type":"tool","name":...}) — any로 걸면 소형 모델이
                               # update_diagram 같은 싸구려 도구로 조건만 때운다(실사고).
    force_from_start: bool     # 미이행 동안 처음부터 tool_choice 강제 (신규 생성)
    void_failed_text: bool     # 실패 텍스트 폐기 (신규 생성: 덤프 확실 / 수정: 답변일 수 있어 보존)
    apology: str               # 최종 실패 시 사용자 안내 ("" = 침묵하고 스코어만 — 수정 턴)
    fail_flag: str             # 최종 실패 시 세울 턴 플래그 속성명 (Langfuse 스코어와 연결)


# _agent_loop_stream의 기본값 센티널: 호출부가 계약을 안 주면 상태에서 기본 계약을 도출
_AUTO_CONTRACT = object()


# 토큰/비용 추출은 agent/usage.py 로 이동 (cache 토큰 포함, context.py 와 공용).
# _extract_usage / _update_generation 는 상단 import 로 들어온다.

# 서브에이전트(_call_tools) generation 이름을 도구명 대신 사람이 읽기 좋은 라벨로.
# 주 도구(tool_names[0])로 매핑, 없으면 "서브에이전트 (도구들)" 폴백. (HAIKU는 llm_config)
_SUBAGENT_LABELS = {
    "add_learning_note": "학습 노트 생성",
    "update_design_doc": "설계 문서 추출",
    "add_code_annotation": "코드 주석 생성",
}

# _emit_step에서 Langfuse 이벤트로 남길 '로컬'(LLM 없는) 단계. 나머지(LLM) 단계는 generation이 커버.
_LOCAL_STEP_ACTIONS = ("verify", "modules")


def _is_tool_error(result: str) -> bool:
    """도구 결과 문자열이 에러/실패를 나타내는지.

    도구 핸들러들의 실패 관례는 "오류:" 프리픽스(예: edit_code의 "오류: ... 교체할 코드를
    찾을 수 없습니다") — 이걸 못 잡으면 아무것도 안 바뀐 턴이 success로 위장된다.
    그 외에는 짧은 결과에 한해 키워드로 판정(긴 정상 결과의 우발적 'error' 오탐 방지)."""
    if result.lstrip().startswith("오류"):
        return True
    return ("실패" in result or "에러" in result or "error" in result.lower()) and len(result) < 200


# 설계(design) 모드: 최소 이만큼 핑퐁(설계 협의)한 뒤에야 구현 전환 가능. (web·blockly 공통)
MIN_DESIGN_TURNS = 3


_TOOL_STATUS = {
    "generate_code": "코드를 생성하고 있어요...",
    "generate_blockly_xml": "블록 코드를 준비하고 있어요...",
    "set_modi_layout": "모디 모듈 배치를 정하고 있어요...",
    "edit_code": "코드를 수정하고 있어요...",
    "plan_tasks": "태스크를 계획하고 있어요...",
    "complete_task": "태스크를 마무리하고 있어요...",
    "update_diagram": "다이어그램을 그리고 있어요...",
    "update_design_doc": "설계 문서를 작성하고 있어요...",
    "add_learning_note": "학습 노트를 정리하고 있어요...",
    "add_code_annotation": "코드 주석을 달고 있어요...",
    "web_search": "정보를 찾아보고 있어요...",
    "transition_phase": "다음 단계로 넘어가고 있어요...",
}


# ── 에이전트 스텝 로그용 매핑 ──
_TOOL_ACTION_MAP = {
    "generate_code": "write_file",
    "generate_blockly_xml": "write_blockly",
    "set_modi_layout": "modi_layout",
    "update_diagram": "diagram",
    "update_design_doc": "design",
    "plan_tasks": "plan",
    "complete_task": "task",
    "add_learning_note": "note",
    "edit_code": "edit",
    "add_code_annotation": "annotation",
}


def _tool_step_description(name: str, inp: dict, state=None) -> str:
    """도구 이름+입력으로 사람이 읽을 수 있는 스텝 설명 생성"""
    if name == "generate_code":
        fp = inp.get("file_path", "")
        fname = fp.rsplit("/", 1)[-1] if "/" in fp else fp
        return f"{fname} 작성" if fname else "코드 작성"
    if name == "generate_blockly_xml":
        return "블록 코드 생성"
    if name == "set_modi_layout":
        return "모디 모듈 배치"
    if name == "update_diagram":
        return "다이어그램 업데이트"
    if name == "update_design_doc":
        return "설계 문서 작성"
    if name == "plan_tasks":
        return "태스크 계획 생성"
    if name == "complete_task":
        tid = inp.get("task_id", "")
        task_name = ""
        if state and tid:
            task = next((t for t in state.project.task_plan.tasks if t.id == tid), None)
            if task:
                task_name = task.name
        if task_name:
            return f"태스크 완료: {task_name}"
        return f"태스크 {tid} 완료" if tid else "태스크 완료"
    if name == "add_learning_note":
        return inp.get("title", "학습 노트")[:30]
    if name == "edit_code":
        fp = inp.get("file_path", "")
        desc = inp.get("description", "")
        fname = fp.rsplit("/", 1)[-1] if "/" in fp else fp
        if desc and fname:
            short_desc = desc[:40] + "..." if len(desc) > 40 else desc
            return f"{fname}: {short_desc}"
        if fname:
            return f"{fname} 수정"
        return "코드 수정"
    if name == "add_code_annotation":
        return "코드 주석 추가"
    return name


class _StreamContext:
    """스트리밍 이벤트 처리 상태를 캡슐화."""

    def __init__(self):
        self.buffered_tokens: list[str] = []
        self.has_tool_use = False
        self._tool_name = ""
        self._tool_json = ""
        self._last_blockly_stage = ""
        self._logged_file = ""          # 현재 도구의 file_path 로그 중복 방지

    def handle(self, event) -> Generator[dict, None, None]:
        if event.type == "content_block_start":
            yield from self._on_block_start(event.content_block)
        elif event.type == "content_block_delta":
            yield from self._on_delta(event.delta)
        elif event.type == "content_block_stop":
            self._on_block_stop()

    def _on_block_start(self, block) -> Generator[dict, None, None]:
        if block.type == "text":
            return
        if block.type != "tool_use":
            return
        # 도구 호출 전후의 모델 텍스트는 버리지 않고 buffered_tokens에 계속 쌓아, 최종 응답(채팅 버블)으로
        # 내보낸다. (예전엔 도구 전 텍스트를 휘발성 status로만 흘리고 도구 후 텍스트는 버려서,
        # "방금 답 확인" 멘트가 사라지고 별도 라운드의 형식적 멘트만 남았음.)
        self.has_tool_use = True
        self._tool_name = block.name
        self._tool_json = ""
        self._last_blockly_stage = ""
        self._logged_file = ""
        msg = _TOOL_STATUS.get(self._tool_name, f"{self._tool_name} 실행 중...")
        kind = "status" if self._tool_name in _TOOL_STATUS else "log"
        yield {"type": kind, "message": msg}

    def _on_delta(self, delta) -> Generator[dict, None, None]:
        if delta.type == "text_delta":
            self.buffered_tokens.append(delta.text)
            return

        if delta.type != "input_json_delta":
            return

        self._tool_json += delta.partial_json

        if self._tool_name in ("generate_code", "edit_code"):
            verb = "작성" if self._tool_name == "generate_code" else "수정"
            yield from self._file_progress(verb)
        elif self._tool_name == "generate_blockly_xml":
            yield from self._blockly_progress()

    def _file_progress(self, verb: str) -> Generator[dict, None, None]:
        """generate_code / edit_code 공통: file_path 추출 후 진행 상태 표시."""
        if not self._logged_file:
            m = re.search(r'"file_path"\s*:\s*"([^"]+)"', self._tool_json)
            if m:
                self._logged_file = m.group(1)
                yield {"type": "status", "message": f"{self._logged_file} {verb} 중..."}

    def _blockly_progress(self) -> Generator[dict, None, None]:
        if '"grid"' in self._tool_json:
            stage = "모듈 배치를 잡고 있어요..."
        elif '"flowchart"' in self._tool_json:
            stage = "흐름도를 그리고 있어요..."
        elif len(self._tool_json) > 200:
            stage = "블록 코드를 만들고 있어요..."
        else:
            return
        if stage != self._last_blockly_stage:
            self._last_blockly_stage = stage
            yield {"type": "status", "message": stage}

    def _on_block_stop(self) -> None:
        """도구 블록 종료. 코드는 검증 완료 후 `code_validated`로만 프론트에 보낸다."""
        self._tool_name = ""
        self._tool_json = ""


def _parse_json_object(text: str) -> dict:
    """LLM 응답에서 JSON 객체만 안전하게 추출해 파싱한다.

    코드펜스(```), 앞뒤 설명 텍스트가 섞여 있어도 첫 '{'~마지막 '}'만 잘라 파싱한다.
    """
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
    return json.loads(text[start:end + 1])


class StreamOrchestrator:
    def __init__(self, api_key: str, session_id: str = ""):
        self.client = create_client(api_key)
        self.session_id = session_id  # Langfuse 세션 묶기용 (server에서 주입)
        self._user_id = ""  # 익명 디바이스 uuid (server의 X-User-Id 헤더에서 주입)
        self._reuse_flag = None  # #44 이번 턴 재사용 게이팅 결과(관측/클라이언트 표시)
        self._reuse_candidate = None  # #84 이번 턴 재사용 최상위 후보(직접서브 판정용 payload)
        self._direct_served = None  # #84 이번 턴 직접서브 만족도 검증 결과(관측/코호트)
        self._reuse_tier = ""       # 이번 턴 재사용 티어(direct_serve|near|cold) — 절감 분석용
        # 이번 턴이 "어떤 질문이었나" — usage_turns 에 실어 질문 유형별 비용/성공률을 가른다.
        # 이미 매 턴 _classify_turn_intent 가 계산하는 값이라 분류기를 새로 돌리지 않는다.
        self._turn_intent = ""      # question|chat|modify_request|implement_request|...
        self._ontology_primed = None  # #27 이번 턴 온톨로지 프라임 주입 결과(관측/검증)
        self.state = SessionState()
        self.router = Router(self.client)
        self.model_fast = HAIKU
        self.model_heavy = HAIKU
        self._cancelled = False
        # 현재 진행 중인 LLM 스트림(S7). /chat/stop 이 다른 스레드에서 이걸 kill 해
        # 블록된 서브프로세스 읽기까지 즉시 깨운다. (없으면 플래그만으로는 안 멈춤)
        self._active_stream = None
        # 입력 가드레일 차단 누적 횟수 — 유도 메시지를 변형 풀에서 돌려 "같은 말 반복" 어색함 방지
        self._block_count = 0
        # 마지막 _llm_call이 도구를 사용했는지 (루프 조기 종료 판단용)
        self._last_had_tools = False
        # 에이전트 스텝 추적
        self._step_count = 0
        self._turn_start_time = 0.0
        self._turn_steps: list[dict] = []

    def cancel(self):
        """현재 진행 중인 스트리밍을 취소합니다.

        플래그만 세우면 서브프로세스가 출력 대기로 블록됐을 때 안 멈추므로(S7),
        활성 스트림이 kill 을 지원하면(=CLI) 진행 중인 claude 프로세스를 직접 종료한다.
        """
        self._cancelled = True
        stream = self._active_stream
        killer = getattr(stream, "kill", None)
        if callable(killer):
            try:
                killer()
            except Exception:
                pass

    def _start_step(self, description: str, action: str) -> dict:
        """스텝 시작 (running 상태) — 도구 실행 전에 yield"""
        self._step_count += 1
        step = {
            "type": "agent_step",
            "step": self._step_count,
            "description": description,
            "action": action,
            "status": "running",
        }
        self._turn_steps.append(step)
        return step

    def _complete_step(self, status: str = "success") -> dict:
        """마지막 스텝 완료 — 도구 실행 후에 yield"""
        if self._turn_steps:
            self._turn_steps[-1]["status"] = status
        return {
            "type": "agent_step_update",
            "step": self._step_count,
            "status": status,
        }

    def _emit_step(self, description: str, action: str, status: str = "success") -> dict:
        """즉시 완료되는 스텝 (서브 에이전트 등). 프론트 카드 + Langfuse 이벤트(타임라인)에 동시 기록."""
        self._step_count += 1
        step = {
            "type": "agent_step",
            "step": self._step_count,
            "description": description,
            "action": action,
            "status": status,
        }
        self._turn_steps.append(step)
        # LLM 후처리 단계(설계추출/노트/주석/흐름도)는 이제 각자 generation으로 트레이스에 뜨므로
        # 이벤트를 또 남기면 중복. LLM 없는 로컬 단계(빌드검증=verify / 모디=modules)와 실패만 이벤트로.
        if action in _LOCAL_STEP_ACTIONS or status == "error":
            try:
                get_client().create_event(
                    name=f"단계 · {description}",
                    metadata={"action": action, "status": status, "step": self._step_count},
                    level="ERROR" if status == "error" else "DEFAULT",
                )
            except Exception:
                pass
        return step

    def _extract_summary(self) -> str:
        """AI 응답의 첫 문장을 요약으로 추출"""
        from agent.context import _extract_text, _is_tool_use
        for msg in reversed(self.state._messages):
            if msg["role"] == "assistant":
                if msg.get("_internal"):
                    continue  # 폐기 마커 등 내부 메시지가 요약으로 노출되는 것 방지
                if any(_is_tool_use(b) for b in msg["content"]):
                    continue
                for block in msg["content"]:
                    text = _extract_text(block)
                    if text:
                        text = text.strip()
                        for delim in ['\n', '. ', '! ', '? ']:
                            idx = text.find(delim)
                            if 0 < idx < 80:
                                return text[:idx + 1].strip()
                        return text[:80].strip()
        actions = [s["description"] for s in self._turn_steps]
        return ", ".join(actions[:3]) + (" 등" if len(actions) > 3 else "")

    @staticmethod
    def _group_tool_uses(tool_uses: list) -> list[list]:
        """연속 동일 이름 tool_use를 그룹으로 묶는다."""
        if not tool_uses:
            return []
        groups = []
        current = [tool_uses[0]]
        for tu in tool_uses[1:]:
            if tu.name == current[0].name:
                current.append(tu)
            else:
                groups.append(current)
                current = [tu]
        groups.append(current)
        return groups

    def _run_tool(self, tool_use) -> tuple[dict, bool]:
        """단일 도구 실행: Langfuse 관찰 래핑 + 에러 기록 → (tool_result, is_error)."""
        _desc = _tool_step_description(tool_use.name, tool_use.input, self.state)
        with get_client().start_as_current_observation(
                name=f"도구 · {_desc}", as_type="tool", input=tool_use.input) as ts:
            result = handle_tool_call(tool_use.name, tool_use.input, self.state)
            is_error = _is_tool_error(result)
            ts.update(output=result, level="ERROR" if is_error else "DEFAULT",
                      status_message=result[:200] if is_error else None,
                      metadata={"tool": tool_use.name, "phase": self.get_phase(),
                                "result_chars": len(result) if isinstance(result, str) else None})
        self._record_tool(tool_use.name, is_error)
        return {"type": "tool_result", "tool_use_id": tool_use.id, "content": result}, is_error

    @staticmethod
    def _group_description(group: list) -> str:
        """그룹화된 도구 호출의 사람이 읽을 수 있는 설명."""
        file_names = []
        for tu in group:
            fp = tu.input.get("file_path", "")
            fname = fp.rsplit("/", 1)[-1] if "/" in fp else fp
            if fname and fname not in file_names:
                file_names.append(fname)
        if file_names:
            return f"{file_names[0]} 외 {len(file_names)-1}개 파일 수정" if len(file_names) > 1 else f"{file_names[0]} 수정"
        return f"{group[0].name} x{len(group)}"

    def _execute_tool_groups(self, tool_uses: list) -> Generator[dict, None, None]:
        """tool_use 목록을 그룹으로 묶어 순차 실행하고 결과를 state에 저장."""
        tool_results = []
        for group in self._group_tool_uses(tool_uses):
            action = _TOOL_ACTION_MAP.get(group[0].name, group[0].name)
            if len(group) == 1:
                tu = group[0]
                yield self._start_step(_tool_step_description(tu.name, tu.input, self.state), action)
                result, is_error = self._run_tool(tu)
                tool_results.append(result)
                yield self._complete_step("error" if is_error else "success")
            else:
                yield self._start_step(self._group_description(group), action)
                errors = 0
                for tu in group:
                    result, is_error = self._run_tool(tu)
                    tool_results.append(result)
                    if is_error:
                        errors += 1
                yield self._complete_step("error" if errors else "success")
        self.state.add_tool_results(tool_results)

    def _get_model(self) -> str:
        if self.state.project.phase == Phase.IMPLEMENT:
            return self.model_heavy
        return self.model_fast

    def _build_design_doc_str(self) -> str:
        """설계 문서를 시스템 프롬프트용 문자열로 직렬화."""
        doc = self.state.project.design_doc
        if not doc.features and not doc.pages:
            return "(설계 문서 없음)"
        parts = []
        if doc.project_name:
            parts.append(f"프로젝트: {doc.project_name}")
        if doc.users:
            parts.append(f"사용자: {', '.join(doc.users)}")
        if doc.features:
            parts.append("기능: " + ", ".join(f.name for f in doc.features))
        if doc.pages:
            parts.append("페이지: " + ", ".join(f"{p.name}({p.description})" for p in doc.pages))
        if doc.data_models:
            parts.append("데이터: " + ", ".join(f"{d.name}[{','.join(d.fields)}]" for d in doc.data_models))
        if doc.strengths:
            parts.append("강점: " + ", ".join(doc.strengths))
        if doc.weaknesses:
            parts.append("약점: " + ", ".join(doc.weaknesses))
        return "\n".join(parts)

    def _implement_base_prompt(self, mode: str, diagram: str) -> str:
        """React/web 구현 단계 베이스 프롬프트 (quick=즉시 / design=설계·태스크 컨텍스트 포함).

        #67 T1: 정적 규칙(IMPLEMENT_SYSTEM_PROMPT)과 매 턴 바뀌는 컨텍스트(설계/문서/태스크/스택)
        사이에 CACHE_BOUNDARY 를 끼워, 정적 프리픽스만 프롬프트 캐시로 재사용되게 한다.
        """
        if mode == "quick":
            return QUICK_IMPLEMENT_PROMPT  # 정적 — 경계는 _get_system_prompt 가 말미에 붙임
        task_progress = self.state.project.task_plan.progress_summary() if self.state.project.task_plan.tasks else "(태스크 미생성 — plan_tasks를 먼저 호출하세요)"
        # .format 금지: 프롬프트 본문에 JS 예시의 리터럴 중괄호({, })가 있어 str.format이
        # "unexpected '{' in field name"으로 깨진다. placeholder가 4개뿐이라 replace로 치환한다.
        context = (
            IMPLEMENT_CONTEXT_TEMPLATE
            .replace("{diagram}", diagram)
            .replace("{design_doc}", self._build_design_doc_str())
            .replace("{task_progress}", task_progress)
            .replace("{tech_stack}", self.state.project.tech_stack or "학습자가 아직 선택하지 않음")
        )
        return IMPLEMENT_SYSTEM_PROMPT + CACHE_BOUNDARY + context

    def _verify_prompt(self, diagram: str, files: str) -> str:
        """검증 단계 프롬프트. #67 T1: 정적 규칙 + CACHE_BOUNDARY + 동적 컨텍스트(설계/파일)."""
        context = VERIFY_CONTEXT_TEMPLATE.replace("{diagram}", diagram).replace("{files}", files)
        return VERIFY_SYSTEM_PROMPT + CACHE_BOUNDARY + context

    def _hybrid_implement_prompt(self, mode: str, diagram: str) -> str:
        """하이브리드 전용 구현 프롬프트. web/react 규칙과 섞지 않는다."""
        if mode == "quick":
            return HYBRID_QUICK_IMPLEMENT_PROMPT
        task_progress = self.state.project.task_plan.progress_summary() if self.state.project.task_plan.tasks else "(태스크 미생성 — plan_tasks를 먼저 호출하세요)"
        return (
            HYBRID_IMPLEMENT_PROMPT
            .replace("{diagram}", diagram)
            .replace("{design_doc}", self._build_design_doc_str())
            .replace("{task_progress}", task_progress)
        )

    def _get_system_prompt(self) -> str:
        # 모든 phase/모드 프롬프트 앞에 안전 규칙을 붙이는 단일 주입 지점.
        # (입력 분류기가 못 막는 '생성 정책' — 예: 주민번호 앱 만들기 — 을 여기서 차단)
        prompt = SAFETY_ADDENDUM + "\n\n" + self._base_system_prompt()
        # #67 T1: 캐시 경계 보장. IMPLEMENT/VERIFY 는 이미 정적/동적 사이에 CACHE_BOUNDARY 를
        # 갖는다. 그 외 정적 전용 프롬프트(DESIGN·blockly·quick·no_code)는 경계가 없으므로
        # 말미에 붙여, 호출부가 이어 붙이는 동적 꼬리(코드 컨텍스트·재사용 블록)가 캐시 밖에
        # 놓이게 한다. (경계가 없으면 그 꼬리까지 정적 캐시에 섞여 매 턴 프리픽스가 깨진다.)
        if CACHE_BOUNDARY not in prompt:
            prompt += CACHE_BOUNDARY
        return prompt

    def _base_system_prompt(self) -> str:
        phase = self.state.project.phase
        diagram = self.state.diagram_manager.get_mermaid()
        files = self.state.get_files_summary()
        mode = getattr(self, '_current_mode', 'design')
        coding_type = getattr(self, '_coding_type', 'react')

        if coding_type == 'blockly':
            if phase == Phase.DESIGN:
                return BLOCKLY_DESIGN_PROMPT
            elif phase == Phase.IMPLEMENT:
                modi_core = load_modi_core()
                # .replace 사용: 프롬프트에 JSON 예시({"motor_b":180} 등) 리터럴 중괄호가 있어
                # str.format은 KeyError를 낸다. placeholder는 {modi_core} 하나뿐이라 replace로 충분.
                return BLOCKLY_IMPLEMENT_PROMPT.replace("{modi_core}", modi_core)
            else:
                return self._verify_prompt(diagram, files)

        # hybrid(소프트웨어+하드웨어): web/react 프롬프트와 분리된 전용 생성 프롬프트를 사용한다.
        if coding_type == 'hybrid':
            if phase == Phase.DESIGN:
                return DESIGN_SYSTEM_PROMPT + HYBRID_DESIGN_ADDENDUM
            elif phase == Phase.IMPLEMENT:
                return self._hybrid_implement_prompt(mode, diagram)
            else:
                return self._verify_prompt(diagram, files)

        if phase == Phase.DESIGN:
            return DESIGN_SYSTEM_PROMPT
        elif phase == Phase.IMPLEMENT:
            return self._implement_base_prompt(mode, diagram)
        elif phase == Phase.VERIFY:
            return self._verify_prompt(diagram, files)
        return DESIGN_SYSTEM_PROMPT

    @observe(name="채팅 턴 (chat_turn)", as_type="chain", capture_input=False, capture_output=False)
    def chat_stream(self, user_input: str, mode: str = "design", coding_type: str = "react", runtime_error: str = "") -> Generator[dict, None, None]:
        """채팅 한 턴 = Langfuse trace 루트. 세션 속성을 전파하고 실제 로직은 _chat_stream_impl에 위임."""
        lf = get_client()
        # 입력 가드레일 1차: 구조적 PII 마스킹 — 가장 먼저 적용해 이후 로깅·저장·LLM이 모두
        # 가려진 값을 쓰게 한다(redact-at-source). 이 줄 덕분에 아래 Langfuse 입력도 가려진 값으로 남는다.
        user_input = redact_pii(user_input)
        self._reuse_flag = None  # #44 턴마다 초기화(이전 턴 재사용 결과 누수 방지)
        self._reuse_candidate = None  # #84 턴마다 초기화(직접서브 후보 누수 방지)
        self._direct_served = None  # #84 턴마다 초기화(직접서브 판정 누수 방지)
        self._reuse_tier = ""       # 같은 이유 — 이전 턴 티어가 새 턴에 새면 절감 분석이 틀어진다
        self._turn_intent = ""      # 같은 이유 — 이전 턴 의도가 새 턴에 새면 유형별 집계가 틀어진다
        self._reuse_seeded = False  # #EDU-27 턴마다 초기화(재사용 코드 시드 여부)
        self._ontology_primed = None  # #27 턴마다 초기화(이전 턴 프라임 누수 방지)
        active_model = self._get_model()
        model_short = active_model.split("-")[1] if active_model.startswith("claude-") else active_model
        # 트레이스 이름은 비교 축(모델 · coding_type · mode)만 — 유저 입력은 Input 컬럼에 이미 있음.
        trace_name = f"{model_short} · {coding_type} · {mode}"
        lf.update_current_span(input={"message": user_input, "mode": mode, "coding_type": coding_type, "model": active_model})
        # tags(필터용) + metadata(대시보드 group-by용) 양쪽에 coding_type/mode/model을 넣어
        # "react vs blockly", "quick vs design", "haiku vs sonnet vs opus"로 비용·지연을 쪼개 본다.
        with propagate_attributes(session_id=self.session_id or None, user_id=self._user_id or None,
                                  trace_name=trace_name,
                                  tags=[coding_type, mode, active_model],
                                  metadata={"coding_type": coding_type, "mode": mode, "model": active_model}):
            try:
                yield from self._chat_stream_impl(user_input, mode, coding_type, runtime_error)
            except Exception as e:
                # 턴 전체 실패 → trace를 ERROR로 마킹(대시보드 error rate 집계).
                lf.update_current_span(level="ERROR", status_message=str(e)[:500])
                raise
            finally:
                # 루트에 "이 턴이 뭘 만들었나" 요약 — 클릭 안 해도 한눈에 결과가 보이게.
                code_map = self.state.generated_code_map or {}
                lf.update_current_span(
                    output={
                        "phase": self.get_phase(),
                        "model": self._get_model(),
                        "files": list(code_map.keys()),
                        "file_count": len(code_map),
                        "learning_notes": len(self.state.learning_notes),
                        "code_annotations": len(self.state.code_annotations),
                    },
                    # #67 T3: done 이벤트의 재사용/온톨로지 결과를 trace 메타데이터로 전파해
                    # Langfuse 에서 코호트 분리·필터가 가능하게 한다(기존엔 프론트로만 나갔음).
                    metadata={
                        "reused": self._reuse_flag,
                        "direct_served": self._direct_served,
                        "ontology_primed": self._ontology_primed,
                    },
                )
                self._emit_turn_scores(mode)

    # 빌드(산출물 생성) 성공률 점수용 도구 — 이게 에러 없이 돌면 빌드 성공으로 본다.
    _BUILD_TOOLS = {"generate_code", "generate_blockly_xml", "edit_code"}

    def _record_tool(self, name: str, is_error: bool) -> None:
        """턴 동안 도구 호출/에러를 집계 — 턴 종료 시 Langfuse 점수로 부착."""
        self._turn_tool_calls += 1
        if is_error:
            self._turn_tool_errors += 1
        if name in self._BUILD_TOOLS:
            self._turn_build_attempts += 1
            if is_error:
                self._turn_build_errors += 1

    def _emit_turn_scores(self, mode: str) -> None:
        """턴 종료 시 휴리스틱 품질 점수를 현재 trace에 부착(유저 피드백 제외).

        - 도구 호출수 (tool_calls): 이 턴의 도구 호출 수 (NUMERIC)
        - 도구 에러율 (tool_error_rate): 도구 에러 비율 0~1 (도구를 쓴 턴만, NUMERIC)
        - 빌드 성공 (build_success): 코드/블록 생성이 에러 없이 됐는지 (빌드 시도한 턴만, BOOLEAN)
        - 설계 협의 횟수 (design_turns): 설계 협의 누적 핑퐁 수 (design 모드만, NUMERIC)
        """
        try:
            lf = get_client()
            calls = getattr(self, "_turn_tool_calls", 0)
            lf.score_current_trace(name="도구 호출수 (tool_calls)", value=calls, data_type="NUMERIC")
            if calls:
                rate = round(getattr(self, "_turn_tool_errors", 0) / calls, 3)
                lf.score_current_trace(name="도구 에러율 (tool_error_rate)", value=rate, data_type="NUMERIC")
            if getattr(self, "_turn_build_attempts", 0) or getattr(self, "_turn_artifact_rejected", False):
                ok = (
                    getattr(self, "_turn_build_errors", 0) == 0
                    and not getattr(self, "_turn_artifact_rejected", False)
                )
                lf.score_current_trace(name="빌드 성공 (build_success)", value=1 if ok else 0, data_type="BOOLEAN")
            # 도구 미호출 실패 계측 — 대시보드에서 모델·프롬프트 변경 전후 실패율 비교용
            if getattr(self, "_turn_code_retries", 0):
                lf.score_current_trace(name="코드생성 재시도수 (code_retries)", value=self._turn_code_retries, data_type="NUMERIC")
            if getattr(self, "_turn_code_salvaged", 0):
                lf.score_current_trace(name="코드 복구 파일수 (code_salvaged)", value=self._turn_code_salvaged, data_type="NUMERIC")
            if getattr(self, "_turn_code_failed", False):
                lf.score_current_trace(name="코드 미생성 (no_code)", value=1, data_type="BOOLEAN")
            if getattr(self, "_turn_modify_failed", False):
                lf.score_current_trace(name="수정 미적용 (modify_no_change)", value=1, data_type="BOOLEAN")
            if getattr(self, "_turn_modify_clarified", False):
                lf.score_current_trace(name="수정 대신 질문 (modify_clarified)", value=1, data_type="BOOLEAN")
            # #67 T2: 출력 상한 truncation — 상한을 내렸을 때 품질 회귀(잘림)를 대시보드에서 감시.
            if getattr(self, "_turn_output_truncated", False):
                lf.score_current_trace(name="출력 잘림 (output_truncated)", value=1, data_type="BOOLEAN")
            # #70 P2: 후처리 서브콜(설계·노트·주석) 출력 잘림 — 후처리 상한을 내렸을 때 회귀 감시.
            if getattr(self, "_turn_post_truncated", False):
                lf.score_current_trace(name="후처리 출력 잘림 (post_output_truncated)", value=1, data_type="BOOLEAN")
            # #68 O1: 출력 토큰 구성 — 전체재작성(generate_code) vs 부분수정(edit_code) vs 산문.
            # 매 코드 턴 무조건 찍어(어느 하나라도 출력이 있으면) 코호트가 항상 분리되게 한다.
            gen_tok = getattr(self, "_turn_out_generate", 0)
            edit_tok = getattr(self, "_turn_out_edit", 0)
            prose_tok = getattr(self, "_turn_out_prose", 0)
            if gen_tok or edit_tok or prose_tok or getattr(self, "_turn_out_other", 0):
                lf.score_current_trace(name="출력 전체재작성 토큰 (output_generate_tokens)", value=gen_tok, data_type="NUMERIC")
                lf.score_current_trace(name="출력 부분수정 토큰 (output_edit_tokens)", value=edit_tok, data_type="NUMERIC")
                lf.score_current_trace(name="출력 산문 토큰 (output_prose_tokens)", value=prose_tok, data_type="NUMERIC")
            # 수정 턴 전용 코호트: 이미 코드가 있는데 전체재작성으로 나간 출력 = O2 가 없애려는 낭비.
            # (콜드 빌드의 정상 generate_code 와 섞이지 않게 분리 — "수정 턴 전체재작성 출력 토큰".)
            if getattr(self, "_turn_had_code_at_start", False):
                lf.score_current_trace(name="수정턴 전체재작성 출력 (edit_turn_full_rewrite_tokens)", value=gen_tok, data_type="NUMERIC")
                lf.score_current_trace(name="수정턴 부분수정 출력 (edit_turn_edit_tokens)", value=edit_tok, data_type="NUMERIC")
            # #67 T3: 재사용/온톨로지 코호트 스코어 — Langfuse 에서 reuse/review/register·프라임
            # 여부로 비용·지연을 쪼개 보게 한다(TAU 튜닝의 전제). 매 턴 무조건 찍어 코호트가
            # 항상 분리되게(안 찍으면 "미측정"과 "미발동"을 구분 못 함).
            reuse_decision = (self._reuse_flag or {}).get("decision") or "none"
            lf.score_current_trace(name="재사용 결정 (reuse_decision)", value=reuse_decision,
                                   data_type="CATEGORICAL")
            # #84 후속 near-miss 계측: 게이트 검색이 돈 턴은 판정과 무관하게 top1(검색 전체 최고점)
            # 과 cand_score(최상위 code 후보 점수)를 남긴다 — 실트래픽이 재사용/직접서브 근처에
            # 오는지(임계 하향 여지)를 분포로 판단하는 유일한 데이터.
            _rf = self._reuse_flag or {}
            if _rf.get("top1") is not None:
                lf.score_current_trace(name="재사용 top1 (reuse_top1)",
                                       value=float(_rf["top1"]), data_type="NUMERIC")
            if _rf.get("cand_score") is not None:
                lf.score_current_trace(name="재사용 후보점수 (reuse_cand_score)",
                                       value=float(_rf["cand_score"]), data_type="NUMERIC")
            # EDU-27: combined(con 인플레)만으로는 진짜 재사용(vec .60+)과 오탐(vec .44)을 못 갈라
            # vec 성분으로 review→reuse 승격했는지 코호트로 본다(실트래픽 22건 검증, 2026-07-08).
            if _rf.get("vec_promoted") is not None:
                lf.score_current_trace(name="재사용 vec승격 (reuse_vec_promoted)",
                                       value=1 if _rf.get("vec_promoted") else 0,
                                       data_type="BOOLEAN")
            lf.score_current_trace(name="온톨로지 프라임 (ontology_primed)",
                                   value=1 if self._ontology_primed else 0, data_type="BOOLEAN")
            # #84 3-tier 라우팅 코호트 — direct_serve(델타無 저장물 서브) / near(reuse·review 프라임+생성)
            # / cold(재사용 후보 없음). 실트래픽에서 tier별 비용·속도·만족도를 쪼개 본다.
            ds = self._direct_served
            if ds and ds.get("accept"):
                tier = "direct_serve"
            elif reuse_decision in ("reuse", "review"):
                tier = "near"
            else:
                tier = "cold"
            # 관측(Langfuse)뿐 아니라 사용량 원장(usage_turns)에도 실어야 한다 —
            # "재사용으로 비용이 얼마나 줄었나"는 티어별 단가를 대조해야만 나오고,
            # Langfuse 는 보존기간이 있어 청구 근거로 쓰기 어렵다.
            self._reuse_tier = tier
            lf.score_current_trace(name="재사용 티어 (reuse_tier)", value=tier, data_type="CATEGORICAL")
            # 직접서브 만족도 검증이 실제로 돈 턴만: served(=생성LLM 0으로 서브했는가) + 만족도 점수.
            if ds is not None:
                lf.score_current_trace(name="직접서브 (direct_served)",
                                       value=1 if ds.get("accept") else 0, data_type="BOOLEAN")
                if ds.get("score") is not None:
                    lf.score_current_trace(name="직접서브 만족도 (direct_serve_score)",
                                           value=ds.get("score"), data_type="NUMERIC")
                # EDU-27 #92: 문서 복원 수 — metadata 에만 있으면 Langfuse UI 필터·집계가
                # 안 되므로 스코어로도 발행(코호트 측정 lf_cohort.py 와 대시보드 관측용).
                if ds.get("docs_restored") is not None:
                    lf.score_current_trace(name="직접서브 문서복원 (docs_restored)",
                                           value=int(ds["docs_restored"]), data_type="NUMERIC")
            if mode == "design":
                lf.score_current_trace(name="설계 협의 횟수 (design_turns)", value=self.state.design_turns, data_type="NUMERIC")
        except Exception:
            # 점수 부착 실패가 응답 흐름을 깨면 안 된다.
            pass

    def _flag_blocked(self, category: str) -> None:
        """입력 가드레일이 차단한 턴을 Langfuse에 플래그(검토용). 실패해도 응답 흐름은 유지."""
        try:
            lf = get_client()
            # chat_stream이 먼저 기록한 입력을 덮어써, 차단된 원문(정규식이 못 가린 의미적 PII 포함)이
            # trace에 남지 않게 한다.
            lf.update_current_span(input={"message": f"[blocked: {category}]"},
                                   level="WARNING", status_message=f"input blocked: {category}")
            lf.score_current_trace(name="안전 차단 (safety_block)", value=1,
                                   data_type="BOOLEAN", comment=category)
        except Exception:
            pass

    def _flag_classifier_error(self) -> None:
        """입력 분류기가 fail-open(에러/타임아웃)한 턴을 Langfuse 점수로 남긴다 — 분류기 건강 모니터링용.

        차단은 아니고(통과시킴) 기록만 한다. 이 점수가 자주 1로 찍히면 분류기가 무음으로
        뚫리고 있다는 신호이므로 운영자가 대시보드에서 잡을 수 있다.
        """
        try:
            get_client().score_current_trace(
                name="가드레일 분류 실패 (guardrail_classify_error)", value=1, data_type="BOOLEAN")
        except Exception:
            pass

    def _add_turn_usage(self, usage) -> None:
        """CLI dict / SDK Usage 어느 쪽이든 usage_details() 를 거쳐 TokenUsage 로 턴 누적.

        getattr 기반 — _llm_call 이 턴 초기화(_chat_stream_impl) 밖에서 단독 호출되는 경로
        (테스트 등)도 안전(기존 _turn_out_* 누적과 동일 원칙 — "getattr 누적 —" 주석 참조).
        Langfuse 계측(usage_details/update_generation)은 그대로 두고 병행 — 여긴 실시간
        쿼터 판정용 로컬 집계만 더한다(#130).
        """
        details = _usage_details(usage)
        if details is None:
            return  # usage가 None(토큰 정보 없는 콜) → 누적 스킵(엣지 케이스 결정)
        current = getattr(self, "_turn_usage", TokenUsage())
        self._turn_usage = current + usage_from_details(details)

    def pop_turn_usage(self) -> TokenUsage:
        """턴 누적을 반환하고 0으로 리셋. 미누적 상태면 TokenUsage()."""
        usage = getattr(self, "_turn_usage", TokenUsage())
        self._turn_usage = TokenUsage()
        return usage

    def _chat_stream_impl(self, user_input: str, mode: str = "design", coding_type: str = "react", runtime_error: str = "") -> Generator[dict, None, None]:
        """
        yield 하는 이벤트 타입:
        - {"type": "status", "message": "..."} — 지금 뭐 하고 있는지
        - {"type": "token", "text": "..."} — LLM 응답 토큰
        - {"type": "tool_call", "name": "...", "description": "..."} — 도구 호출 시작
        - {"type": "tool_result", "name": "...", "result": "..."} — 도구 결과
        - {"type": "done", "phase": "...", "diagram": "...", "generated_code": {...}} — 완료

        mode: "design" = 설계부터, "quick" = 바로 만들기
        """

        self._cancelled = False
        self._current_mode = mode
        self._coding_type = coding_type
        self.state.coding_type = coding_type

        # 에이전트 스텝 초기화
        self._step_count = 0
        self._turn_start_time = time.time()
        self._turn_steps = []

        # Langfuse 점수/에러율 집계용 턴 카운터
        self._turn_tool_calls = 0
        self._turn_tool_errors = 0
        self._turn_build_attempts = 0
        self._turn_build_errors = 0
        self._turn_code_retries = 0  # 턴 계약 미이행으로 재시도된 횟수
        self._turn_code_salvaged = 0  # 텍스트 덤프에서 복구(salvage)된 파일 수
        self._turn_code_failed = False  # 신규 생성 계약 실패(코드 미생성) 턴
        self._turn_modify_failed = False  # 수정 계약 실패(변경 미적용) 턴
        self._turn_modify_clarified = False  # 수정 대신 요구사항 질문으로 완결된 턴
        self._turn_output_truncated = False  # #67 T2: 출력이 max_tokens 상한에 걸려 잘린 턴
        self._turn_post_truncated = False  # #70 P2: 후처리 서브콜 출력이 상한에 걸려 잘린 턴
        # #68 O1: 출력 토큰 구성(전체재작성 vs 부분수정 vs 산문) 턴 누적 — 수정 턴 재작성 낭비 계량.
        self._turn_out_generate = 0
        self._turn_out_edit = 0
        self._turn_out_prose = 0
        self._turn_out_other = 0
        # #130: 턴 단위 로컬 토큰 누적(쿼터 판정용, Langfuse 계측과 병행) — pop_turn_usage() 로 회수.
        self._turn_usage = TokenUsage()
        # 이 턴이 '수정 턴'인지(=진입 시 이미 산출물 코드가 있음) — 전체재작성 낭비 코호트 분리 기준.
        self._turn_had_code_at_start = bool(self.state.generated_code_map)
        self._turn_artifact_rejected = False  # 검증/빌드 실패 산출물을 롤백한 턴
        self._turn_user_message_index = None  # 실패 시 이번 턴 tool 히스토리만 제거하기 위한 기준점

        # 턴 시작: 델타 추적 + 검증 실패 롤백용 스냅샷 (검증 실패 산출물은 state에서 되돌린다 —
        # done 억제 플래그 방식은 다음 턴 done·세션 저장/복원으로 실패물이 새어 나갔다)
        self.state.begin_turn()

        # (입력 PII 마스킹은 chat_stream 진입부에서 이미 적용됨 — user_input은 가려진 값)

        # -1. coding_type 선택 메시지 처리 (첫 메시지)
        target_msg = user_input.strip()
        if not self.state._messages and target_msg in ("소프트웨어로 할게요", "하드웨어로 할게요", "소프트웨어+하드웨어로 할게요"):
            self.state.add_user_message(user_input)
            if coding_type == "blockly":
                reply = "좋아요, MODI로 만들어볼게요! 어떤 **동작**을 만들고 싶은지 설명해 주세요.\n예: \"버튼을 누르면 LED가 켜지게\""
            elif coding_type == "hybrid":
                reply = "좋아요, 웹 화면과 MODI가 함께 동작하는 걸 만들어볼게요! 어떤 상호작용을 원하는지 설명해 주세요.\n예: \"거리센서가 가까워지면 화면이 빨개지고, 웹 버튼을 누르면 LED가 켜지게\""
            else:
                reply = "좋아요, 웹·앱을 만들어볼게요! 무엇이든 편하게 설명해 주세요.\n진행 방식은 아래에서 언제든 바꿀 수 있어요."
            self.state.add_assistant_message([{"type": "text", "text": reply}])
            yield {"type": "token", "text": reply}
            yield self._done_event()
            return

        # 입력 가드레일 2차: 어린 학생 안전·적절성 분류 → 차단·유도.
        # 정규식이 못 잡는 의미·맥락(욕설/괴롭힘/자해)과 서술형 개인정보를 Haiku가 잡는다.
        # (위 coding_type 선택지는 고정 문구라 이 검사 전에 이미 return됨 → 분류 호출 절약.)
        with get_client().start_as_current_observation(
                name="가드레일 입력검사 (check_input)", as_type="generation",
                input={"text": user_input}) as _gen:
            verdict = check_input(self.client, user_input, self.model_fast)
            _update_generation(_gen, verdict.model or self.model_fast,
                               usage=verdict.usage, cost=verdict.cost_usd,
                               output={"category": verdict.category, "ok": verdict.ok},
                               step="guardrail_input", category=verdict.category)
            self._add_turn_usage(verdict.usage)  # #130: 가드레일 콜도 턴 누적에 합산
        if verdict.category == "error":
            # 분류기가 에러/타임아웃으로 통과(fail-open)된 턴 — 무음 실패 방지용으로 기록만 한다.
            self._flag_classifier_error()
        if not verdict.ok:
            # 유도 메시지는 변형 풀에서 누적 차단 횟수로 골라 매번 다른 문구가 나오게 한다.
            self._block_count += 1
            msg = pick_redirect(verdict.category, self._block_count)
            # 차단된 턴은 히스토리에 남기지 않는다 — LLM 컨텍스트·설계추출·디스크 저장 오염 방지.
            # (학생은 실시간 token으로 유도 메시지를 이미 봄. Langfuse 입력은 _flag_blocked가 덮어씀.)
            self._flag_blocked(verdict.category)
            yield {"type": "token", "text": msg}
            yield self._done_event()
            return

        # 0. 모호한 요청 처리 (설계 모드에서만)
        if mode == "design" and self.state.project.phase == Phase.DESIGN and not self.state._messages and is_vague_request(user_input):
            msg = get_suggestion_message()
            yield {"type": "token", "text": msg}
            yield self._done_event()
            return

        # 1. 라우팅
        yield {"type": "status", "message": "의도를 파악하고 있어요..."}

        intent = self._classify_turn_intent(user_input, mode)
        # 사용량 원장에 실을 수 있게 인스턴스에 남긴다 — "단순 질문이었나 코드 생성이었나"로
        # 비용·응답시간을 쪼개 보려면 턴마다 이 값이 필요하다(관측 전용, 흐름에 영향 없음).
        self._turn_intent = intent

        # quick 모드라도 먼저 intent를 본다. 질문/잡담 첫 턴을 IMPLEMENT phase로 밀어 넣으면
        # 산출물 없음 계약이 발동해 불필요한 구현·재시도를 시작한다.
        if (mode == "quick" and self.state.project.phase == Phase.DESIGN
                and self._intent_uses_code_tools_for_phase(intent, Phase.IMPLEMENT)):
            self.state.project.phase = Phase.IMPLEMENT

        # 2. Phase 전환
        if intent == "phase_change":
            prev_phase = self.state.project.phase
            result = self._handle_phase_change(user_input)
            yield {"type": "token", "text": result}
            # 설계→구현 전환에 성공하면 같은 턴에 구현까지 진행한다(유저가 "만들자"로 확인한 제어된 빌드).
            # 그 외(전환 실패/다른 phase 전환)는 메시지만 내고 종료.
            if prev_phase == Phase.DESIGN and self.state.project.phase == Phase.IMPLEMENT:
                intent = "implement_request"
            else:
                yield self._done_event()
                return

        # 3. 컨텍스트 요약
        if len(self.state._messages) > 30:
            yield {"type": "status", "message": "대화 내용을 정리하고 있어요..."}
            self.state.summarize_if_needed(self.client)

        # 4. 메시지 추가
        self.state.add_user_message(user_input)
        self._turn_user_message_index = len(self.state._messages) - 1

        # 5. 에이전트 루프 (스트리밍)
        no_code_turn = intent in _NO_CODE_INTENTS
        system_prompt = self._no_code_system_prompt() if no_code_turn else self._get_system_prompt()

        # 경량(텍스트만) 히스토리 사용 조건:
        # - 설계 phase: 도구 호출/결과 노이즈를 빼고 깔끔한 Q&A만 모델에 줘야 "방금 답"을 제대로 인지함
        #   (노이즈가 끼면 방금 답을 못 보고 같은 걸 또 묻는 현상 발생)
        # - 구현 phase(코드 있음): 입력 토큰 절감
        # bool() 필수: and/or는 피연산자 값을 반환하므로 괄호식이 코드맵 dict 자체가 되어
        # Langfuse 메타데이터(compact 필드)로 코드 전체가 유출됐었다.
        use_compact = bool(
            self.state.project.phase == Phase.DESIGN
            or (self.state.project.phase == Phase.IMPLEMENT and self.state.generated_code_map)
        )
        code_action_intent = self._intent_uses_code_tools(intent)
        # 구현 phase에 코드가 있으면 현재 코드를 시스템 프롬프트에 주입 (설계엔 코드 없음).
        # 단순 chat/clarify_request에는 전체 코드를 싣지 않는다 — 비용을 줄이고 "수정해야 한다"는
        # 압력을 낮춘다. question은 현재 앱 설명이 필요할 수 있어 코드 컨텍스트를 유지한다.
        if self.state.project.phase == Phase.IMPLEMENT and self.state.generated_code_map:
            if code_action_intent or intent == "question":
                system_prompt = system_prompt + "\n\n" + self.state.get_current_code_context()
            if code_action_intent:
                # 프론트가 보내준 미리보기 런타임 에러 — 수정 턴의 블라인드 디버깅을 없애는 근거.
                # 질문/잡담 턴에는 주입하지 않는다(오래된 런타임 에러가 대화를 수정으로 끌고 감).
                if runtime_error:
                    system_prompt += (
                        "\n\n## 미리보기 런타임 에러 (수정 근거)\n"
                        "미리보기 실행 중 아래 에러가 발생했습니다. 수정 요청과 관련되면 이 에러를 우선 해결하세요.\n"
                        f"```\n{runtime_error[:2000]}\n```"
                    )
                # 도구 계약은 코드 '뒤'에 다시 — 상단 규칙은 수만 자 코드에 묻힌다
                system_prompt = system_prompt + "\n\n" + CODE_TOOL_CONTRACT
                # #68 O2: 수정 턴(기존 코드 있음)은 전체재작성 대신 edit_code 유도 지시를 말단에 덧붙인다.
                # (implement_request 는 대개 첫 빌드라 제외 — 여기 진입 자체가 코드 있음이지만
                #  intent 로 구분해 신규 다중 파일 생성 흐름엔 이 diet 지시를 걸지 않는다.)
                if intent == "modify_request":
                    system_prompt = system_prompt + "\n\n" + MODIFY_EDIT_DIRECTIVE
            yield {"type": "status", "message": "코드를 수정하고 있어요..." if code_action_intent else "답변을 준비하고 있어요..."}
        else:
            yield {"type": "status", "message": "생각하고 있어요..."}

        # 설계 모드: 협의 턴 수를 센다(프롬프트의 "여러 턴 핑퐁" 가이드 참고용).
        if mode == "design" and self.state.project.phase == Phase.DESIGN:
            self.state.design_turns += 1

        # #44 재사용 게이팅: 명시적 구현 요청(implement_request) 빌드 직전 검색 → 프라임.
        # #27: 코드 생성 턴(implement_request·modify_request, IMPLEMENT phase)이면 빌드 직전
        # 온톨로지 프라임 + 코드 재사용 게이트. code_action_intent 는 phase==IMPLEMENT 를 이미 포함.
        # (기존엔 implement_request 만 걸러 quick 흐름의 modify_request 빌드에서 프라임이 안 붙었음 —
        #  실 sim 으로 발견. 기존 코드가 있는 modify 는 _reuse_block 이 조기 반환해 재프라임 안 함.)
        # #27: 코드 생성 턴(implement_request·modify_request, IMPLEMENT phase)이면 빌드 직전
        # 온톨로지 프라임 + 코드 재사용 게이트. code_action_intent 는 phase==IMPLEMENT 를 이미 포함.
        # (기존엔 implement_request 만 걸러 quick 흐름의 modify_request 빌드에서 프라임이 안 붙었음 —
        #  실 sim 으로 발견. 기존 코드가 있는 modify 는 _reuse_block 이 조기 반환해 재프라임 안 함.)
        if code_action_intent:
            block, msg = self._reuse_block(user_input, coding_type)
            # #84 직접서브 티어: reuse 고신뢰 후보를 값싼 만족도 검증 → accept 면 저장물을 그대로
            # 서브하고(생성 LLM=0) 이 턴을 종료한다. accept 아니면 아래 프라임 + 생성 경로로 폴백.
            served = yield from self._maybe_direct_serve(user_input)
            if served:
                yield self._code_validated_event()
                yield self._done_event()
                return
            if block:
                system_prompt = system_prompt + "\n\n" + block
                yield {"type": "status", "message": msg}

        # 첫 구현/생성은 1라운드. 설계 phase는 여러 라운드(도구 호출 뒤에도 질문/답변을 이어가도록),
        # 수정 요청도 여러 라운드.
        phase_before_loop = self.state.project.phase
        loops = 1 if no_code_turn else (3 if (intent == "modify_request" or self.state.project.phase == Phase.DESIGN) else 1)
        yield from self._agent_loop_stream(
            system_prompt, compact=use_compact, max_loops=loops,
            tools_override=self._tools_override_for_intent(intent),
            contract=self._implement_contract(intent),
        )

        if self._cancelled:
            yield {"type": "cancelled"}
            return

        # 설계 에이전트가 이번 턴에 transition_phase로 구현 전환했으면(=대화를 보고 "이제 만들자"라고
        # 판단), 같은 턴에 바로 구현을 생성한다. 설계 루프는 DESIGN 프롬프트/도구였으니 코드는 아직 없음.
        if phase_before_loop == Phase.DESIGN and self.state.project.phase == Phase.IMPLEMENT:
            system_prompt = self._get_system_prompt()  # 이제 IMPLEMENT 프롬프트
            # #2(#44): 설계→전환 빌드도 재사용 게이트 적용. 트리거 메시지("이제 만들자")가 아니라
            # 설계 목표(design_doc)로 검색해야 유사 결과물이 잡힌다.
            goal = (self.state.project.design_doc.description
                    or self.state.project.design_doc.project_name or user_input)
            block, msg = self._reuse_block(goal, coding_type)
            if block:
                system_prompt = system_prompt + "\n\n" + block
                yield {"type": "status", "message": msg}
            yield {"type": "status", "message": "좋아요, 이제 만들기 시작할게요..."}
            yield from self._agent_loop_stream(system_prompt, max_loops=1)

        # 코드 검증 + 빌드 체크 + 후처리(설계/노트/주석) — React 모드, 구현 phase에서 코드 변경 시
        if coding_type != 'blockly' and self.state.project.phase == Phase.IMPLEMENT and self.state.code_changed_this_turn():
            yield from self._post_impl_react(intent, system_prompt)

        # blockly: 구현 phase에서 메인 루프가 블록(XML)을 생성했을 때만 검증·후속 처리
        if (coding_type == 'blockly' and self.state.project.phase == Phase.IMPLEMENT
                and self.state.blockly_changed_this_turn()):
            yield from self._post_impl_blockly(system_prompt)

        yield self._done_event()

    def _classify_turn_intent(self, user_input: str, mode: str) -> str:
        """한 턴의 의도 분류.

        quick 첫 턴은 질문/잡담만 규칙으로 걸러낸 뒤 바로 구현으로 보낸다. 이미 코드/XML이
        있는 세션의 후속 발화는 규칙으로 명확한 것만 먼저 처리하고, 애매한 산출물 피드백은
        LLM classifier + policy gate로 판단한다.
        """
        # 명확한 규칙 케이스 + 애매한 기존-산출물 케이스(LLM classifier)는 #74 로직을 그대로 유지한다.
        # 시뮬레이터(/api/simulate)는 LLM 없이 prime_service.resolve_intent(규칙)로 이 흐름을 근사한다
        # — 애매한 기존-산출물 케이스만 규칙 폴백이라 그 경우에 한해 /chat 과 갈릴 수 있다(문서화).
        if mode == "quick" and not self._impl_artifact_ready():
            return self._classify_quick_initial_intent(user_input)

        known = self.router.classify_known(user_input, self.state.project.phase)
        if self.state.project.phase == Phase.IMPLEMENT and self._impl_artifact_ready():
            return classify_existing_artifact_intent(
                self.client,
                self.model_fast,
                user_input,
                known,
                phase=self.get_phase(),
                coding_type=getattr(self, "_coding_type", None) or self.state.coding_type or "",
                artifact_files=list((self.state.generated_code_map or {}).keys()),
                last_assistant_message=self._last_assistant_text(),
                is_clarification_answer=self._looks_like_clarification_answer(user_input),
            )

        if known:
            return known

        return self.router.classify(user_input, self.state.project.phase)

    def _intent_uses_code_tools(self, intent: str) -> bool:
        return self._intent_uses_code_tools_for_phase(intent, self.state.project.phase)

    def _intent_uses_code_tools_for_phase(self, intent: str, phase: Phase) -> bool:
        return phase == Phase.IMPLEMENT and intent in _CODE_ACTION_INTENTS

    def _no_code_system_prompt(self) -> str:
        # #67 T1: 말미 캐시 경계 — question 인텐트는 이 뒤에 코드 컨텍스트(동적)를 붙일 수
        # 있으므로, 그 꼬리가 정적 캐시에 섞이지 않도록 경계로 정적 프리픽스를 닫는다.
        return SAFETY_ADDENDUM + "\n\n" + _NO_CODE_CHAT_PROMPT + CACHE_BOUNDARY

    def _tools_override_for_intent(self, intent: str) -> list | None:
        """코드 변경이 아닌 대화 턴에는 도구를 아예 제공하지 않는다."""
        if intent in _NO_CODE_INTENTS:
            return []
        # #EDU-27 재사용 시드편집 강제: 후보 코드를 세션에 심은 턴은 generate_code(전체재작성)를
        # 도구에서 빼 diff 편집(edit_code)만 가능하게 한다 — haiku 가 지시만으론 재작성하는 문제를
        # 도구 수준에서 원천 차단(출력토큰 실감축). complete_task 등 루프 완료 도구는 유지.
        # edit_code 가 없는 셋(blockly 등)이면 제한하지 않는다(무회귀).
        if getattr(self, "_reuse_seeded", False):
            restricted = [t for t in get_tools_for_phase(Phase.IMPLEMENT, self.state.coding_type)
                          if t.get("name") != "generate_code"]
            if any(t.get("name") == "edit_code" for t in restricted):
                return restricted
        return None

    def _classify_quick_initial_intent(self, user_input: str) -> str:
        """quick 첫 턴도 질문/잡담이면 구현으로 들어가지 않는다."""
        known = self.router.classify_known(user_input, Phase.IMPLEMENT)
        if known in ("chat", "question", "phase_change"):
            return known
        if known in ("modify_request", "implement_request"):
            return "implement_request"
        # quick 첫 턴의 애매한 앱/게임/도구 아이디어는 본 에이전트가 바로 생성한다.
        # 별도 의도 LLM 호출을 끼우면 "의도 파악"만으로 CLI 왕복이 추가되어 첫 응답이 크게 늦어진다.
        return "implement_request"

    def _looks_like_clarification_answer(self, user_input: str) -> bool:
        """직전 assistant 질문에 대한 구체 답변이면 수정 요청으로 이어간다."""
        if not self._last_assistant_question_text():
            return False
        text = (user_input or "").strip()
        if not text:
            return False
        if looks_like_short_confirmation(text):
            return False
        normalized = re.sub(r"\s+", " ", text).lower()
        if re.fullmatch(
            r"(아니|아니요|아냐|몰라|모르겠어|모르겠어요|없어|없어요|됐어|됐어요|괜찮아|괜찮아요|그냥|글쎄|나중에|no|nope|not sure)[.!?~…]*",
            normalized,
        ):
            return False
        return True

    def _reject_failed_artifacts(self, message: str, step: str | None = None) -> Generator[dict, None, None]:
        """검증 실패 산출물을 턴 시작 스냅샷으로 되돌리고 실패를 안내한다.

        state에는 항상 '검증을 통과한' 산출물만 남긴다 — 이전에 잘 동작하던 버전이 있으면
        그대로 유지되고, done 이벤트·세션 저장/복원 어느 경로로도 실패물이 렌더되지 않는다.
        LLM 히스토리에서도 이번 턴의 실패 tool_use/tool_result를 제거해 다음 턴 비용과
        앵커링을 막고, 유저가 본 실패 안내만 저장한다.
        """
        self.state.rollback_turn_artifacts()
        self._turn_artifact_rejected = True
        self.state.rollback_turn_messages_to_user(
            getattr(self, "_turn_user_message_index", None),
            message,
        )
        if step:
            yield self._emit_step(step, "verify", "error")
        yield {"type": "token", "text": message}

    def _post_impl_react(self, intent: str, system_prompt: str) -> Generator[dict, None, None]:
        """react 코드 생성 후 검증·빌드·후처리(설계/노트/주석).

        원칙: 검증→수정→'재검증'이 한 사이클 — 수정이 실제로 통했는지 확인 없이
        결과를 내보내지 않는다. (예전엔 수정 후 재검증이 없어 실패한 수정이 조용히 나갔다)
        검증 실패 시 산출물은 턴 시작 스냅샷으로 롤백된다(_reject_failed_artifacts).
        """
        first_impl = (intent != "modify_request")
        hybrid_mode = getattr(self, '_coding_type', 'react') == 'hybrid'
        # 단일 파일 계약 도입 전에 여러 파일로 저장된 hybrid 프로젝트의 수정 턴에는
        # 구조 계약을 소급하지 않는다 — 소급하면 그 프로젝트의 모든 수정이 검증 실패가 된다.
        prev_scripts = [p for p in self.state._code_map_snapshot if p.endswith(SCRIPT_EXTS)]
        enforce_single_file = len(prev_scripts) <= 1

        def current_errors() -> list[str]:
            found = validate_generated_code(self.state.generated_code_map, hybrid=hybrid_mode)
            if hybrid_mode:
                found.extend(validate_hybrid_code_map(
                    self.state.generated_code_map, enforce_single_file=enforce_single_file))
            return found

        # ① 정적 검증 → 수정 → 재검증 (예산 내)
        errors = current_errors()
        for _ in range(MAX_FIX_ROUNDS):
            if not errors:
                break
            yield self._emit_step("코드 검증", "verify", "error")
            app_before = self.state.generated_code_map.get("App.tsx")
            yield from self._fix_code(errors, system_prompt)
            # hybrid 단일 파일 계약의 탈출구: 도구(generate_code/edit_code)로는 파일을
            # 지울 수 없어, 여분 스크립트가 생기면 수정 라운드가 App.tsx를 완벽히 재작성해도
            # 재검증이 영영 실패한다(결정적 롤백). 수정 라운드가 App.tsx를 실제로 다시
            # 썼을 때(=병합 지시가 실행됐을 때)만 나머지 스크립트를 시스템이 제거한다 —
            # hybrid 런타임은 어차피 App.tsx만 로드하므로 여분 파일은 실행에 못 닿는다.
            if hybrid_mode and enforce_single_file:
                app_after = self.state.generated_code_map.get("App.tsx")
                if app_after and app_after != app_before:
                    self._drop_extra_hybrid_scripts()
            errors = current_errors()
        if errors:
            yield from self._reject_failed_artifacts(
                "코드가 검증을 통과하지 못해서 이번 결과는 반영하지 않았어요. "
                "이전에 잘 동작하던 내용은 그대로 유지돼요. 다시 요청해 주시면 새로 만들어볼게요.")
            return

        # ② 빌드 체크. 후처리(설계/노트/주석)는 미리보기(code_validated) '뒤'로 순차 실행한다 —
        # 같은 풀에서 병렬로 묶으면 풀 join 때문에 미리보기가 후처리 LLM 호출까지 기다리고,
        # 검증 실패 시 후처리 스레드가 롤백 뒤에 노트를 덧붙이는 경합도 생긴다.
        # 학습노트·설계 탭이 몇 초 늦는 비용으로 미리보기 지연과 실패물 게이팅을 지킨다.
        yield {"type": "status", "message": "빌드를 검증하고 있어요..."}
        build_ok, build_errors = yield from self._run_post_agents(
            run_build=True, run_post=False
        )

        # ③ 빌드 실패 → 수정 → '재빌드'로 확인 (예산 내)
        for _ in range(MAX_FIX_ROUNDS):
            if build_ok:
                break
            yield from self._fix_code(build_errors, system_prompt)
            build_ok, build_errors = build_check(dict(self.state.generated_code_map))
            yield self._emit_step("수정 후 재검증", "verify", "success" if build_ok else "error")

        # 하이브리드(SW+HW): 생성된 React 코드의 MODI SDK 사용에서 모듈을 추출해 준비물 문서 생성.
        # 하드웨어(blockly)의 modi_modules와 동일 형식이라 프론트 "모디" 탭이 자동으로 표시된다.
        if not build_ok:
            yield from self._reject_failed_artifacts(
                "코드가 빌드 검증을 통과하지 못해서 이번 결과는 반영하지 않았어요. "
                "이전에 잘 동작하던 내용은 그대로 유지돼요. 다시 요청해 주시면 새로 만들어볼게요.",
                step="빌드 검증")
            return

        # 빌드 실패 수정이 새 정적 문제를 만들 수 있으므로, 렌더 직전 한 번 더 확인한다.
        errors = current_errors()
        if errors:
            yield from self._reject_failed_artifacts(
                "수정 후 코드가 최종 검증을 통과하지 못해서 이번 결과는 반영하지 않았어요. "
                "이전에 잘 동작하던 내용은 그대로 유지돼요. 다시 요청해 주시면 새로 만들어볼게요.",
                step="최종 코드 검증")
            return

        if hybrid_mode:
            keys = extract_modi_module_keys("\n".join(self.state.generated_code_map.values()))
            # 배치/회전/부착물은 set_modi_layout 툴이 코드 생성과 같은 턴에 state에 기록한다
            # (블록 모드의 grid와 동일한 state 필드 재사용). 없으면 build에서 한 줄 폴백.
            derived = build_modi_modules_doc(
                keys, self.state.modi_grid,
                self.state.modi_rotations, self.state.modi_attachments)
            derived["title"] = self.state.title
            derived["description"] = self.state.description
            self.state.modi_modules = derived
            yield self._emit_step(
                f"모디 준비물 {len(derived.get('modules', []))}개 모듈", "modules", "success")

        yield self._code_validated_event()
        if first_impl:
            yield {"type": "status", "message": "설계/학습 노트를 정리하고 있어요..."}
            yield from self._run_post_agents(run_build=False, run_post=True)
        yield {"type": "token", "text": self._impl_done_message(first_impl)}

    def _drop_extra_hybrid_scripts(self) -> None:
        """App.tsx 외의 스크립트 파일을 code_map·generated_files에서 제거 (hybrid 단일 파일 계약).

        검증 실패 시엔 rollback_turn_artifacts가 턴 시작 스냅샷으로 되돌리므로,
        여기서 지워도 기존 프로젝트 파일이 유실될 위험은 없다.
        """
        extras = [p for p in self.state.generated_code_map
                  if p.endswith(SCRIPT_EXTS) and p != "App.tsx"]
        if not extras:
            return
        for p in extras:
            self.state.generated_code_map.pop(p, None)
        dropped = set(extras)
        self.state.project.generated_files = [
            f for f in self.state.project.generated_files if f.path not in dropped
        ]

    def _post_impl_blockly(self, system_prompt: str) -> Generator[dict, None, None]:
        """blockly XML 생성 후 검증·흐름도·준비물 처리."""
        def current_errors() -> list[str]:
            found = validate_blockly_xml(self.state.blockly_xml)
            if not self.state.modi_grid:
                found.append("모듈 배치(grid)가 없습니다. grid를 2D 배열로 포함해 주세요.")
            return found

        errors = current_errors()
        if errors:
            yield self._emit_step("블록 검증", "verify", "error")
            yield from self._fix_blockly(errors, system_prompt)
            errors = current_errors()

        if errors:
            yield from self._reject_failed_artifacts(
                "블록이 검증을 통과하지 못해서 이번 결과는 반영하지 않았어요. "
                "이전에 잘 동작하던 블록은 그대로 유지돼요. 다시 요청해 주시면 새로 만들어볼게요.",
                step="블록 검증")
            return

        # 흐름도·멀티랭 코드 변환(무거운 LLM 호출) 전에, 블록 미리보기와 모디 준비물을 먼저 보내
        # 즉시 렌더되게 한다. 흐름도/코드 변환/세부설명은 done에서 따라온다.
        # (모디 모듈은 결정적 격자 연산이라 빠름 — extras가 title/description을 채워 다시 갱신함.)
        if self.state.blockly_xml:
            keys = extract_module_keys_from_xml(self.state.blockly_xml)
            early_modules = build_modi_modules_doc(
                keys, self.state.modi_grid,
                self.state.modi_rotations, self.state.modi_attachments)
            early_modules["title"] = self.state.title
            early_modules["description"] = self.state.description
            self.state.modi_modules = early_modules
            yield {
                "type": "blockly_ready",
                "blockly_xml": self.state.blockly_xml,
                "modi_modules": self.state.modi_modules or None,
                "phase": self.get_phase(),
            }

        yield {"type": "status", "message": "흐름도와 코드 변환을 생성하고 있어요..."}
        yield from self._generate_blockly_extras()
        yield {"type": "token", "text": self._blockly_done_message()}

    def _attach_note_to_last_tool_result(self, note: str) -> bool:
        """가장 최근의 '실제' tool_result에 노트를 덧붙인다(검증 에러·재시도 지시 공용).

        - 가짜 tool_use_id를 만들지 않으므로 API에 유효함
        - tool_result(리스트 content)는 _serialize_messages·텍스트 히스토리에서 걸러져
          유저에게 안 보임 (유저가 한 말이 아니므로 채팅 버블로 노출되면 안 됨)
        """
        for msg in reversed(self.state._messages):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                for block in reversed(msg["content"]):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        block["content"] = (block.get("content") or "") + note
                        return True
        return False

    def _request_fix(self, errors: list, system_prompt: str, fix_tools: list,
                     instruction: str, status: str) -> Generator[dict, None, None]:
        """검증 에러를 모델에 전달하고 1회 수정 시도 (blockly 경로).

        에러는 직전 도구 호출의 '실제' tool_result에 덧붙인다.
        (react는 _fix_code가 컨텍스트 다이어트 버전으로 대체 — XML은 히스토리 tool 입력에만
        있어 compact를 못 쓰므로 blockly는 이 전체-히스토리 방식을 유지한다.)
        """
        yield {"type": "status", "message": status}
        err_text = f"\n\n⚠️ 다음 문제가 발견되었습니다. {instruction}\n" + "\n".join(f"- {e}" for e in errors)

        # 안전망: 도구를 호출한 턴이면 tool_result가 반드시 존재하므로 거의 도달 불가.
        if not self._attach_note_to_last_tool_result(err_text):
            self.state.add_user_message(err_text.strip())

        yield from self._agent_loop_stream(system_prompt, tools_override=fix_tools,
                                           max_tokens=MAX_OUTPUT_TOKENS_FIX)  # #67 T2

    def _fix_code(self, errors: list, system_prompt: str) -> Generator[dict, None, None]:
        """react 코드 검증/빌드 에러 1회 수정 (edit_code/generate_code).

        컨텍스트 다이어트: 에러가 특정한 파일의 코드만 시스템 프롬프트에 싣고 히스토리는
        compact로 보낸다 — 에러 한 줄 고치는 데 전체 코드(파일 8개면 ~17K 토큰, 턴 비용의
        ~26%)를 재전송하던 것을 제거. 에러에서 파일을 특정 못 하면 전체 컨텍스트로 폴백.
        """
        fix_tools = [t for t in TOOL_DEFINITIONS if t["name"] in self._FIX_ONLY_TOOLS]
        yield {"type": "status", "message": "코드를 수정하고 있어요..."}
        err_text = ("⚠️ 다음 문제가 발견되었습니다. edit_code(부분 수정) 또는 "
                    "generate_code(파일 재작성·누락 파일 생성)로 고치세요.\n"
                    + "\n".join(f"- {e}" for e in errors))
        # 내부 메시지 채널: 유저 채팅에 안 보이고 루프 종료 시 drop되는 일회성 지시.
        # (compact 히스토리는 tool_result를 걸러내므로 tool_result 부착 방식은 못 쓴다)
        self.state.add_internal_user_message(err_text)
        yield from self._agent_loop_stream(
            self._fix_system_prompt(errors, system_prompt),
            compact=True, tools_override=fix_tools,
            max_tokens=MAX_OUTPUT_TOKENS_FIX,  # #67 T2: 수정 라운드는 타깃 편집 — 낮은 상한
        )

    def _fix_system_prompt(self, errors: list, full_prompt: str) -> str:
        """수정 루프 전용 프롬프트 — 에러가 특정한 파일의 코드만 싣는다."""
        err_text = "\n".join(str(e) for e in errors)
        relevant = {p: c for p, c in self.state.generated_code_map.items() if p in err_text}
        if not relevant:
            return full_prompt  # 에러에서 파일 특정 불가 — 전체 컨텍스트 폴백
        # #67 T1: 매 수정 라운드 동일한 정적 헤더(안전규칙 + 수정 지침)만 캐시하고, 라운드마다
        # 바뀌는 파일 목록·대상 코드는 CACHE_BOUNDARY 뒤 동적 영역에. CODE_TOOL_CONTRACT 는
        # 소형 모델 준수를 위해 말단 유지(자체 설계) → 동적 영역 끝에 둔다.
        static = "\n\n".join([
            SAFETY_ADDENDUM,
            "코드 수정 도우미. 지시된 에러만 고치세요 — 에러와 무관한 코드·디자인은 건드리지 마세요.\n"
            "수정은 반드시 edit_code(부분 수정) 또는 generate_code(파일 전체 재작성·누락 파일 생성) 도구로 하세요.",
        ])
        dynamic_parts = ["## 전체 파일 목록\n" + "\n".join(f"- {p}" for p in self.state.generated_code_map)]
        for path, code in relevant.items():
            dynamic_parts.append(f"## 수정 대상: {path}\n```\n{code}\n```")
        dynamic_parts.append(CODE_TOOL_CONTRACT)
        return static + CACHE_BOUNDARY + "\n\n" + "\n\n".join(dynamic_parts)

    def _fix_blockly(self, errors: list, system_prompt: str) -> Generator[dict, None, None]:
        """blockly XML 검증 에러 1회 수정 (generate_blockly_xml로 다시 저장)."""
        blockly_tools = get_tools_for_phase(Phase.IMPLEMENT, 'blockly')
        yield from self._request_fix(
            errors, system_prompt, blockly_tools,
            "수정해서 generate_blockly_xml로 다시 저장해주세요.", "블록 코드를 수정하고 있어요...",
        )

    def _run_post_agents(self, run_build: bool = False, run_post: bool = False):
        """빌드 체크 + 후처리(설계 추출/학습 노트/코드 주석)를 한 풀에서 병렬 실행.

        - build_check는 코드맵 스냅샷으로 실행(스레드 안전).
        - 설계/노트/주석은 각각 state의 다른 필드에만 기록 → 쓰기 충돌 없음.
        - 빌드 결과 (ok, errors)를 반환. run_build=False면 (True, []).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        build_ok, build_errors = True, []
        jobs = {}
        code_snapshot = dict(self.state.generated_code_map)

        # 후처리는 ThreadPoolExecutor로 병렬 → wall-clock = max(자식들)이지 합이 아니다.
        # "후처리 (병렬)" span으로 묶어 트리에서 (1)병렬 묶음의 실제 소요시간 (2)후처리 비용 롤업이
        # 한눈에 보이게 한다. copy_context()를 이 span 안에서 떠서 자식 generation이 span 밑에 붙는다.
        with get_client().start_as_current_observation(name="후처리 (병렬)", as_type="span"):
            with ThreadPoolExecutor(max_workers=4) as executor:
                if run_build:
                    jobs[executor.submit(copy_context().run, build_check, code_snapshot)] = "build"
                if run_post:
                    code_ctx = self.state.get_current_code_context()
                    # #70 P1: 이미 있는 산출물은 재생성하지 않는다(재실행/재진입 시 중복 누적 방지 +
                    # 후처리 출력 절감). blockly 경로(_generate_blockly_extras)의 노트 가드와 동일 원칙.
                    # - 설계 문서가 이미 있으면(설계 모드에서 작성됨) 추출 스킵
                    if not self.state.project.design_doc.features:
                        jobs[executor.submit(copy_context().run, self._extract_design_doc)] = "design"
                    if not self.state.learning_notes:
                        # #70 후속(속도): 노트를 병렬 샤드로 생성 — 후처리 임계경로 단축(산출물 동일).
                        jobs[executor.submit(copy_context().run, self._generate_learning_notes,
                                             code_ctx, "5~8", POST_NOTES_SHARDS)] = "notes"
                    if not self.state.code_annotations:
                        jobs[executor.submit(copy_context().run, self._generate_annotations)] = "annotations"

                for future in as_completed(jobs):
                    key = jobs[future]
                    try:
                        result = future.result()
                        if key == "build":
                            build_ok, build_errors = result
                            yield self._emit_step("코드 검증", "verify", "success" if build_ok else "error")
                        elif key == "design":
                            yield self._emit_step("설계 문서 추출", "design", "success")
                        elif key == "notes":
                            n = len(self.state.learning_notes)
                            yield self._emit_step(f"학습 노트 {n}개 생성", "note", "success")
                        elif key == "annotations":
                            n = len(self.state.code_annotations)
                            yield self._emit_step(f"코드 주석 {n}개 추가", "annotation", "success")
                    except Exception:
                        yield self._emit_step(f"{key} 처리 실패", "error", "error")

        return build_ok, build_errors

    def _call_tools(self, tool_names: list, system: str, prompt: str, tool_choice: dict) -> list:
        """지정한 툴을 강제 호출하도록 LLM을 1회 실행하고 호출된 tool_use를 처리한다.

        설계 추출 / 학습 노트 / 코드 주석 서브 에이전트가 공유하는 보일러플레이트.
        호출된 (name, input) 목록 반환. 예외는 호출자(_run_post_agents 등)가 처리.
        """
        # CLI 모드에는 max_tokens 상한도 tool_choice 하드 강제도 없다 — 소형 모델이 툴콜 앞에
        # 초안/설명을 평문으로 한 번 쓰고 버리면(실측: 유효 3k에 생성 9.9k) 후처리 지연·비용이
        # 그만큼 배가된다. 남은 수단은 프롬프트 조임뿐이라 여기(공용 경로)서 일괄 지시한다.
        system = (system + "\n\n응답은 반드시 툴 호출로만 구성하세요. 툴 호출 밖에 인사말·계획·"
                  "설명·초안 등 어떤 텍스트도 쓰지 마세요 — 툴 밖 텍스트는 전부 버려집니다.")
        tools = [t for t in TOOL_DEFINITIONS if t["name"] in tool_names]
        _label = _SUBAGENT_LABELS.get(tool_names[0] if tool_names else "", f"서브에이전트 ({', '.join(tool_names)})")
        # #70 P2: 후처리 서브콜에도 프롬프트 캐싱 적용 — API 모드는 정적 system/tools 에
        # cache_control 부착(반복 후처리 턴 재사용), CLI 모드는 원본 문자열(캐시는 CLI 가 제어).
        # 메인 루프(_llm_call)와 동일한 분기라 무회귀.
        if _use_local_cli():
            sys_arg, tools_arg = strip_cache_boundary(system), tools
        else:
            sys_arg = cacheable_system(system) or system
            tools_arg = cacheable_tools(tools)
        with get_client().start_as_current_observation(
                name=_label, as_type="generation",
                input={"system": system, "prompt": prompt, "tools": tool_names}) as _gen:
            response = self.client.messages.create(
                model=self.model_fast,
                max_tokens=MAX_OUTPUT_TOKENS_POST,  # #70 P2: 후처리 출력 상한(튜닝 가능)
                system=sys_arg,
                tools=tools_arg,
                tool_choice=tool_choice,
                messages=[{"role": "user", "content": prompt}],
            )
            # #70 P2: 후처리 출력이 상한에 걸려 잘렸는지 — 상한을 내렸을 때 품질 회귀(잘린 노트/
            # 설계)를 대시보드에서 감시(_emit_turn_scores 가 스코어화). 메인 루프 T2 truncation 과 동형.
            if getattr(response, "stop_reason", None) == "max_tokens":
                self._turn_post_truncated = True
            called = []
            for block in response.content:
                if block.type == "tool_use" and block.name in tool_names:
                    handle_tool_call(block.name, block.input, self.state)
                    called.append((block.name, block.input))
            _update_generation(_gen, self.model_fast, response=response,
                               output={"called": [{"name": n, "input": i} for n, i in called]},
                               step="sub_agent", tools_offered=tool_names,
                               phase=self.get_phase())
            self._add_turn_usage(getattr(response, "usage", None))  # #130: 후처리 서브콜도 턴 누적
            return called

    # 학습 노트 작성 규칙 (web/blockly 공통)
    _NOTE_RULES = (
        "- 기술 용어 금지, 중학생이 이해할 수 있는 일상 언어\n"
        "- title: 호기심 자극하는 제목\n"
        "- what: 일상 비유로 3~4문장\n"
        "- why: 없으면 어떤 불편함? 3~4문장\n"
        "- where: 인스타, 카톡, 쿠팡 등 일상 앱/기기 예시\n"
        "- 이 프로젝트에 실제 쓰인 개념 위주"
    )

    # #70 후속(속도): 노트 병렬 샤드용 '개념 렌즈' — 샤드마다 다른 관점을 줘 개념 중복을 최소화.
    _NOTE_LENSES = (
        "화면·UI·사용자 상호작용에서 쓰인 개념",
        "데이터·상태 관리·계산·로직에서 쓰인 개념",
        "컴포넌트 구성·구조·재사용에서 쓰인 개념",
        "그 외 이 프로젝트에 실제 쓰인 일반 프로그래밍 개념",
    )

    def _generate_learning_notes(self, context: str, count: str = "5~8", shards: int = 1) -> None:
        """학습 노트 생성 — web/blockly 공통.

        context: web은 생성된 코드, blockly는 XML+동작코드+대화.
        add_learning_note를 tool_choice로 강제해 {title, what, why, where} 구조를 보장.

        shards>1(#70 후속): 총 개수는 유지하되 노트 생성을 '개념 렌즈'로 나눠 병렬 호출한다.
        한 호출이 배열 전체를 순차 출력하던 지연(후처리 임계경로, 실측 ~25s)을 wall≈1/shards 로
        줄인다. 각 샤드는 서로 다른 렌즈라 개념이 안 겹치고, 완료 후 제목 기준 중복만 제거해
        산출물(개수·품질)을 보존한다. shards<=1이면 종전 단일 호출과 동일.
        """
        if not context or not context.strip():
            return
        if shards <= 1:
            self._notes_call(context, count)
            return
        from concurrent.futures import ThreadPoolExecutor
        # 총 목표 = count 문자열의 최대 정수("5~8"→8). 샤드별 균등 분배(올림), 최소 2개.
        nums = [int(n) for n in re.findall(r"\d+", count)] or [6]
        per = max(2, -(-max(nums) // shards))  # 올림 나눗셈(math 불필요)
        before = len(self.state.learning_notes)
        with ThreadPoolExecutor(max_workers=shards) as ex:
            futs = [ex.submit(copy_context().run, self._notes_call, context, str(per),
                              self._NOTE_LENSES[i % len(self._NOTE_LENSES)])
                    for i in range(shards)]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass
        # 샤드 간 우연한 제목 중복 제거 — 이번 턴에 추가된 것만 대상(기존 노트는 보존).
        added = self.state.learning_notes[before:]
        seen: set = set()
        deduped = []
        for n in added:
            key = (n.get("title") or "").strip().lower()
            if key and key in seen:
                continue
            seen.add(key)
            deduped.append(n)
        self.state.learning_notes[before:] = deduped

    def _notes_call(self, context: str, count: str, lens: str = "") -> None:
        """학습 노트 1회 서브콜(단일 샤드). lens 가 있으면 그 관점의 개념 위주로 고른다."""
        focus = f"- **이번에는 특히 '{lens}' 위주로** 골라 작성하세요.\n" if lens else ""
        prompt = (
            f"아래 내용을 보고 학습 노트 {count}개를 add_learning_note 툴로 생성하세요.\n\n"
            f"규칙:\n{self._NOTE_RULES}\n{focus}\n{context}\n"
        )
        self._call_tools(
            ["add_learning_note"],
            "학습 노트 생성기. 반드시 add_learning_note 툴을 호출하세요.",
            prompt,
            {"type": "tool", "name": "add_learning_note"},
        )

    def _generate_annotations(self) -> None:
        """코드 주석 생성 (web 전용 — 줄 번호가 필요해 코드가 있어야 함)."""
        code_context = self.state.get_current_code_context()
        if not code_context:
            return
        prompt = f"""\
아래 앱 코드를 보고 코드 주석 10~15개를 add_code_annotation 툴로 생성하세요.

규칙:
- file: 파일 경로, line: 줄 번호 (코드에 실제로 존재하는 위치)
- title: 한 줄 제목 (예: "조건에 따라 다른 화면 보여주기")
- explanation: 초보자도 이해할 수 있는 쉬운 설명 1~2문장
- 프로그래밍 입문자 수준. 자료 구조, 흐름 제어, 데이터 전달 같은 제너럴한 개념 위주
- 반드시 add_code_annotation 툴을 호출하세요. 텍스트 응답은 하지 마세요.

{code_context}
"""
        self._call_tools(
            ["add_code_annotation"],
            "코드 주석 생성기. 반드시 add_code_annotation 툴을 호출하세요.",
            prompt,
            {"type": "tool", "name": "add_code_annotation"},
        )

    def _extract_design_doc(self) -> None:
        """생성된 코드를 주 근거로 설계 문서와 다이어그램을 별도 LLM 호출로 추출 (web 전용)."""
        conversation = self.state.get_text_history()
        code_context = self.state.get_current_code_context()
        # 코드가 없으면 추출할 게 없음 (설계 문서는 실제 구현 기준)
        if not code_context:
            return

        prompt = f"""\
아래 **실제로 생성된 코드를 분석해서** 이 서비스의 설계 정보를 추출하세요.
설계 문서는 전적으로 코드 기준입니다 — 코드에 구현된 화면/기능/데이터/흐름을 그대로 읽어 작성하세요.
"미정"이나 "정의되지 않음" 같은 플레이스홀더는 절대 쓰지 마세요.
(대화 내용은 서비스 주제를 파악하는 보조 힌트로만 참고하세요.)

반드시 `update_design_doc` 툴과 `update_diagram` 툴을 둘 다 호출하세요.
텍스트 응답은 하지 마세요.

또한 이 서비스의 UI 타입을 판단하세요:
- 세로형 UI(채팅, 피드, 단일 리스트), "앱/모바일" 언급, 네이티브 기능(지도, 카메라, GPS 등) → mobile
- 가로형 UI(대시보드, 테이블, 사이드바, 다단 그리드), 일반 웹사이트 → desktop

{code_context}

## 참고: 대화 주제
{conversation[-1000:] if conversation else "(대화 없음)"}
"""
        called = self._call_tools(
            ["update_design_doc", "update_diagram"],
            "설계 문서 추출기. update_design_doc과 update_diagram 툴을 호출하세요.",
            prompt,
            {"type": "any"},
        )
        # update_design_doc이 호출됐으면 제목/설명/app_type 추론
        if any(name == "update_design_doc" for name, _ in called):
            doc = self.state.project.design_doc
            if doc.project_name and not self.state.title:
                self.state.title = doc.project_name
            if doc.description and not self.state.description:
                self.state.description = doc.description
            if not self.state.app_type:
                all_text = (doc.project_name + doc.description + " ".join(f.name for f in doc.features)).lower()
                mobile_keywords = ["앱", "모바일", "배달", "채팅", "메신저", "sns", "피드", "지도", "gps", "카메라", "알림"]
                self.state.app_type = "mobile" if any(k in all_text for k in mobile_keywords) else "desktop"

    def _generate_blockly_extras(self) -> Generator[dict, None, None]:
        """blockly XML에서 흐름도+멀티랭 코드 / 학습 노트 / MODI 모듈 준비물을 생성.

        흐름도(LLM)와 학습 노트(LLM)는 서로 의존이 없어 병렬로 실행한다.
        노트는 흐름도가 만드는 Python 출력에 의존하지 않도록 XML+대화만 컨텍스트로 쓴다
        (XML이 동작 로직의 원본이라 노트 작성에 충분).
        모듈 준비물은 결정적(빠름)이라 인라인 처리.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        xml = self.state.blockly_xml
        if not xml:
            return

        conversation = self.state.get_text_history()[-1500:]
        note_context = f"## 대화 주제\n{conversation[-1000:]}\n\n## Blockly XML\n{xml}"

        # ── 흐름도 + 학습 노트 병렬 ──
        jobs = {}
        # 메인 후처리와 동일하게 병렬 묶음을 span으로 — wall-clock/비용 롤업이 트리에 보이게.
        with get_client().start_as_current_observation(name="후처리 (병렬)", as_type="span"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                # copy_context().run — 워커 스레드에 Langfuse 컨텍스트 전파(generation 트레이스 부착)
                jobs[executor.submit(copy_context().run, self._generate_blockly_diagram, xml, conversation)] = "flowchart"
                # 첫 생성에서만 (이미 노트가 있으면 modify로 보고 스킵 — 누적 방지)
                if not self.state.learning_notes:
                    jobs[executor.submit(copy_context().run, self._generate_learning_notes, note_context, "3~5")] = "notes"

                for future in as_completed(jobs):
                    key = jobs[future]
                    try:
                        future.result()
                        if key == "flowchart":
                            yield self._emit_step(f"흐름도 {len(self.state.blockly_flowchart)}개 노드 생성", "flowchart", "success")
                        else:
                            yield self._emit_step(f"학습 노트 {len(self.state.learning_notes)}개 생성", "note", "success")
                    except Exception:
                        if key == "flowchart":
                            yield self._emit_step("흐름도/코드 생성 실패", "flowchart", "error")
                        else:
                            yield self._emit_step("학습 노트 생성 실패", "note", "error")

        # ── MODI 모듈 준비물 (XML에서 모듈 추출 + 격자로 배치/조립) ──
        # 하이브리드와 '동일한' 공용 파이프라인(build_modi_modules_doc): repair_grid 물리 보정 +
        # grid_to_layout/grid_to_assembly/accessory_parts. 모듈 추출만 XML 기반(블록)으로 다르다.
        keys = extract_module_keys_from_xml(xml)
        derived = build_modi_modules_doc(
            keys, self.state.modi_grid,
            self.state.modi_rotations, self.state.modi_attachments)
        # 동작 소개용 — 다이어그램/조립 아래에 표시 (병렬 흐름도 작업이 채워 둠)
        derived["title"] = self.state.title
        derived["description"] = self.state.description
        self.state.modi_modules = derived
        yield self._emit_step(f"모디 준비물 {len(derived.get('modules', []))}개 모듈", "modules", "success")

    def _blockly_done_message(self) -> str:
        """blockly 구현이 끝난 뒤 보낼 완료 메시지 한 줄. (앞 텍스트와 분리되도록 \\n\\n 시작)"""
        title = (self.state.title or "").strip()
        head = f"'{title}' 완성!" if title else "다 만들었어요!"
        return f"\n\n{head} 블록 코드와 흐름도, 준비물·조립 방법까지 준비했어요. 미리보기에서 확인해 보세요."

    def _impl_done_message(self, first_impl: bool) -> str:
        """react 구현/수정 완료 후 사용자에게 보일 완료 멘트.
        (구현 단계의 모델 프로즈는 agent log로 가므로, 완료 메시지는 명시적으로 버블로 보낸다.)"""
        if first_impl:
            return "다 만들었어요! 오른쪽 미리보기에서 확인해보세요. 바꾸고 싶은 부분이 있으면 편하게 말해주세요."
        return "수정했어요! 미리보기에서 확인해보세요."

    def _generate_blockly_diagram(self, xml: str, conversation: str) -> None:
        """Blockly XML → 흐름도 + 멀티랭 코드(title/description 포함). 병렬 워커용 plain 메서드.

        코드 문자열의 따옴표·줄바꿈이 JSON을 깨기 쉬워 모듈 생성과는 분리되어 있다.
        """
        code_prompt = f"""\
아래 MODI Blockly XML과 대화 내용을 분석해서 JSON으로 생성하세요. 텍스트 응답 없이 JSON만 출력하세요.

1. title: 이 프로젝트의 한글 제목 (예: "조이스틱 자동차", "스마트 조명")
2. description: 한 줄 설명 (예: "조이스틱으로 조종하는 MODI 자동차")
3. detail: 이 코드가 무슨 동작을 하는지 2~4문장으로 자세히 설명. 어떤 입력에 어떻게 반응하는지, 동작 순서·조건을 풀어서. (예: "조이스틱을 위로 밀면 두 모터가 같은 속도로 돌아 앞으로 가요. 좌우로 밀면 한쪽 모터만 빠르게 돌려 방향을 바꿔요. 가운데로 두면 멈춰요.")
4. flowchart: 동작 흐름 노드 배열
   - type: "start"/"loop"/"condition"/"action"/"end"
   - label: 한글로 짧게 (예: "반복", "버튼 클릭이면", "모터A 속도 100")
   - children: 루프 내부 노드 (배열)
   - branches: 조건 분기 (배열, 각 항목에 label + children)
5. code_langs: 같은 로직을 Python/JavaScript/C로 변환
   - python: import modi 포함, 들여쓰기 4칸
   - javascript: const modi = require('modi') 포함
   - c: #include "modi.h" 포함
   ※ 코드 안의 따옴표·역슬래시·줄바꿈은 JSON 문자열로 올바르게 이스케이프하세요(\\n, \\", \\\\).
6. design: 이 하드웨어 프로젝트의 설계 문서 (quick·design 모드 공통으로 항상 채움)
   - features: 주요 기능/동작 배열. 각 항목 {{"name": "기능 이름", "description": "한 줄 설명"}}
   - users: 누가 쓰는지 배열 (예: ["로봇을 만들어보고 싶은 학생"])
   - flows: 동작 흐름 배열 (예: ["전원 켜기 → 조이스틱 입력 감지 → 모터 회전"])

JSON 형식:
{{"title": "...", "description": "...", "detail": "...", "flowchart": [...], "code_langs": {{"python": "...", "javascript": "...", "c": "..."}}, "design": {{"features": [{{"name": "...", "description": "..."}}], "users": ["..."], "flows": ["..."]}}}}

대화:
{conversation}

XML:
{xml}
"""
        with get_client().start_as_current_observation(
                name="Blockly 분석 (JSON 추출)", as_type="generation",
                input={"prompt": code_prompt}) as _gen:
            response = self.client.messages.create(
                model=self.model_fast,
                max_tokens=8192,
                system="MODI Blockly XML 분석기. JSON만 출력하세요.",
                messages=[{"role": "user", "content": code_prompt}]
            )
            _update_generation(_gen, self.model_fast, response=response,
                               output=response.content[0].text,
                               step="blockly_analyze", phase=self.get_phase())
            self._add_turn_usage(getattr(response, "usage", None))  # #130: 병렬 워커 콜도 턴 누적
        data = _parse_json_object(response.content[0].text)
        self.state.blockly_flowchart = data.get("flowchart", [])
        self.state.blockly_code_langs = data.get("code_langs", {})
        self.state.blockly_detail = data.get("detail", "")
        title = data.get("title", "")
        desc = data.get("description", "")
        if title:
            self.state.title = title
        if desc:
            self.state.description = desc

        # 설계 문서: blockly도 quick·design 모두 동일하게 설계문서 탭이 뜨도록 빌드 시 채운다.
        # 이미 채워진 필드(설계 모드에서 에이전트가 만든 것)는 보존하고 빈 곳만 보강.
        from agent.models import Feature
        doc = self.state.project.design_doc
        design = data.get("design") or {}
        if title and not doc.project_name:
            doc.project_name = title
        if desc and not doc.description:
            doc.description = desc
        if not doc.features:
            for f in design.get("features", []):
                if isinstance(f, dict) and f.get("name"):
                    doc.features.append(Feature(name=f["name"], description=f.get("description", "")))
                elif isinstance(f, str) and f:
                    doc.features.append(Feature(name=f))
        if not doc.users:
            doc.users = [u for u in design.get("users", []) if isinstance(u, str)]
        if not doc.user_flows:
            doc.user_flows = [s for s in design.get("flows", []) if isinstance(s, str)]

    _FIX_ONLY_TOOLS = {"generate_code", "edit_code"}

    @observe(name="LLM 호출 (llm_call)", as_type="generation", capture_input=False, capture_output=False)
    def _llm_call(self, system_prompt: str, tools: list, compact: bool = False,
                  tool_choice: dict | None = None, defer_text: bool = False,
                  max_tokens: int | None = None) -> Generator[dict, None, None]:
        """LLM 1회 호출 → 스트리밍 → 도구 실행. 딱 1번만. tool_choice로 도구 사용 강제 가능.

        max_tokens(#67 T2): 이 호출의 출력 상한. None이면 MAX_OUTPUT_TOKENS(기본 빌드 상한).
        """
        tools = tools or []
        # 기본값 False — 취소/한도 초과 등 조기 반환 시 루프가 멈추도록
        self._last_had_tools = False
        # 이번 호출이 LLM 실패(rate limit 소진 등)로 자체 안내를 내고 끝났는지 —
        # 루프가 "재시도 무의미 + 사과 중복 방지"를 판단하는 신호.
        self._last_call_failed = False
        # 이번 응답 텍스트가 채팅에서 억제됐는지(도구 강제 위반 응답) — 루프가
        # "라이브에 안 보였으니 히스토리에서도 폐기"를 판단하는 명시적 신호.
        self._last_text_suppressed = False
        # 수정 계약처럼 "말만 하고 미이행" 가능성이 있는 턴은 텍스트 표시를 루프 판단 뒤로 미룬다.
        # 요구사항 확인 질문이면 루프가 다시 내보내고, 거짓 완료/약속이면 폐기한다.
        self._last_text_deferred = False
        # 방금 모델이 낸 자연어. 계약 루프가 "질문으로 유저에게 턴을 넘김"인지 판정할 때 쓴다.
        self._last_model_text = ""
        if self._cancelled:
            yield {"type": "cancelled"}
            return

        messages = self.state.get_compact_messages() if compact else self.state.get_api_messages()
        get_client().update_current_generation(
            model=self._get_model(), input=messages,
            metadata={
                "phase": self.get_phase(),
                "coding_type": getattr(self, "_coding_type", "react"),
                "tools_offered": [t["name"] for t in tools] if tools else [],
                "compact": compact,
                # 도구 강제 여부 — 트레이스에서 "강제했는데도 텍스트로 뱉음" 같은 실패를 식별용
                "tool_choice": (tool_choice or {}).get("type", "auto"),
            },
        )

        # 프롬프트 캐싱: API 모드에서만 system/tools 에 cache_control 부착(반복 호출 토큰 절감).
        if _use_local_cli():
            # #67 T1(CLI): CLI 모드는 캐시를 우리가 제어하지 않는다 — Claude Code 가
            # --system-prompt 전체를 캐시하고 에이전트 루프의 여러 라운드에서 그대로
            # 재사용(cache_read)한다. 벤치마크 실측상 이 방식이 이미 cache_read ~50% 로
            # 효율이 높았다. 동적 컨텍스트를 메시지 채널로 빼 정적 프리픽스만 캐시하려던
            # 시도는 오히려 큰 코드가 캐시 밖으로 나가 cache_read 가 9% 로 급락(비용 +18%)해
            # 폐기했다. 경계 토큰만 제거하고 원본 프롬프트를 그대로 넘긴다(무회귀).
            sys_arg, tools_arg = strip_cache_boundary(system_prompt), tools
        else:
            # API 모드: 정적 프리픽스만 cache_control(경계로 분리) → 이슈 원문의 cache_read
            # 14% 문제를 잡는 지점. CLI 와 달리 여기선 우리가 브레이크포인트를 제어한다.
            sys_arg = cacheable_system(system_prompt) or system_prompt
            tools_arg = cacheable_tools(tools)

        out_cap = max_tokens or MAX_OUTPUT_TOKENS
        stream_kwargs = dict(
            model=self._get_model(),
            max_tokens=out_cap,
            system=sys_arg,
            messages=messages,
        )
        if tools_arg:
            stream_kwargs["tools"] = tools_arg
        # 도구 사용 강제(예: 구현 단계에서 코드를 텍스트로 뱉는 것 방지). None이면 미지정(auto).
        # API 모드는 하드 강제, CLI 모드는 프롬프트 지시문으로 소프트 강제된다.
        if tool_choice:
            stream_kwargs["tool_choice"] = tool_choice

        stream_obj = None
        for attempt in range(3):
            try:
                stream_obj = self.client.messages.stream(**stream_kwargs)
                break
            except Exception as e:
                # 일시적 실패(429/529/timeout/네트워크)만 재시도. 분류는 agent.retry 로 통일.
                if not is_retryable_error(str(e)):
                    raise
                wait = backoff_delay(attempt)
                yield {"type": "status", "message": f"요청이 많아서 {wait:.0f}초 후 재시도합니다..."}
                time.sleep(wait)

        if stream_obj is None:
            get_client().update_current_generation(
                level="ERROR", status_message="rate limit exceeded after retries")
            self._last_call_failed = True  # 자체 안내를 냈으니 루프는 재시도/사과 생략
            yield {"type": "token", "text": "요청 한도를 초과했어요. 잠시 후 다시 시도해주세요."}
            return

        tool_uses = []
        content_blocks = []

        # 스트림 이터레이션 중 일시적 서버 오류(529 Overloaded 등) 재시도
        max_stream_retries = 3
        try:
          for stream_attempt in range(max_stream_retries):
            try:
                # cancel() 이 종료할 수 있도록 현재 스트림을 등록(재시도로 재생성될 때마다 갱신).
                self._active_stream = stream_obj
                with stream_obj as stream:
                    ctx = _StreamContext()
                    for event in stream:
                        if self._cancelled:
                            break
                        yield from ctx.handle(event)

                    if self._cancelled:
                        yield {"type": "cancelled"}
                        return

                    final_message = stream.get_final_message()
                    for block in final_message.content:
                        content_blocks.append(block)
                        if block.type == "tool_use":
                            tool_uses.append(block)
                break  # 성공 시 재시도 루프 탈출
            except RuntimeError as e:
                err_msg = str(e)
                # Claude CLI 미로그인(세션 만료 등): 재시도·사과로 안 풀리고 운영자 재로그인이
                # 필요하다. 크래시로 올려 Sentry 서버에러가 되지 않게 여기서 명확히 안내하고 끝낸다.
                # (학생은 못 고치므로 "잠시 후 다시 / 선생님께 알려주세요"로 유도.)
                if is_auth_login_error(err_msg):
                    get_client().update_current_generation(
                        level="ERROR", status_message="claude cli not logged in (재로그인 필요)")
                    self._last_call_failed = True
                    yield {"type": "token",
                           "text": "지금 코딩 도우미에 연결할 수 없어요. 잠시 후 다시 시도해 주세요. "
                                   "문제가 계속되면 선생님께 알려주세요."}
                    # 위 사과 토큰은 유지(프론트 표시용). 직후 구조화 error 이벤트를 추가 방출해
                    # 프론트가 인증 문제임을 코드로 감지하게 한다(additive).
                    yield error_event(ErrorCode.LLM_AUTH)
                    return
                if is_quota_limit_error(err_msg):
                    get_client().update_current_generation(
                        level="WARNING", status_message="claude cli quota/session limit")
                    self._last_call_failed = True
                    msg = self._quota_limit_message(err_msg)
                    yield {"type": "token", "text": msg}
                    # 위 사과 토큰은 유지. 직후 구조화 error 이벤트를 추가 방출(additive).
                    # 리셋시각을 담은 동적 문구를 message 로 넘겨 이벤트에도 동일 정보를 싣는다.
                    yield error_event(ErrorCode.LLM_QUOTA, message=msg)
                    return
                # CLI 경로의 일반 RuntimeError(429 등)도 일시적이면 스트림 재생성으로 재시도.
                # (스트림 재생성은 CLI 의 경우 새 _CliStream 이라 안전. 대부분 첫 토큰 이전에 실패.)
                is_transient = is_retryable_error(err_msg)
                if not is_transient or stream_attempt >= max_stream_retries - 1:
                    raise
                wait = backoff_delay(stream_attempt)
                yield {"type": "status", "message": f"서버가 과부하 상태입니다. {wait:.0f}초 후 재시도합니다..."}
                time.sleep(wait)
                # 재시도를 위해 stream_obj 재생성
                try:
                    stream_obj = self.client.messages.stream(**stream_kwargs)
                except Exception:
                    raise RuntimeError(err_msg) from e
        finally:
            # 스트림 종료(성공/취소/에러) 시 활성 등록 해제 — 다음 cancel 이 죽은 스트림을 안 건드리도록.
            self._active_stream = None

        # 에이전트가 '한 말'(프로즈)은 도구를 같이 호출했든 아니든 항상 채팅 메시지(버블)로 보낸다 —
        # "여러 모듈로 스마트팜 만들게요" 같은 코멘트 포함. 작업 '진행 과정'은 도구 실행 스텝
        # (generate_*, 흐름도, 모디 등)이 agent log에 따로 남긴다. (한 말=채팅, 한 일=로그.)
        # 예외: 도구를 강제했는데(tool_choice) 호출이 없으면 실패 응답(코드 텍스트 덤프 등) —
        # 채팅으로 흘리지 않는다. 텍스트는 버퍼 후 일괄 방출이라 여기서 막으면 유저는 못 보고,
        # 루프가 복구(salvage)하거나 폐기·재시도하며 최종 실패 시 사과 메시지가 따로 나간다.
        model_text = "".join(ctx.buffered_tokens).strip()
        self._last_model_text = model_text
        self._last_text_suppressed = bool(model_text) and bool(tool_choice) and not tool_uses
        self._last_text_deferred = bool(model_text) and defer_text and not self._last_text_suppressed
        if model_text and not self._last_text_suppressed and not self._last_text_deferred:
            for token in ctx.buffered_tokens:
                yield {"type": "token", "text": token}

        # generation 출력/토큰 기록 (도구 디스패치·후속 에러와 무관하게 부착)
        usage_details, cost_details = _extract_usage(final_message)
        self._add_turn_usage(getattr(final_message, "usage", None))  # #130: 메인 루프 콜 턴 누적
        called_names = [tu.name for tu in tool_uses]
        stop_reason = getattr(final_message, "stop_reason", None)
        # #67 T2: 출력이 상한에 걸려 잘렸는지(품질 회귀 신호). 상한을 내렸을 때 truncation 이
        # 늘지 않는지 대시보드에서 검증할 수 있게 턴 플래그로 남긴다(_emit_turn_scores 가 스코어화).
        if stop_reason == "max_tokens":
            self._turn_output_truncated = True
        # #68 O1: 이 호출의 출력 토큰을 생성 종류별(전체재작성/부분수정/산문/기타도구)로 근사
        # 배분해 generation 메타에 남기고 턴 누적에 더한다 — 수정 턴 전체재작성 낭비를 숫자로 확정.
        out_breakdown = _attribute_output((usage_details or {}).get("output", 0), model_text, tool_uses)
        # getattr 누적 — _llm_call 이 턴 초기화(_chat_stream_impl) 밖에서 단독 호출되는 경로(테스트 등)도 안전.
        self._turn_out_generate = getattr(self, "_turn_out_generate", 0) + out_breakdown["generate_code"]
        self._turn_out_edit = getattr(self, "_turn_out_edit", 0) + out_breakdown["edit_code"]
        self._turn_out_prose = getattr(self, "_turn_out_prose", 0) + out_breakdown["prose"]
        self._turn_out_other = getattr(self, "_turn_out_other", 0) + out_breakdown["other_tool"]
        # 이름은 단계만 — 무슨 도구를 썼는지는 하위 "도구 · ..." span 이 이미 보여줘서 중복 나열 안 함.
        get_client().update_current_generation(
            name=f"LLM · {self.get_phase()}",
            output=model_text, usage_details=usage_details, cost_details=cost_details,
            metadata={
                "phase": self.get_phase(),
                "tools_called": called_names,
                "stop_reason": stop_reason,
                "had_text": bool(model_text),
                "max_output_tokens": out_cap,  # #67 T2: 이 호출의 출력 상한(튜닝 관측)
                "output_breakdown": out_breakdown,  # #68 O1: 종류별 출력 토큰 근사
            },
        )

        self.state.add_assistant_message(content_blocks)

        self._last_had_tools = bool(tool_uses)
        had_text = bool(model_text)
        if not tool_uses:
            return had_text

        yield from self._execute_tool_groups(tool_uses)
        return had_text

    @observe(name="에이전트 루프 (agent_loop)", as_type="agent", capture_input=False, capture_output=False)
    def _agent_loop_stream(self, system_prompt: str, compact: bool = False, max_loops: int = None,
                           tools_override: list = None, contract=_AUTO_CONTRACT,
                           max_tokens: int | None = None) -> Generator[dict, None, None]:
        """_llm_call을 max_loops번 반복. 기본 1회.

        contract(TurnContract): 이 턴의 완료 계약. 계약이 있으면 '계약 이행'이 완료 조건이고,
        텍스트로만 끝나거나 라운드 예산이 소진됐는데 미이행이면 공용 복구 기계가 돈다 —
        ① 덤프에서 코드 복구(salvage, LLM 비용 0) ② nudge 후 재시도(MAX_CODE_RETRIES 내)
        ③ 그래도 실패면 정직하게 종료(사과 또는 스코어). 계약이 None이면 대화 턴 —
        텍스트로 끝나는 게 정상. 기본값(_AUTO_CONTRACT)은 상태에서 기본 계약을 도출한다.
        """
        coding_type = getattr(self, '_coding_type', 'react')
        tools = get_tools_for_phase(self.state.project.phase, coding_type) if tools_override is None else tools_override
        limit = max_loops or 1
        if contract is _AUTO_CONTRACT:
            contract = self._implement_contract(None)
        get_client().update_current_span(metadata={
            "phase": self.get_phase(),
            "coding_type": coding_type,
            "max_loops": limit,
            "tools_offered": [t["name"] for t in tools] if tools else [],
            "contract": getattr(contract, "fail_flag", None),
        })

        start_phase = self.state.project.phase
        retries_left = MAX_CODE_RETRIES
        produced = 0  # 도구를 실제로 쓴 라운드 수 (재시도 라운드는 예산에서 제외)
        completed_by_question = False
        try:
            while True:
                unmet = contract is not None and not contract.done()
                # 강제 시점은 계약이 정한다: 신규 생성은 처음부터, 수정은 재시도부터
                # (첫 응답이 질문에 대한 답일 수 있으므로). CLI는 프롬프트 소프트 강제.
                force_tool = unmet and (contract.force_from_start or retries_left < MAX_CODE_RETRIES)
                defer_text = bool(unmet and contract is not None and contract.question_exempt)
                errors_before = getattr(self, "_turn_tool_errors", 0)  # 이번 라운드 도구 에러 판정용
                had_text = yield from self._llm_call(
                    system_prompt, tools, compact,
                    tool_choice=contract.force_choice if force_tool else None,
                    defer_text=defer_text,
                    max_tokens=max_tokens,
                )
                if self._cancelled:
                    return
                # 에이전트가 phase를 바꿨으면(예: 설계→구현 transition_phase 호출) 루프 종료 —
                # 상위(chat_stream)에서 새 phase에 맞게 처리(구현 생성 등)한다.
                if self.state.project.phase != start_phase:
                    break
                unmet = contract is not None and not contract.done()
                if not self._last_had_tools:
                    # 순수 텍스트 응답 — 계약이 없거나 이행됐으면 완료. 미이행이면 실패다:
                    # 완료로 오해해 end로 보내지 않는다.
                    if unmet:
                        if contract.question_exempt and self._last_response_asks_user_question():
                            # 유저에게 질문으로 턴을 넘김 = 정상 대화 완결. 단 강제 라운드의
                            # 텍스트는 채팅에서 억제/보류됐을 수 있으므로 질문을 다시 내보낸다 —
                            # 유저가 봐야 답할 수 있다. (스코어로 발생률 관측)
                            if getattr(self, "_last_text_suppressed", False) or getattr(self, "_last_text_deferred", False):
                                yield {"type": "token", "text": self._last_assistant_text()}
                            self._turn_modify_clarified = True
                            completed_by_question = True
                            break
                        if (yield from self._salvage_code_dump()):
                            break  # 산출물 확보 — 검증·렌더는 사후 파이프라인(_post_impl_react)이
                        if retries_left > 0 and self._prepare_code_retry(contract):
                            retries_left -= 1
                            yield {"type": "status", "message": "코드를 다시 생성하고 있어요..."}
                            continue  # 재시도 — limit(produced) 예산과 별개
                    break
                produced += 1
                # 설계 단계에선 도구를 썼더라도 같은 라운드에 응답 텍스트(확인+질문)까지 냈으면
                # 완결이므로 종료 — 다음 라운드에서 나오던 형식적 멘트("편하게 답해줘요")를 막는다.
                if had_text and self.state.project.phase == Phase.DESIGN:
                    break
                # 계약이 이행됐고 이번 라운드 도구가 전부 성공했으면 즉시 완결 — 남은 라운드
                # 예산은 도구 에러 복구 전용이다(CODE_TOOL_CONTRACT의 원샷 배칭 계약).
                # 이행 후 "마무리 멘트" 라운드를 위해 모델을 다시 부르면, 소형 모델은 재작성
                # 금지 지시를 무시하고 매 라운드 전체 파일을 다시 생성한다(실사고: 수정 1턴에
                # 동일 App.tsx 구현 3회, $0.17·2m11s — 마지막 재생성이 앞 라운드의 수정을
                # 되돌리기도 함). 마무리 멘트는 보류된 이행 라운드 텍스트를 여기서 방출한다.
                if (contract is not None and contract.done()
                        and getattr(self, "_turn_tool_errors", 0) == errors_before):
                    if getattr(self, "_last_text_deferred", False):
                        deferred_text = self._last_assistant_text()
                        if deferred_text:
                            yield {"type": "token", "text": deferred_text}
                    break
                if produced >= limit:
                    # 도구는 썼지만 계약 미이행으로 라운드 예산 소진. 실사고 패턴: haiku가
                    # update_diagram 같은 싸구려 도구로 강제만 때우고 코드는 텍스트로 덤프 —
                    # 이런 혼합 라운드도 ① 먼저 덤프에서 복구(salvage)하고 ② 안 되면 재시도.
                    if unmet:
                        if contract.question_exempt and self._last_response_asks_user_question():
                            if getattr(self, "_last_text_suppressed", False) or getattr(self, "_last_text_deferred", False):
                                yield {"type": "token", "text": self._last_assistant_text()}
                            self._turn_modify_clarified = True
                            completed_by_question = True
                            break
                        if (yield from self._salvage_code_dump()):
                            break
                        if retries_left > 0 and self._prepare_code_retry(contract, after_tools=True):
                            retries_left -= 1
                            yield {"type": "status", "message": "코드를 생성하고 있어요..."}
                            continue
                    break

            # 복구·재시도까지 소진하고도 계약 미이행 — 조용히 end로 끝내지 않는다.
            if contract is not None and not contract.done() and not completed_by_question and not self._cancelled:
                if contract.void_failed_text:
                    # 마지막 응답이 텍스트 덤프였다면 폐기 — _internal 마킹되어 아래 finally의
                    # drop이 히스토리에서 제거한다(다음 턴 컨텍스트에 수만 토큰 잔존 방지).
                    self.state.void_last_assistant_text(self._VOID_MARKER)
                elif getattr(self, "_last_text_deferred", False):
                    # 수정 턴에서 유저에게 보여주지 않고 보류했던 약속/완료 텍스트는 저장하지 않는다.
                    # 도구가 섞인 메시지는 페어링 보존을 위해 텍스트 블록만 제거한다.
                    if not self.state.drop_text_from_last_assistant_with_tools():
                        self.state.void_last_assistant_text(self._VOID_MARKER)
                elif getattr(self, "_last_text_suppressed", False):
                    # 마지막 응답 텍스트가 라이브 채팅에서 억제됐다(도구 강제 위반) —
                    # 히스토리에서도 폐기해 라이브/복원을 일치시키고, "완료!" 같은
                    # 거짓 성공 주장이 기록으로 남는 것을 막는다. (보였던 응답은 보존)
                    self.state.void_last_assistant_text(self._VOID_MARKER)
                setattr(self, contract.fail_flag, True)
                get_client().update_current_span(
                    level="ERROR", status_message=f"턴 계약 미이행 ({contract.fail_flag})")
                # 사과는 계약이 정한다: 신규 생성은 사과(산출물이 아예 없음), 수정 턴은 침묵 —
                # 라우터가 질문을 수정으로 오분류했을 수 있어 모델 답변을 그대로 두고 스코어만.
                # 조기 실패(rate limit 소진 등)는 _llm_call이 자체 안내를 이미 냈으므로 생략.
                if contract.apology and not getattr(self, "_last_call_failed", False):
                    # 내부 잔재를 먼저 지우고 사과를 '정식' assistant 메시지로 기록 —
                    # 히스토리가 [user(요청), assistant(사과)]가 되어 복원 채팅에도 그대로 보인다.
                    self.state.drop_internal_messages()
                    self.state.add_assistant_message([{"type": "text", "text": contract.apology}])
                    yield {"type": "token", "text": contract.apology}
        finally:
            # 내부 메시지(폐기 마커·nudge·수정 지시)는 어떤 종료 경로(성공/실패/취소/예외)로도
            # 턴 밖으로 새지 않게 제거 — 다음 턴 LLM 컨텍스트·요약·직렬화가 깨끗해지고,
            # 조기 실패 시 user(nudge)로 끝나 다음 턴과 user-user 연속이 되는 것도 막는다.
            # (내부 메시지는 정의상 '이번 루프 한정' 지시라 무조건 정리해도 안전)
            self.state.drop_internal_messages()

    def _implement_contract(self, intent: str | None) -> TurnContract | None:
        """이번 턴의 완료 계약을 상태(phase·coding_type·산출물)와 intent로 결정하는 단일 지점.

        - 구현 phase가 아니거나 대화 턴(question 등) → None (텍스트 완결이 정상)
        - 산출물이 아직 없음 → '산출물 생성' 계약: 처음부터 강제, 실패 덤프 폐기, 최종 실패 시 사과
        - 산출물 있음 + 수정/구현 요청 → '이번 턴 변경' 계약: 강제는 재시도부터, 응답 보존
          (질문 오분류 가능성), 최종 실패는 사과 없이 스코어만
        """
        if self.state.project.phase != Phase.IMPLEMENT:
            return None
        if intent in _NO_CODE_INTENTS:
            return None
        blockly = getattr(self, '_coding_type', 'react') == 'blockly'
        if not self._impl_artifact_ready():
            code_tool = "generate_blockly_xml" if blockly else "generate_code"
            return TurnContract(
                done=self._impl_artifact_ready,
                tool_hint=code_tool,
                question_exempt=False,
                # 산출물 도구를 '지목'해 강제한다. any로 걸면 소형 모델이 update_diagram
                # 하나로 조건만 때우고 코드를 텍스트로 쏟는다(실사고). CLI 리마인더도
                # "MUST call the `generate_code` tool"로 렌더된다. (API 모드에선 하드 강제라
                # plan_tasks-먼저 흐름이 막히지만, 산출물이 목적인 계약에선 감수 — 프로덕션은 CLI 소프트)
                force_choice={"type": "tool", "name": code_tool},
                force_from_start=True,
                void_failed_text=True,
                apology=("죄송해요, 이번에는 코드 생성이 제대로 되지 않았어요. "
                         "같은 요청을 한 번 더 보내주시면 다시 만들어볼게요."),
                fail_flag="_turn_code_failed",
            )
        if intent in ("modify_request", "implement_request"):
            return TurnContract(
                done=self.state.blockly_changed_this_turn if blockly else self.state.code_changed_this_turn,
                tool_hint="generate_blockly_xml" if blockly else "edit_code 또는 generate_code",
                question_exempt=True,
                # 수정은 edit_code/generate_code 둘 다 유효해 지목 불가 — any + nudge로
                force_choice={"type": "any"},
                force_from_start=False,
                void_failed_text=False,
                # 침묵하면 모델의 "수정 완료!" 거짓 주장만 남는다 — 정직하게 알린다.
                # (질문 오분류였어도 어색하지 않게 조건부 문구)
                apology=("혹시 코드를 바꿔달라는 요청이었다면 아직 적용되지 않았어요. "
                         "바꾸고 싶은 부분을 조금 더 구체적으로 말해주면 바로 고칠게요!"),
                fail_flag="_turn_modify_failed",
            )
        return None

    def _prepare_code_retry(self, contract: TurnContract, after_tools: bool = False) -> bool:
        """계약 미이행 응답의 재시도 준비(폐기/nudge). 재시도가 무의미하면 False.

        - 조기 실패(rate limit 등): 같은 이유로 또 실패한다 → 재시도 안 함
        - after_tools(도구는 썼지만 산출물 없음): nudge를 tool_result에 덧붙임
        - 텍스트-only + void 계약(신규 생성): 실패 덤프를 마커로 폐기 (role 교대 유지)
        - 텍스트-only + 수정 계약: 보류된 거짓 약속은 지우고, 행동만 다시 요구
        """
        if getattr(self, "_last_call_failed", False):
            return False
        if after_tools:
            if getattr(self, "_last_text_deferred", False):
                self.state.drop_text_from_last_assistant_with_tools()
            note = (f"\n\n⚠️ 아직 완료되지 않았습니다. 지금 바로 `{contract.tool_hint}` "
                    "도구를 호출해 작업을 완료하세요.")
            if not self._attach_note_to_last_tool_result(note):
                self.state.add_internal_user_message(note.strip())
        elif contract.void_failed_text:
            if not self.state.void_last_assistant_text(self._VOID_MARKER):
                return False  # 마지막이 텍스트 응답이 아님 — 폐기할 것도 없다
            self.state.add_internal_user_message(
                "방금 응답은 도구를 호출하지 않아 폐기했습니다. 코드를 설명이나 "
                f"코드블록 텍스트로 쓰지 말고, 반드시 `{contract.tool_hint}` 도구를 "
                "호출해 생성하세요. 지금 바로 도구로 출력하세요.")
        else:
            if getattr(self, "_last_text_deferred", False):
                self.state.void_last_assistant_text(self._VOID_MARKER)
            self.state.add_internal_user_message(
                "위 요청이 코드 수정 요청이라면 말로 안내하지 말고, 지금 바로 "
                f"`{contract.tool_hint}` 도구를 호출해 실제로 수정하세요.")
        self._turn_code_retries += 1
        self._log_code_failure(
            f"계약 미이행 {'(도구 호출은 있었으나 산출물 없음)' if after_tools else '(텍스트 응답)'}"
            f" — nudge 후 재시도 ({contract.fail_flag})")
        return True

    # 폐기된 구현 응답의 자리표시 마커 — 재시도 중 role 교대를 유지하다 턴 종료 시 drop된다.
    _VOID_MARKER = "(무효 처리됨: 코드를 도구 호출 없이 텍스트로 출력해 폐기)"

    def _impl_artifact_ready(self) -> bool:
        """구현 단계 산출물이 나왔는지 — react/hybrid는 코드맵, blockly는 XML이 산출물.
        (blockly를 코드맵으로 판정하면 성공 턴이 전부 '코드 미생성' 오탐이 된다.)"""
        if getattr(self, '_coding_type', 'react') == 'blockly':
            return bool(self.state.blockly_xml)
        return bool(self.state.generated_code_map)

    _FALSE_DONE_RE = re.compile(
        r"(수정|변경|적용|업데이트|업그레이드|개선|추가|구현|완성|완료|생성|제작|만들)"
        r".{0,16}(했|했습니다|됐|되었습니다|완료|끝났|적용했|바꿨|고쳤|만들었|생성했)"
    )
    _QUESTION_MARKERS = (
        "어떤", "무슨", "무엇", "뭐", "어느", "몇", "구체적으로", "필요", "원하",
        "말씀", "말해", "알려", "정해", "골라", "선택", "먼저", "추가", "기능",
        "스타일", "느낌", "방향", "색상", "크기", "종류", "which", "what", "choose",
        "tell me",
    )
    _RESET_TIME_RE = re.compile(r"resets?\s+([^\"}]+)", re.IGNORECASE)

    def _quota_limit_message(self, error_text: str) -> str:
        """Claude CLI 세션/사용량 한도 초과를 사용자에게 짧고 구체적으로 안내."""
        m = self._RESET_TIME_RE.search(error_text or "")
        reset = m.group(1).strip() if m else ""
        if reset:
            return f"Claude 사용 한도에 도달했어요. {reset} 이후에 다시 시도해주세요."
        return "Claude 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요."

    def _last_response_asks_user_question(self) -> bool:
        """수정 계약에서 '무변경 실패'가 아니라 유저에게 요구사항을 물은 정상 턴인지 판정.

        비용 방어 계약이 잡아야 할 것은 "수정했어!"라고 말만 하고 끝나는 거짓 완료다.
        반대로 "어떤 기능을 원해요?"처럼 필요한 정보를 묻는 응답은 유저에게 턴을 넘긴
        정상 대화이므로 재시도·실패 스코어를 태우지 않는다.
        """
        text = (getattr(self, "_last_model_text", "") or "").strip()
        if not text:
            text = self._last_assistant_text()
        return self._text_asks_user_question(text)

    def _last_assistant_question_text(self) -> str:
        text = self._last_assistant_text()
        return text if self._text_asks_user_question(text) else ""

    def _last_assistant_text(self) -> str:
        from agent.context import _extract_text
        for msg in reversed(self.state._messages):
            if msg.get("_internal"):
                continue
            if msg.get("role") == "assistant":
                return "\n".join(
                    t for b in msg.get("content", []) if (t := _extract_text(b))
                ).strip()
        return ""

    def _text_asks_user_question(self, text: str) -> bool:
        if not text:
            return False
        normalized = re.sub(r"\s+", " ", text).lower()
        if self._FALSE_DONE_RE.search(normalized):
            return False
        has_question_shape = (
            "?" in normalized
            or "？" in normalized
            or re.search(r"(까요|나요|일까요|을까요|말해\s*주|말씀해\s*주|알려\s*주|정해\s*주|골라\s*주|선택해)", normalized)
        )
        if not has_question_shape:
            return False
        return any(marker in normalized for marker in self._QUESTION_MARKERS)

    def _log_code_failure(self, message: str) -> None:
        """산출물 미생성 실패를 Langfuse 타임라인에 빨간 이벤트로 — 트레이스에서 한눈에 보이게."""
        try:
            with get_client().start_as_current_observation(
                    name="구현 실패 · 코드 산출물 없음", as_type="span") as span:
                span.update(level="ERROR", status_message=message)
        except Exception:
            pass

    def _salvage_code_dump(self) -> Generator[dict, None, bool]:
        """도구 호출 없이 끝난 구현 응답의 텍스트에서 코드펜스를 generate_code 호출로 복구.

        성공하면 덤프를 '원래 냈어야 할 형태'(tool_use 블록)로 되살리고 도구를 실제로
        실행해 산출물을 만든다 — LLM 재호출이 없으니 추가 비용 0.
        복구할 게 없으면 False (호출부가 폐기+재시도로 폴백).

        두 가지 라운드 모두 지원:
        - 순수 텍스트 응답: 그 메시지를 tool_use 형태로 재작성 (수만 토큰 덤프 소멸)
        - 혼합 라운드(싸구려 도구 + 텍스트 덤프, 예: update_diagram만 부르고 코드는 텍스트):
          기존 tool_use↔tool_result 페어링을 깨지 않도록 원본은 보존하고,
          복구 tool_use를 '새 assistant 메시지'로 덧붙인다 (role 교대 유지)."""
        if getattr(self, '_coding_type', 'react') == 'blockly':
            return False  # blockly XML은 격자 등 부속 정보가 필요해 텍스트 복구 대상이 아님
        from agent.context import _extract_text
        msgs = self.state._messages
        # 말미의 tool_result(user, 리스트 content)들을 건너뛰고 마지막 assistant를 찾는다
        idx = len(msgs) - 1
        while idx >= 0 and msgs[idx]["role"] == "user" and isinstance(msgs[idx]["content"], list):
            idx -= 1
        if idx < 0 or msgs[idx]["role"] != "assistant":
            return False
        dump = "\n".join(t for b in msgs[idx]["content"] if (t := _extract_text(b)))
        # 수정 턴이면 기존 파일 경로를 알려줘 부분 덤프(일부 파일만)도 복구 가능하게
        files = extract_code_files(dump, known_paths=self.state.generated_code_map.keys())
        if not files:
            return False

        tool_uses = [
            ToolUseBlock(
                id=f"toolu_salvaged_{uuid.uuid4().hex[:12]}",
                name="generate_code",
                input={"file_path": path, "code": code,
                       "description": "텍스트 응답에서 복구된 파일"},
            )
            for path, code in files
        ]
        pure_text_response = idx == len(msgs) - 1
        if pure_text_response:
            # 덤프 앞머리의 짧은 안내 문장("~를 만들게요")은 살려서 채팅 버블로 보여준다
            intro_lines = dump.split("```", 1)[0].strip().splitlines()
            intro = intro_lines[0].strip() if intro_lines else ""
            if len(intro) > 120:
                intro = ""
            self.state.replace_last_assistant_with_tool_uses(intro, tool_uses)
            if intro:
                yield {"type": "token", "text": intro}
        else:
            # 혼합 라운드: 원본(도구+결과 페어) 보존, 복구 호출은 새 assistant로
            self.state.add_assistant_message([
                {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
                for tu in tool_uses
            ])
        self._turn_code_salvaged = getattr(self, "_turn_code_salvaged", 0) + len(files)
        try:
            get_client().create_event(
                name="구현 복구 · 텍스트 덤프에서 코드 추출",
                metadata={"files": [p for p, _ in files], "mixed_round": not pure_text_response},
                level="WARNING",
            )
        except Exception:
            pass
        yield {"type": "status", "message": "응답에서 코드를 복구하고 있어요..."}
        yield from self._execute_tool_groups(tool_uses)
        return self._impl_artifact_ready()

    def _can_transition_to_implement(self) -> tuple:
        """구현 전환 가능 여부 — 충분히 협의했는지(최소 턴)만 본다.
        유저가 '만들자'고 하면 기능/페이지/데이터 같은 건 막지 말고, 전체 대화를 바탕으로
        구현 단계에서 알아서 채운다(react·blockly 공통). '핵심 기능이 아직...' 같은 거부 금지."""
        missing = []
        if self.state.design_turns < MIN_DESIGN_TURNS:
            missing.append(f"조금만 더 같이 설계해요 (최소 {MIN_DESIGN_TURNS}번은 주고받기)")
        return (len(missing) == 0, missing)

    def _handle_phase_change(self, user_input: str) -> str:
        """phase 전환을 Langfuse span 으로 감싸 from→to 를 타임라인에 남긴다."""
        _from = str(self.state.project.phase)
        with get_client().start_as_current_observation(
                name="단계 전환 (phase_change)", as_type="span",
                input={"from": _from}) as _sp:
            result = self._handle_phase_change_impl(user_input)
            _sp.update(output={"from": _from, "to": self.get_phase()})
            return result

    def _handle_phase_change_impl(self, user_input: str) -> str:
        current = self.state.project.phase

        if current == Phase.DESIGN:
            can, missing = self._can_transition_to_implement()
            if not can:
                return "아직 만들 준비가 덜 됐어요:\n" + "\n".join(f"- {m}" for m in missing) + "\n\n조금만 더 같이 정해봐요!"

            self.state.project.phase = Phase.IMPLEMENT

            # blockly: 설계 문서(기능/페이지) 대신 대화로 협의 → 가벼운 안내 후 바로 블록 생성으로
            if getattr(self, '_coding_type', 'react') == 'blockly':
                return "좋아요! 지금까지 함께 정한 내용으로 블록을 만들어볼게요."

            doc = self.state.project.design_doc
            diagram = self.state.diagram_manager.get_mermaid()

            summary = f"**{doc.project_name}** 구현을 시작할게요!\n\n"
            summary += "설계 요약:\n"
            summary += f"- 사용자: {', '.join(doc.users)}\n"
            summary += f"- 기능 {len(doc.features)}개, 페이지 {len(doc.pages)}개\n"
            if doc.strengths:
                summary += f"- 강점: {', '.join(doc.strengths)}\n"
            if doc.weaknesses:
                summary += f"- 보완점: {', '.join(doc.weaknesses)}\n"
            if diagram != "(아직 다이어그램이 없습니다)":
                summary += f"\n```mermaid\n{diagram}\n```\n"
            summary += "\n태스크를 생성하고 순서대로 구현할게요."
            return summary

        elif current == Phase.IMPLEMENT:
            self.state.project.phase = Phase.VERIFY
            return "검증 단계로 넘어갈게요! 설계와 구현이 일치하는지 확인해볼게요."

        elif current == Phase.VERIFY:
            self.state.project.phase = Phase.IMPLEMENT
            return "구현 단계로 돌아갈게요. 수정할 부분을 말해주세요!"

        return "Phase 전환을 처리할 수 없습니다."

    def _code_validated_event(self) -> dict:
        """검증·수정이 끝난 '실행 가능한' 코드맵을 보낸다. 프론트는 이 시점에 미리보기를 렌더한다
        (학습노트·설계 등 후처리 완료를 기다리는 done보다 먼저 도착)."""
        return {
            "type": "code_validated",
            "generated_code": dict(self.state.generated_code_map) if self.state.generated_code_map else None,
            "app_type": self.state.app_type or None,
            "phase": self.get_phase(),
        }

    def _maybe_direct_serve(self, query: str) -> Generator[dict, None, bool]:
        """#84 직접서브 티어: 재사용 고신뢰(reuse) 후보를 값싼 만족도 검증으로 판정해,
        accept 면 저장물을 **LLM 생성 없이 그대로** 세션 코드로 채운다(생성 LLM=0 → 비용·속도 win).

        반환(StopIteration.value): 직접서브했으면 True → 호출측은 생성 루프를 건너뛴다.

        전제(D3 실측, #84): 직접서브는 요청이 저장물에 **새 델타를 안 더할 때만** 만족(95~100).
        유사도(주제)만으로는 델타 유무를 못 가리므로(예 "파란 하트"는 유사도 高이나 색델타로 실패),
        decision=='reuse'(유사도 高) 일 때만 만족도 검증 1회를 태우고 accept 여야 서브한다.
        먼저 _reuse_block 이 stash 한 후보/플래그를 소비한다(게이트 재검색 없음).

        EDU-27: accept 후 files 뿐 아니라 같은 세션의 학습노트/설계문서도 함께 복원한다
        (direct_serve.restore_docs — writeback 동봉 우선, 없으면 세션 조인 폴백).
        """
        if not direct_serve.ENABLED:
            return False
        flag = self._reuse_flag or {}
        if flag.get("decision") != "reuse":  # review/register/None 은 직접서브 대상 아님
            return False
        # 이미 코드가 있는 수정 턴은 직접서브 안 함(사용자가 현재 산출물을 바꾸려는 것).
        if self.state.generated_code_map:
            return False
        cand = self._reuse_candidate or {}
        files = (cand.get("payload") or {}).get("files")
        if not isinstance(files, dict) or not files:
            return False

        verdict = direct_serve.check_satisfaction(self.client, self.model_fast, query, cand)
        self._direct_served = {
            "score": verdict.get("score"), "accept": bool(verdict.get("accept")),
            "delta": bool(verdict.get("delta")), "ok": bool(verdict.get("ok")),
            "source_title": cand.get("title"),
        }
        if not verdict.get("accept"):
            return False

        # accept: 저장물 파일을 그대로 세션 코드에 채운다(생성 LLM 미호출). 저장물은 등록 시
        # outcome=success 로 검증을 통과한 것이라 빌드체크를 다시 돌리지 않는다(속도 win 보존).
        served = 0
        for fn, code in files.items():
            if fn and isinstance(code, str):
                self.state.generated_code_map[fn] = code
                served += 1
        if not served:
            self._direct_served["accept"] = False
            return False
        # EDU-27: 직접서브는 생성 루프를 건너뛰므로 add_learning_note/update_design_doc 이
        # 호출되지 않아 문서 탭이 비었다(실유저 트레이스로 확인). writeback 동봉(payload.docs)
        # 우선, 없으면 세션 조인 폴백으로 복원 — 문서는 부가물이라 실패해도 코드 서브는 유지.
        try:
            docs_restored = direct_serve.restore_docs(self.state, cand)
        except Exception:
            docs_restored = 0
        self._direct_served["docs_restored"] = docs_restored
        yield {"type": "status", "message": "이전에 만든 결과물이 이 요청에 딱 맞아 그대로 불러왔어요..."}
        return True

    def _reuse_block(self, query: str, coding_type: str) -> tuple[str | None, str | None]:
        """#44 재사용 게이트: 빌드 직전 검색 → reuse/review 면 (프라임 블록, 상태메시지).

        후보 없으면 (None, None). self._reuse_flag 세팅.
        LLM 추가 호출 없음(검색 1회). 두 빌드 진입점(명시 구현 · 설계→전환)에서 공유.

        #68 O3: 수정 턴(이미 산출물 코드 있음)도 재사용 검색을 돌려 재사용 코호트(#67 T3)를
        기록하고 경량 온톨로지 제안(개념·선수학습 + 결과물 '요약')은 주입한다. 단, 유사 결과물
        **전체 코드**를 다시 싣는 무거운 프라임(_prime)은 콜드 빌드 전용으로 남긴다 — 수정 턴은
        이미 편집 대상 코드가 프롬프트에 있어 또 실으면 입력이 붓고 전체 재작성을 유발(#68 O2 상충).
        _REUSE_ON_MODIFY=0 이면 수정 턴은 종전대로 즉시 (None, None)(무회귀 킬스위치).
        """
        is_modify = bool(self.state.generated_code_map)  # 수정 턴 = 진입 시 이미 코드가 있음
        # 프라임 조립은 prime_service 단일 소스에 위임 — 시뮬레이터(/api/simulate)와 100% 동일.
        # 여기서는 인스턴스 상태(seen 누적 / 관측 플래그)만 반영한다.
        seen = getattr(self, "_suggested_keys", None)
        res = prime_service.assemble_prime(query, coding_type, is_modify=is_modify,
                                           user_id=self._user_id, seen=seen)
        if res.ontology:
            if seen is None:
                self._suggested_keys = seen = set()
            seen.update(res.ontology.get("seen_keys") or [])
            # #27 관측/검증: 이번 턴에 온톨로지 프라임이 실제 주입됐음을 done 이벤트로 노출.
            self._ontology_primed = {
                "concept": (res.ontology.get("primary") or {}).get("label"),
                "prerequisites": len(res.ontology.get("prerequisites") or []),
                "modi_modules": len(res.ontology.get("modi_modules") or []),
                "cards": len(res.ontology.get("cards") or []),
                "artifacts": len(res.ontology.get("artifacts") or []),
            }
        if res.reuse_gate:
            g = res.reuse_gate
            # 재사용 결정은 콜드·수정 공통으로 기록(#68 O3 코호트 관측 = #67 T3 확장).
            # near-miss(#84 후속): register/none 도 오므로 candidate 는 없을 수 있다.
            self._reuse_flag = {"decision": g["decision"], "top1": g.get("top1"),
                                "kind": g["kind"], "cand_score": g.get("cand_score"),
                                "source_title": (g.get("candidate") or {}).get("title"),
                                # EDU-27 vec 승격 관측: 이 재구성이 vec 필드를 떨어뜨려
                                # reuse_vec_promoted 스코어가 프로덕션에서 미발행되던 누락 수정(PR#89 후속).
                                "vec": g.get("vec"), "vec_promoted": g.get("vec_promoted")}
            # #84 직접서브 판정용: 최상위 후보 원본을 stash(files 포함). _maybe_direct_serve 가 소비.
            self._reuse_candidate = g.get("candidate")
        block, msg = res.block, res.status_msg
        # #EDU-27 재사용 시드편집: reuse 티어(거의 동일) 콜드 빌드는 후보 코드를 세션에 시드해
        # "수정 턴"으로 만든다 → edit_code(diff)만 출력하도록 유도(전체재작성 낭비 제거).
        # 앞단(_chat_stream_impl 코드주입/O2 유도)은 진입 시 빈 맵이라 콜드에선 아무것도 안 붙였으므로
        # 여기서 코드컨텍스트 + 계약 + edit 지시를 직접 얹는다(중복 없음). review 티어는 시드 안 함(예시 참고).
        if (prime_service.REUSE_SEED_EDIT and not is_modify and res.reuse_gate
                and res.reuse_gate.get("decision") == "reuse"):
            cand = res.reuse_gate.get("candidate") or {}
            files = (cand.get("payload") or {}).get("files")
            if isinstance(files, dict) and files:
                for fn, code in files.items():
                    if fn and isinstance(code, str):
                        self.state.generated_code_map[fn] = code
                edit_parts = [self.state.get_current_code_context(), REUSE_SEED_HEAD,
                              CODE_TOOL_CONTRACT, MODIFY_EDIT_DIRECTIVE]
                edit_block = "\n\n".join(p for p in edit_parts if p)
                block = (block + "\n\n" + edit_block) if block else edit_block
                msg = "이전 결과물을 불러와 편집해서 만들어요..."
                self._reuse_seeded = True  # 관측/테스트용: 이번 턴 재사용 코드가 시드됐는가
        return block, msg

    def _done_event(self) -> dict:
        # 검증 실패 산출물은 _reject_failed_artifacts가 이미 state에서 롤백했으므로,
        # 여기서는 state를 그대로 내보내면 된다(state = 항상 검증 통과분).
        generated_code = dict(self.state.generated_code_map) if self.state.generated_code_map else None
        doc = self.state.project.design_doc
        design_doc_data = None
        if doc.features or doc.pages:
            design_doc_data = {
                "project_name": doc.project_name,
                "description": doc.description,
                "users": doc.users,
                "features": [{"name": f.name, "description": f.description, "priority": f.priority} for f in doc.features],
                "pages": [{"name": p.name, "description": p.description} for p in doc.pages],
                "data_models": [{"name": d.name, "fields": d.fields} for d in doc.data_models],
                "user_flows": doc.user_flows,
                "strengths": doc.strengths,
                "weaknesses": doc.weaknesses,
            }
        task_plan = self.state.project.task_plan
        task_data = None
        if task_plan.tasks:
            task_data = {
                "tasks": [{"id": t.id, "name": t.name, "status": t.status, "files": t.files} for t in task_plan.tasks],
                "progress": task_plan.progress_summary(),
            }
        # 이번 턴에 새로 생긴 것만 프론트에 전달 (중복 방지)
        new_notes = self.state.get_new_learning_notes()
        new_annotations = self.state.get_new_code_annotations()

        # 에이전트 스텝 데이터
        agent_steps_data = None
        if self._turn_steps:
            agent_steps_data = {
                "steps": [
                    {"step": s["step"], "description": s["description"], "action": s["action"], "status": s["status"]}
                    for s in self._turn_steps
                ],
                "duration": round(time.time() - self._turn_start_time),
                "summary": self._extract_summary(),
            }
            self._tag_agent_steps(agent_steps_data)

        return {
            "type": "done",
            "phase": self.get_phase(),
            "diagram": self.get_diagram(),
            "generated_code": generated_code,
            "blockly_xml": self.state.blockly_xml or None,
            "blockly_flowchart": self.state.blockly_flowchart or None,
            "blockly_detail": self.state.blockly_detail or None,
            "blockly_code_langs": self.state.blockly_code_langs or None,
            "modi_modules": self.state.modi_modules or None,
            "design_doc": design_doc_data,
            "task_plan": task_data,
            "learning_notes": new_notes if new_notes else None,
            "code_annotations": new_annotations if new_annotations else None,
            "app_type": self.state.app_type or None,
            "agent_steps": agent_steps_data,
            "reused": self._reuse_flag,  # #44 재사용 게이팅 결과(None=신규 생성)
            "direct_served": self._direct_served,  # #84 직접서브 만족도 판정(None=미판정)
            "ontology_primed": self._ontology_primed,  # #27 온톨로지 프라임 주입 결과(None=미주입)
        }

    def _tag_agent_steps(self, agent_steps_data: dict) -> None:
        """에이전트 스텝 데이터를 이번 턴의 적절한 assistant 메시지에 태깅 (히스토리 직렬화용)."""
        from agent.context import _extract_text
        fallback = None
        target = None
        for msg in reversed(self.state._messages):
            if msg["role"] == "user" and isinstance(msg["content"], str):
                break
            if msg["role"] == "assistant":
                if fallback is None:
                    fallback = msg
                if any(_extract_text(b) for b in msg["content"]):
                    target = msg
                    break
        target = target or fallback
        if target is not None:
            target["_agent_steps"] = agent_steps_data

    def get_phase(self) -> str:
        phase_labels = {
            Phase.DESIGN: "설계",
            Phase.IMPLEMENT: "구현",
            Phase.VERIFY: "검증"
        }
        return phase_labels.get(self.state.project.phase, "Unknown")

    def get_diagram(self) -> str:
        return self.state.diagram_manager.get_mermaid()

    def reset(self):
        self.state.reset()
