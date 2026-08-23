"""온프렘 쿼터 상한 두 개(턴/토큰)의 정합성 회귀 테스트 — LLM/네트워크 미사용.

배경 (2026-08-21):
  서버 쿼터 게이트는 턴 상한과 토큰 상한 중 **하나라도** 소진되면 차단한다
  (server.py `/chat`). 그래서 두 값을 따로 정하면 조용히 어긋난다.

  실제로 그럴 뻔했다: 2시간 수업 기준으로 `QUOTA_DAILY_MAX_TURNS=70` 을 정했는데,
  토큰 상한이 기본 2,000,000 이라 **생성 위주 사용자는 15.6턴에서 먼저 막혔다.**
  70 은 되묻기 위주 사용자에게만 유효한 장식이 될 뻔했다. 두 값을 5,000,000 / 70 으로
  맞춰 정상 수업(혼합)이 70턴을 실제로 쓸 수 있게 했다.

  이 테스트는 **설정 파일의 두 값이 서로 정합한지**를 실측 토큰 단가로 검증한다.
  나중에 한쪽만 바꾸면 여기서 잡힌다.
"""
import pathlib
import re

import pytest

# 실측 가중토큰/턴 (2026-08-21 프로덕션, Langfuse + /agent/quota)
#   가중토큰 = input + output*5 + cache_read*0.1 + cache_creation*1.25
#   (이 가중치는 Haiku 4.5 단가 비율과 일치 → 가중토큰/1e6 = USD)
GEN_TURN_WEIGHTED = 128_600   # 풀 생성 턴 중위값(구현+분석+학습노트, LLM 4~5회)
ASK_TURN_WEIGHTED = 12_814    # 되묻기 턴(design 모드 질의응답)
MIX_TURN_WEIGHTED = 0.5 * GEN_TURN_WEIGHTED + 0.5 * ASK_TURN_WEIGHTED

ONPREM_ENV = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "onprem.env"


def _env_int(name: str) -> int:
    """onprem.env 에서 KEY=정수 를 읽는다(주석 줄 무시)."""
    text = ONPREM_ENV.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}=(\d+)\s*$", text, re.MULTILINE)
    assert m, f"{name} 이 {ONPREM_ENV.name} 에 없다"
    return int(m.group(1))


@pytest.fixture(scope="module")
def caps():
    return _env_int("QUOTA_DAILY_MAX_TURNS"), _env_int("QUOTA_DAILY_WEIGHTED_TOKENS")


def test_quota_enabled(caps):
    """쿼터 게이트 자체가 켜져 있어야 상한이 의미를 가진다."""
    text = ONPREM_ENV.read_text(encoding="utf-8")
    assert re.search(r"^QUOTA_ENABLED=true\s*$", text, re.MULTILINE), \
        "QUOTA_ENABLED 가 true 가 아니면 두 상한 모두 집행되지 않는다"


def test_turn_cap_is_set(caps):
    """0(무제한)이면 한 사용자가 공유 자원을 독점할 수 있다."""
    turns, _ = caps
    assert turns > 0, "턴 상한 0 = 무제한 — 폭주/자동화 방어가 사라진다"


def test_mixed_usage_can_actually_reach_turn_cap(caps):
    """★ 핵심: 정상 수업(생성/되묻기 혼합)이 턴 상한까지 도달할 수 있어야 한다.

    토큰 상한이 낮으면 턴 상한은 장식이 된다. 실제로 2M/70 조합이 그랬다
    (혼합 사용자가 28.3턴에서 토큰 상한에 먼저 막힘).
    """
    turns, tokens = caps
    reachable = tokens / MIX_TURN_WEIGHTED
    assert reachable >= turns, (
        f"혼합 사용자가 {reachable:.1f}턴에서 토큰 상한에 막혀 턴 상한 {turns}에 도달 못 한다. "
        f"토큰 상한을 최소 {turns * MIX_TURN_WEIGHTED:,.0f} 로 올려야 한다"
    )


def test_generation_heavy_still_bounded_by_tokens(caps):
    """반대 방향: 생성만 반복하는 비정상 패턴은 턴 상한 전에 토큰으로 끊겨야 한다.

    이게 없으면(토큰 상한이 과도하게 높으면) 폭주 방어가 턴 상한 하나로 줄어든다.
    """
    turns, tokens = caps
    gen_reachable = tokens / GEN_TURN_WEIGHTED
    assert gen_reachable < turns, (
        f"생성만 해도 {gen_reachable:.1f}턴까지 가능해 토큰 상한이 사실상 무력하다 "
        f"(턴 상한 {turns}). 토큰 상한을 낮추는 편이 안전하다"
    )


def test_cost_ceiling_per_user_is_sane(caps):
    """1인당 하루 최대 비용(API 환산)이 상식 범위인지 — 가중토큰/1e6 = USD."""
    _, tokens = caps
    usd = tokens / 1_000_000
    assert usd <= 10.0, f"1인 하루 상한이 ${usd:.2f} — 40명이면 ${usd * 40:,.0f}. 과도하다"
    assert usd >= 1.0, f"1인 하루 상한이 ${usd:.2f} — 정상 수업을 막을 만큼 낮다"


def test_ask_only_usage_is_not_the_binding_constraint(caps):
    """되묻기 위주 사용자는 토큰이 아니라 턴 상한에 걸려야 한다(설계 의도 확인)."""
    turns, tokens = caps
    assert tokens / ASK_TURN_WEIGHTED > turns, \
        "되묻기만 해도 토큰 상한에 먼저 걸린다 — 상한이 지나치게 낮다"
