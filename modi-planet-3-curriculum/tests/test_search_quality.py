"""검색 매칭 품질 회귀 가드 — centroid blend 로 개념 정확도 유지.

gold 8건에 대한 primary_concept 정확도가 기준(6/8) 아래로 떨어지면 실패.
벡터(BGE-m3) 자산·의존이 없으면 스킵(부분일치만으론 이 기준 미보장).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import search_lib as S
    import eval_search
except Exception as e:
    pytest.skip(f"import 불가: {e}", allow_module_level=True)

if not S.vector_enabled() or S._embed_query("확인") is None:
    pytest.skip("벡터(BGE-m3) 미가용 — 품질 eval 스킵", allow_module_level=True)


def test_primary_concept_accuracy_baseline():
    # 시드 alias 정제 + centroid에 alias 보너스 블렌드로 gold 8/8 달성(#27 P1).
    # 회귀 가드 floor는 7(임베딩/데이터 미세 변동 허용, 큰 회귀는 차단).
    res = eval_search.run()
    assert res["primary_concept_hits"] >= 7, (
        f"primary_concept {res['primary_concept_hits']}/{res['n']} < 7 — 매칭 품질 회귀"
    )


def test_centroid_boost_present_in_results():
    r = S.search("빙글빙글 돌리는 손잡이로 숫자 바꾸기", top=3)
    assert r["primary_concept"] is not None
    assert any(x["con"] > 0 for x in r["results"])  # centroid 신호가 점수에 반영
