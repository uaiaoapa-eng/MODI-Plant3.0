"""#67 T4: TAU precision 스윕 로직 검증.

실제 검색 백엔드 없이 결정적 search_fn 을 주입해, 스윕이 precision/recall/coverage 를
올바로 계산하고 콜드(재사용 대상 없음) 질문의 false positive 를 정밀도에 반영하는지 본다.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from eval_search import sweep_tau  # noqa: E402


# (query, expected_concept | None). None = 콜드.
GOLD = [("q_hi", "cA"), ("q_mid", "cB"), ("q_cold_high", None)]
# 결정적 점수/상위 개념. q_cold_high 는 점수는 높지만 재사용 대상이 없다(콜드).
SCORES = {
    "q_hi": (0.80, "cA"),          # 정답 매치
    "q_mid": (0.55, "cB"),         # 정답 매치(점수 낮음)
    "q_cold_high": (0.75, "cZ"),   # 콜드인데 점수 높음 → 넘기면 false positive
}


def _fake_search(q, top=1):
    score, concept = SCORES[q]
    return {"top1_score": score, "results": [{"concept_key": concept}]}


def test_precision_penalizes_cold_false_positive():
    res = sweep_tau(taus=[0.62, 0.78], gold=GOLD, search_fn=_fake_search)
    by_tau = {r["tau"]: r for r in res["rows"]}

    # TAU=0.62: q_hi(0.80)+q_cold_high(0.75) 재사용 → TP=1, FP=1(콜드) → precision 0.5
    low = by_tau[0.62]
    assert low["reused"] == 2 and low["tp"] == 1 and low["fp"] == 1
    assert low["precision"] == 0.5

    # TAU=0.78: q_hi 만 재사용 → precision 1.0 (콜드 걸러짐)
    hi = by_tau[0.78]
    assert hi["reused"] == 1 and hi["fp"] == 0
    assert hi["precision"] == 1.0


def test_recall_is_over_reusable_only():
    res = sweep_tau(taus=[0.50], gold=GOLD, search_fn=_fake_search)
    row = res["rows"][0]
    # 재사용 가능(비콜드)=2 (q_hi, q_mid). TAU 0.50 이면 둘 다 넘음 → recall 1.0
    assert res["reusable"] == 2
    assert row["tp"] == 2 and row["recall"] == 1.0


def test_empty_reuse_has_none_precision():
    res = sweep_tau(taus=[0.99], gold=GOLD, search_fn=_fake_search)
    row = res["rows"][0]
    assert row["reused"] == 0 and row["precision"] is None
