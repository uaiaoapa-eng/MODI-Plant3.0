from __future__ import annotations

import re
from agent.models import Phase


# 키워드 기반 라우터 — API 호출 없이 의도 분류
# Phase 전환 패턴 — 구현↔검증, 설계 복귀 등. (설계→구현 '빌드 시작'은 키워드로 판단하지 않는다.
# 표현 변형·오작동에 취약하므로, 대화를 읽는 설계 에이전트가 transition_phase 도구로 판단 — classify 참고)
PHASE_CHANGE_PATTERNS = [
    r"검증\s*하자", r"확인\s*해보자", r"돌아가자", r"다시\s*설계",
    r"phase", r"단계.*넘어", r"단계.*바꿔",
]

IMPLEMENT_PATTERNS = [
    r"만들어\s*줘", r"구현\s*해\s*줘", r"코드\s*짜", r"페이지.*만들",
    r"기능.*추가", r"컴포넌트.*만들", r"API.*만들", r"화면.*만들",
]

MODIFY_PATTERNS = [
    r"수정\s*해", r"바꿔\s*줘", r"변경\s*해", r"고쳐\s*줘",
    r"다시\s*만들", r"이거\s*말고",
]

QUESTION_PATTERNS = [
    r"\?$", r"뭐야", r"왜\s", r"어떻게", r"무슨", r"설명\s*해",
    r"알려\s*줘", r"차이가\s*뭐",
]

# 잡담은 "발화 전체가 잡담"일 때만(앵커 전체 일치, 꼬리 부호·ㅋㅎ 허용) — 요청이 섞이면
# ("안녕 계산기 만들어줘") 위의 IMPLEMENT/MODIFY 패턴이 먼저 잡거나 여기 안 걸려 통과한다.
CHAT_PATTERNS = [
    # 수용/리액션 — 짧은 반응이 구현 요청으로 fall-through 되는 것 방지
    r"^(고마워요?|고맙습니다|감사합니다|감사해요|감사|땡큐|thanks?|thank you|좋아요?|좋네요?"
    r"|괜찮네요?|괜찮아요?|오케이|오키|ㅇㅋ|ok|okay|굿|굿굿|나이스|nice|그래요?|고+|ㄱㄱ+"
    r"|알겠어요?|알겠습니다|알았어요?|아하|그렇구나|그러네요?|네|넵|넹|예|응|웅|음+|흠+|오+"
    r"|와+|우와|대박|짱|ㅇㅇ|ㅋㅋ+|ㅎㅎ+|ㅠㅠ+|ㅜㅜ+|\^\^+)[\s.!?~^…;ㅋㅎ]*$",
    # 인사말(만남/작별) — 어린 학습자 표기 변형(안뇽·하이루·ㅎㅇ·방가·빠이 등)까지 넓게.
    # 실사고: "안녕"이 여기 없어서 implement_request로 fall-through → 구현 phase 전환+
    # 생성 계약(generate_code 강제)까지 물었음.
    r"^((?:안녕){1,3}(?:하세요|하세용|하세여|하세염|하셔요|하십니까|하셈)?|안뇽|안늉"
    r"|(?:하이){1,3}(?:루|용|염)?|하잉|하잇|하위|(?:ㅎㅇ){1,3}|헬로+우?|할로+|hello+|hi+"
    r"|hey+|헤이|yo|얍|야호|야+|(?:방가){1,2}|반가워요?|반갑습니다|반갑네요"
    r"|좋은 ?(?:아침|오후|저녁|하루)(?:이에요|입니다|예요)?|굿모닝|굿애프터눈|굿이브닝"
    r"|모닝|good ?(?:morning|afternoon|evening|night)|what'?s ?up|wass?up|sup"
    r"|굿밤|굿나잇|잘 ?자요?|오랜만(?:이야|이네|이에요|입니다)?|처음 ?뵙겠습니다"
    r"|잘 ?가요?|빠이+|빠빠이|바이+|bye+|굿바이|씨유|see ?(?:you|ya)|cya"
    r"|안녕히 ?(?:가세요|계세요)|또 ?봐요?|다음에 ?봐요?|내일 ?봐요?|낼 ?봐요?)"
    r"[\s.!?~^…;ㅋㅎ]*$",
]


class Router:
    def __init__(self, client=None):
        self.client = client  # 더 이상 API 호출 안 함

    def classify(self, user_input: str, current_phase: Phase) -> str:
        known = self.classify_known(user_input, current_phase)
        if known:
            return known
        return self._fallback(current_phase)

    def classify_known(self, user_input: str, current_phase: Phase) -> str | None:
        """명확한 규칙 매칭만 반환. 애매한 구현 후속 발화는 호출자가 LLM 라우터로 보낸다."""
        text = user_input.strip()

        # 설계 단계: 설계→구현 전환(빌드 시작)은 키워드로 판단하지 않는다.
        # 대화를 읽는 설계 에이전트가 "유저가 이제 만들자는 건지"를 판단해 transition_phase 도구로
        # 전환한다(키워드 매칭은 "만들자/ㄱㄱ/그래 가자" 같은 표현 변형·오작동에 취약).
        # 그래서 설계 중엔 모두 일반 설계 대화로 넘긴다.
        if current_phase == Phase.DESIGN:
            return "design_explain"

        # 구현/검증 단계
        if self._matches(text, PHASE_CHANGE_PATTERNS):
            return "phase_change"

        if current_phase == Phase.IMPLEMENT and self._matches(text, MODIFY_PATTERNS):
            return "modify_request"

        if self._matches(text, IMPLEMENT_PATTERNS):
            return "implement_request"

        if self._matches(text, QUESTION_PATTERNS):
            return "question"

        if self._matches(text.lower(), CHAT_PATTERNS):
            return "chat"

        return None

    def _matches(self, text: str, patterns: list) -> bool:
        return any(re.search(p, text) for p in patterns)

    def _fallback(self, phase: Phase) -> str:
        if phase == Phase.DESIGN:
            return "design_explain"
        elif phase == Phase.IMPLEMENT:
            # 구현 phase에서 fallback을 구현 요청으로 두면 "고마워", "음...", "괜찮네" 같은
            # 후속 대화가 코드 수정 계약을 물고 들어간다. 애매하면 안전하게 대화/질문으로 둔다.
            return "question"
        return "question"
