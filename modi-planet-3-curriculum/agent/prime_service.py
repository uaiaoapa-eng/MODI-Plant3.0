"""온톨로지 RAG 분기·프라임의 단일 소스(Single Source of Truth).

`/chat`(agent/orchestrator_stream.py)과 시뮬레이터(server.py `/api/simulate`)가
**같은 함수**를 태워 항상 동일한 결과를 내도록, intent 분류(규칙 기반)와 프라임 조립을
오케스트레이터 인스턴스에서 떼어낸 순수 모듈이다. LLM 추가 호출 없음.

경계:
- `resolve_intent`  : 규칙(정규식 Router) 기반 intent 분류 — 인스턴스 상태 비의존.
- `assemble_prime`  : 코드 턴에서 온톨로지 제안 + 재사용 게이트를 프롬프트 블록으로 조립.
- `build_prime`     : 위 둘을 묶어 "이 입력에서 /chat이 온톨로지 RAG를 태우는가/무엇을
                       주입하는가"를 한 번에 반환(시뮬레이터/관측용).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from agent.models import Phase

# 코드 도구를 쓰는 intent(= 온톨로지 RAG 발동 대상). 파일 내 단일 정의 —
# orchestrator_stream.py 가 여기서 import 한다(중복 정의 금지).
CODE_ACTION_INTENTS = {"modify_request", "implement_request"}
NO_CODE_INTENTS = {"chat", "question", "clarify_request"}

# #68 O3: 수정 턴 재사용 게이트 완화 스위치(기본 ON). 0/false/no 면 수정 턴은 즉시 빈 프라임.
REUSE_ON_MODIFY = (os.getenv("REUSE_ON_MODIFY", "1") or "").strip().lower() not in ("0", "false", "no", "")

# #EDU-27 재사용 시드편집 스위치(#84 이후 기본 OFF). ON 이면 reuse 티어 콜드 빌드에서 후보
# 코드를 세션에 시드→edit_code(diff) 강제로 유도한다. 그러나 D3 실측에서 콜드 빌드 3축(비용
# 2,408토큰 최악·속도 84s·품질 빌드실패) 모두 순손해로 확인돼(#84) 기본 OFF 로 내렸다 —
# 콜드 재사용의 정답은 시드편집이 아니라 ①델타無=직접서브(agent/direct_serve.py) ②델타有 근접=
# 참고프라임+자유 generate 다. 킬스위치는 유지(REUSE_SEED_EDIT=1 로 옛 동작 복구 가능).
REUSE_SEED_EDIT = (os.getenv("REUSE_SEED_EDIT", "0") or "").strip().lower() not in ("0", "false", "no", "")


def code_action(intent: str, phase: Phase) -> bool:
    """온톨로지 RAG(프라임)를 태우는 턴인가 = IMPLEMENT phase 의 코드 요청 intent."""
    return phase == Phase.IMPLEMENT and intent in CODE_ACTION_INTENTS


def resolve_intent(message: str, mode: str, phase: Phase, *,
                   has_code: bool = False, is_clarification_answer: bool = False,
                   router=None, quick_ready: bool | None = None) -> str:
    """한 턴의 의도 분류(규칙 기반, LLM 미사용).

    orchestrator_stream._classify_turn_intent 의 **인스턴스 비의존 로직**을 이관한 것.
      - has_code               : 이미 산출물(코드/XML)이 있는 세션인가(= _impl_artifact_ready).
      - is_clarification_answer: 직전 assistant 질문에 대한 구체 답변인가.
      - quick_ready            : quick 첫 턴 판정에 쓰는 산출물 준비 여부(기본 has_code).
    Router 는 무상태이므로 미주입 시 지연 생성.
    """
    if router is None:
        from agent.router import Router
        router = Router()

    ready = has_code if quick_ready is None else quick_ready
    if mode == "quick" and not ready:
        return _classify_quick_initial(message, router)

    known = router.classify_known(message, phase)
    if known:
        # 이미 산출물이 있는 IMPLEMENT 에서 "기능/화면 추가"는 신규 생성이 아니라 기존 변경.
        if known == "implement_request" and has_code:
            return "modify_request"
        return known

    if phase == Phase.IMPLEMENT and has_code:
        # 명시적 수정 키워드 없는 애매한 후속 피드백: 기본은 대화/확인, 단 직전 질문의 답이면 수정.
        return "modify_request" if is_clarification_answer else "clarify_request"

    return router.classify(message, phase)


def _classify_quick_initial(message: str, router) -> str:
    """quick 첫 턴: 질문/잡담/전환만 규칙으로 걸러내고 나머지는 바로 구현."""
    known = router.classify_known(message, Phase.IMPLEMENT)
    if known in ("chat", "question", "phase_change"):
        return known
    if known in ("modify_request", "implement_request"):
        return "implement_request"
    return "implement_request"


@dataclass
class PrimeResult:
    """프라임 조립 결과(assemble_prime 반환). 오케스트레이터가 인스턴스 상태로 반영한다."""
    block: str | None = None          # 시스템 프롬프트 말단에 붙일 프라임 블록
    status_msg: str | None = None     # 사용자 표시 상태 메시지
    ontology: dict | None = None      # ontology_suggest 결과(primary/prereq/related/modi/cards/artifacts/seen_keys)
    reuse_gate: dict | None = None    # reuse.gate 결과(decision/kind/candidate/top1)


def assemble_prime(query: str, coding_type: str, *, is_modify: bool,
                   user_id: str | None = None, seen: set | None = None) -> PrimeResult:
    """코드 턴 프라임 조립 — orchestrator_stream._reuse_block(2366-2414)의 단일 소스 이관.

    반환 PrimeResult 는 (block, status_msg) 외에 온톨로지/게이트 원본을 담아,
    호출측이 관측 상태(_ontology_primed/_reuse_flag/seen)를 반영할 수 있게 한다.
    LLM 추가 호출 없음(검색 1회 + SQLite 그래프 조회). 어떤 예외도 삼켜 chat 을 깨지 않는다.
    """
    if is_modify and not REUSE_ON_MODIFY:
        return PrimeResult()

    blocks: list[str] = []
    msg: str | None = None
    ontology: dict | None = None
    reuse_gate: dict | None = None

    # 제안형 온톨로지 프라임(#27): 개념·선수학습·연관 + MODI·카드 + 유사 결과물을 '각색' 전제로.
    try:
        from agent.reuse import ontology_suggest as _suggest
        sug = _suggest(query, coding_type, user_id, seen=seen)
    except Exception:
        sug = None
    if sug and sug.get("ok") and sug.get("block"):
        blocks.append(sug["block"])
        ontology = sug

    # 코드 재사용 게이트(#44): reuse/review 면 전체 코드 프라임(콜드 빌드 전용, 구조 유지 최소 수정).
    try:
        from agent.reuse import gate as _gate, prime as _prime
        g = _gate(query, "implement_request", coding_type, user_id)
    except Exception:
        g = None
    if g:
        # near-miss 계측(#84 후속): register/none 도 표면화해 top1 분포를 관측에 남긴다.
        # 프라임 주입은 아래 reuse/review 분기만 탄다(동작 무변경).
        reuse_gate = g
    if g and g.get("decision") in ("reuse", "review"):
        if not is_modify:
            # reuse + 시드편집 ON: heavy 코드 프라임(전체재작성 유도)을 붙이지 않는다 —
            # 오케스트레이터가 후보 코드를 세션에 시드하고 edit_code(diff) 유도 블록을 대신 붙인다.
            # (stateless 시뮬레이터는 세션이 없어 시드를 못 하므로 reuse_gate 로만 관측된다.)
            if g["decision"] == "reuse" and REUSE_SEED_EDIT:
                pass
            else:
                cblock = _prime(g["candidate"].get("payload") or {}, g["decision"])
                if cblock:
                    blocks.append(cblock)
                    msg = ("비슷한 결과가 있어 그걸 기반으로 만들어요..." if g["decision"] == "reuse"
                           else "비슷한 예시를 참고해 만들어요...")

    if not blocks:
        return PrimeResult(ontology=ontology, reuse_gate=reuse_gate)
    if msg is None:  # 코드 재사용 없이 온톨로지 제안만 있음
        msg = "관련 개념과 자료를 참고해 만들어요..."
    return PrimeResult(block="\n\n".join(blocks), status_msg=msg,
                       ontology=ontology, reuse_gate=reuse_gate)


@dataclass
class PrimeBundle:
    """시뮬레이터/관측용 종합 결과 — /chat 이 이 입력에서 온톨로지 RAG를 어떻게 처리하는가."""
    intent: str
    code_action: bool
    is_modify: bool
    injected: bool = False
    prime_block: str | None = None
    status_msg: str | None = None
    ontology: dict | None = None
    reuse_gate: dict | None = None


def build_prime(message: str, coding_type: str = "react", *, phase: Phase = Phase.IMPLEMENT,
                has_code: bool = False, mode: str = "quick", user_id: str | None = None,
                seen: set | None = None, is_clarification_answer: bool = False,
                router=None) -> PrimeBundle:
    """intent 분류 → 분기 → (코드 턴이면) 프라임 조립을 한 번에. LLM 미호출.

    /chat 과 동일한 함수(resolve_intent + assemble_prime)를 태워, 시뮬레이터가 실제 주입
    프라임(prime_block)과 분기(code_action)를 그대로 재현하게 한다.
    """
    intent = resolve_intent(message, mode, phase, has_code=has_code,
                            is_clarification_answer=is_clarification_answer, router=router)
    ca = code_action(intent, phase)
    if not ca:
        return PrimeBundle(intent=intent, code_action=False, is_modify=has_code)

    res = assemble_prime(message, coding_type, is_modify=has_code, user_id=user_id, seen=seen)
    return PrimeBundle(
        intent=intent, code_action=True, is_modify=has_code,
        injected=bool(res.block), prime_block=res.block, status_msg=res.status_msg,
        ontology=res.ontology, reuse_gate=res.reuse_gate,
    )
