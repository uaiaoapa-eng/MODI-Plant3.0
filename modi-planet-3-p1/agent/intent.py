from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langfuse import get_client

from agent.usage import update_generation as _update_generation


ARTIFACT_FOLLOWUP_INTENT_SYSTEM = """\
당신은 코딩 생성 서비스의 후속 대화 의도 분류기입니다.
이미 만들어진 결과물이 있는 IMPLEMENT 단계에서, 사용자 발화가 코드 변경으로 이어져야 하는지 판정합니다.

JSON 객체 하나만 출력하세요:
{
  "intent": "modify_request" | "question" | "chat" | "clarify_request" | "continue_pending_action",
  "confidence": 0.0,
  "target": ["변경 대상"],
  "change_request": "구체적인 변경 내용",
  "needs_clarification": false,
  "reason": "짧은 근거"
}

판정 기준:
- modify_request: 사용자가 결과물의 구체적인 변경, 누락, 오작동, 시각/동작 개선을 원함.
- continue_pending_action: 직전 assistant가 작업 진행을 제안/약속했고 사용자가 짧게 동의함.
- clarify_request: 변경 의도는 있어 보이지만 대상이나 방향이 부족함.
- question: 결과물/코드 설명을 요구함.
- chat: 감사, 단순 반응, 종료 등 추가 행동 기대가 없음.

주의:
- "별로야", "아쉬워", "느낌이 안 나"처럼 방향이 부족하면 needs_clarification=true.
- modify_request라도 target 또는 change_request가 비어 있으면 needs_clarification=true.
- 확신이 낮으면 confidence를 낮추고 clarify_request로 분류하세요.
"""

_FOLLOWUP_INTENTS = {
    "modify_request",
    "question",
    "chat",
    "clarify_request",
    "continue_pending_action",
}


@dataclass(frozen=True)
class ArtifactFollowupIntent:
    intent: str
    confidence: float
    target: tuple[str, ...]
    change_request: str
    needs_clarification: bool
    reason: str = ""


def classify_artifact_followup_intent(
        client, model: str, payload: dict, known_intent: str | None, phase: str) -> ArtifactFollowupIntent | None:
    """Classify an ambiguous follow-up utterance for an existing artifact.

    This only returns a structured classifier decision. The caller must still
    apply route_artifact_followup_decision before executing code changes.
    """
    try:
        with get_client().start_as_current_observation(
                name="의도 분류 (artifact_followup)", as_type="generation",
                input=payload) as _gen:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=ARTIFACT_FOLLOWUP_INTENT_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            text = first_response_text(response)
            data = _parse_json_object(text)
            decision = artifact_followup_intent_from_json(data)
            routed_intent = route_artifact_followup_decision(
                decision,
                has_pending_action=assistant_suggested_action(payload.get("last_assistant_message", "")),
            )
            _update_generation(
                _gen, model, response=response, output={**data, "routed_intent": routed_intent},
                step="intent_router", known_intent=known_intent, phase=phase)
            return decision
    except Exception as exc:
        try:
            get_client().create_event(
                name="의도 분류 실패 (artifact_followup)",
                metadata={"error": str(exc)[:300], "known_intent": known_intent},
                level="WARNING",
            )
        except Exception:
            pass
        return None


