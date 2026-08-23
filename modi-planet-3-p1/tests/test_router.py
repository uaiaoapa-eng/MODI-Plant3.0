"""라우터 규칙 분류 — 잡담/인사는 '발화 전체'일 때만 chat, 요청이 섞이면 구현으로.

실사고: quick 세션에서 "안녕"이 CHAT_PATTERNS에 없어 fall-through로
implement_request가 되어 구현 phase 전환 + 생성 계약(generate_code 강제)까지
물고 들어갔다. 이 경계(전체 일치 잡담 vs 부분 일치 요청)를 잠근다.
"""
import pytest

from agent.models import Phase
from agent.router import Router

router = Router()


@pytest.mark.parametrize("utt", [
    # 인사말(만남) — 표기 변형 포함
    "안녕", "안녕하세요!", "안녕하세염", "안녕안녕", "안뇽", "하이", "하이루~",
    "하잇", "ㅎㅇ", "ㅎㅇㅎㅇ", "헬로", "Hello", "HI", "hey~", "야호", "방가방가",
    "반가워", "굿모닝", "모닝!", "좋은 아침이에요", "good morning", "wassup",
    "오랜만이야", "처음 뵙겠습니다",
    # 인사말(작별)
    "잘자", "잘 가", "빠이", "빠빠이", "bye", "굿바이", "씨유", "see you",
    "안녕히 가세요", "또 봐요", "내일 봐",
    # 수용/리액션 — 꼬리 ㅋㅋ/부호 허용, 이모티콘
    "고마워ㅋㅋ", "감사합니다", "넵", "ㅇㅇ", "대박", "그래", "ㄱㄱ", "오케이~",
    "ㅠㅠ", "^^",
])
def test_smalltalk_full_match_is_chat(utt):
    assert router.classify_known(utt, Phase.IMPLEMENT) == "chat"


@pytest.mark.parametrize("utt", [
    "안녕 계산기 만들어줘",
    "안녕하세요 시계 앱 만들어 줘",
])
def test_greeting_plus_request_is_implement(utt):
    # 인사가 붙어도 요청이 있으면 구현 — 잡담 패턴은 전체 일치라 여기 안 걸린다
    assert router.classify_known(utt, Phase.IMPLEMENT) == "implement_request"


def test_bare_idea_falls_through_to_none():
    # 동사 없는 아이디어("공룡 게임")는 규칙으로 단정 못 함 — None을 돌려주고
    # quick 첫 턴 호출자의 폴스루(구현)가 받는다 (의도된 동작)
    assert router.classify_known("공룡 게임", Phase.IMPLEMENT) is None


def test_design_phase_short_circuits_to_design_explain():
    # 설계 단계는 인사도 설계 에이전트가 받는다 (전환 판단은 에이전트 몫)
    assert router.classify_known("안녕", Phase.DESIGN) == "design_explain"