def classify_existing_artifact_intent(
        client,
        model: str,
        user_input: str,
        known_intent: str | None,
        *,
        phase: str,
        coding_type: str,
        artifact_files: list[str],
        last_assistant_message: str,
        is_clarification_answer: bool = False,
) -> str:
    """Route a follow-up utterance when an implementation artifact already exists."""
    if known_intent == "phase_change":
        return known_intent
    if known_intent in ("modify_request", "implement_request"):
        return "modify_request"
    if looks_like_stop_or_delay_request(user_input):
        return "chat"
    if looks_like_short_confirmation(user_input) and assistant_suggested_action(last_assistant_message):
        return "modify_request"
    if looks_like_continue_request(user_input) and assistant_suggested_action(last_assistant_message):
        return "modify_request"
    if looks_like_no_problem_feedback(user_input):
        return "chat"
    if looks_like_artifact_defect_report(user_input):
        return "modify_request"
    if known_intent == "chat":
        return "chat"
    if known_intent == "question" and not question_needs_artifact_intent_check(user_input):
        return "question"
    if is_clarification_answer and not known_intent:
        return "modify_request"

    payload = {
        "user_message": user_input,
        "known_rule_intent": known_intent,
        "last_assistant_message": last_assistant_message,
        "artifact_files": artifact_files,
        "coding_type": coding_type,
    }
    decision = classify_artifact_followup_intent(
        client,
        model,
        payload,
        known_intent=known_intent,
        phase=phase,
    )
    if not decision:
        return "question" if known_intent == "question" else "clarify_request"
    return route_artifact_followup_decision(
        decision,
        has_pending_action=assistant_suggested_action(last_assistant_message),
    )


def route_artifact_followup_decision(
        decision: ArtifactFollowupIntent, *, has_pending_action: bool = False) -> str:
    """Convert an LLM classifier decision into an executable route."""
    if decision.intent in ("chat", "question"):
        return decision.intent
    if decision.intent == "continue_pending_action":
        return "modify_request" if has_pending_action else "clarify_request"
    if decision.intent != "modify_request":
        return "clarify_request"

    has_action_detail = bool(decision.target or decision.change_request)
    if decision.confidence >= 0.75 and has_action_detail and not decision.needs_clarification:
        return "modify_request"
    return "clarify_request"


def artifact_followup_intent_from_json(data: dict) -> ArtifactFollowupIntent:
    raw_targets = data.get("target") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    targets = tuple(str(item).strip() for item in raw_targets if str(item).strip())
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    intent = str(data.get("intent") or "clarify_request").strip()
    if intent not in _FOLLOWUP_INTENTS:
        intent = "clarify_request"
    return ArtifactFollowupIntent(
        intent=intent,
        confidence=confidence,
        target=targets,
        change_request=str(data.get("change_request") or "").strip(),
        needs_clarification=_json_bool(data.get("needs_clarification")),
        reason=str(data.get("reason") or "").strip(),
    )


def question_needs_artifact_intent_check(user_input: str) -> bool:
    """Whether a question-shaped utterance may be asking to change the artifact."""
    text = re.sub(r"\s+", " ", (user_input or "").strip().lower())
    change_markers = (
        "더", "좀", "조금", "크게", "작게", "밝게", "어둡게", "잘 보", "안 보",
        "넣", "추가", "빼", "없애", "바꾸", "수정", "고치", "개선", "강조",
        "줄이", "늘리", "움직", "보이게", "같이", "처럼", "느낌", "스타일",
    )
    question_action_markers = ("할 수", "가능", "되나", "될까", "돼", "어때", "좋을까", "나을까")
    return (
        any(marker in text for marker in change_markers)
        and any(marker in text for marker in question_action_markers)
    )


def looks_like_artifact_defect_report(user_input: str) -> bool:
    """Clear artifact defect reports can skip the LLM classifier."""
    text = re.sub(r"\s+", " ", (user_input or "").strip().lower())
    if not text:
        return False
    if looks_like_no_problem_feedback(text):
        return False
    visual_only_targets = (
        "배경", "색", "색상", "화면", "글자", "텍스트", "버튼", "이미지", "사진",
        "에셋", "캐릭터", "아이콘", "카드", "우주선", "남색", "파란", "빨간",
        "초록", "검정", "검은", "하얀", "흰", "회색",
    )
    visual_only_pattern = rf"({'|'.join(visual_only_targets)}).{{0,16}}만\s*(있|나오|보이|나와)"
    defect_patterns = (
        r"(.{1,24}(가|이|은|는|도)?\s*없(는데|어|어요|습니다|네|다))",
        r"(안\s*보|잘\s*안\s*보|보이\s*지\s*않|안\s*나오|나오\s*지\s*않|안\s*돼|안\s*되|작동\s*안)",
        r"(깨졌|깨져|잘렸|삐져나|넘쳐|겹쳐|가려|멈춰|흰\s*화면|빈\s*화면|에러|오류)",
        visual_only_pattern,
    )
    return any(re.search(pattern, text) for pattern in defect_patterns)


def looks_like_no_problem_feedback(user_input: str) -> bool:
    text = re.sub(r"\s+", " ", (user_input or "").strip().lower())
    if not text:
        return False
    no_problem = re.search(r"(문제|오류|에러|버그|고장)\s*(가|이|은|는|도)?\s*없", text)
    simple_positive = re.fullmatch(
        r"(괜찮(아|아요|네요)?|좋아(요)?|좋네(요)?|상관\s*없(어|어요)?"
        r"|필요\s*없(어|어요)?|없어도\s*돼(요)?)[\s.!?~…]*",
        text,
    )
    if not no_problem and not simple_positive:
        return False
    if simple_positive and not no_problem:
        return True
    trailing = text[no_problem.end():]
    if re.search(
            r"(근데|그런데|하지만|다만|인데).{0,24}"
            r"(안\s*보|안\s*나오|안\s*돼|안\s*되|없(는데|어|어요|네)|깨졌|깨져|오류|에러)",
            trailing):
        return False
    return True


def looks_like_stop_or_delay_request(user_input: str) -> bool:
    text = re.sub(r"\s+", " ", (user_input or "").strip().lower())
    if not text:
        return False
    return bool(re.search(
        r"(아직|지금은|일단|잠깐|잠시).{0,12}(하지\s*마|하지\s*말|기다려|멈춰|보류|중단)"
        r"|^(하지\s*마|하지\s*말아|멈춰|중단|보류|기다려|잠깐만|잠시만|stop|wait|hold)[\s.!?~…]*$",
        text,
    ))


def looks_like_continue_request(user_input: str) -> bool:
    text = re.sub(r"\s+", " ", (user_input or "").strip().lower())
    if not text:
        return False
    urgency = r"(빨리|어서|얼른|바로|지금)"
    action = r"(만들|수정|고치|바꾸|추가|적용|구현|생성|그리|그려|꾸미|시작|해\s*줘|해봐)"
    return bool(re.search(rf"{urgency}.{{0,16}}{action}|{action}.{{0,16}}{urgency}", text))


def looks_like_short_confirmation(user_input: str) -> bool:
    text = re.sub(r"[\s.!?~^…;ㅋㅎ]+", "", (user_input or "").strip().lower())
    return bool(re.fullmatch(
        r"(응|웅|ㅇㅇ+|어|엉|네|넵|넹|예|그래|그랭|좋아|좋습니다|좋아요|오케이|오키|ㅇㅋ|ok|okay|ㄱㄱ+|고+)",
        text,
    ))


def assistant_suggested_action(text: str) -> bool:
    text = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not text:
        return False
    action_markers = (
        "넘어갈까", "넘어가 볼까", "넘어가볼까", "진행할까", "계속할까", "시작할까",
        "만들어 볼까", "만들어볼까", "수정할까", "고쳐볼까", "바꿔볼까", "추가할까",
        "해볼까", "해 줄까", "해줄까", "준비됐어", "준비되었어",
    )
    promised_action = re.search(
        r"(만들|수정|고치|바꾸|추가|적용|완성|구현|생성|그리|그려|꾸미).{0,24}"
        r"(줄게|해볼게|할게|하겠습니다|하자)",
        text,
    )
    return any(marker in text for marker in action_markers) or bool(promised_action)


def first_response_text(response) -> str:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "") or ""
    return ""


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
    return json.loads(text[start:end + 1])


def _json_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)
