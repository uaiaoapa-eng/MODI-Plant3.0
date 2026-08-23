"""search_lib 하이브리드 검색 + %게이팅 단위 테스트.

torch 유무와 무관하게 돌도록:
  - 어휘/토큰·결정 게이팅은 순수 함수로 결정적 검증
  - 벡터 경로는 _embed_query 를 몽키패치해 폴백/활성 두 경우를 모두 강제
데이터 자산(chunk_emb.npy / chunk_meta.json)이 없으면 모듈 스킵.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import search_lib as S
except Exception as e:  # numpy 미설치 등
    pytest.skip(f"search_lib import 불가: {e}", allow_module_level=True)

if not os.path.exists(S.META_PATH):
    pytest.skip("chunk_meta.json 자산 없음", allow_module_level=True)


# ── 순수 함수 ────────────────────────────────────────────────
def test_tokens_drops_stopwords_and_short():
    toks = S._tokens("자동차가 벽을 보고 멈추게 하고 싶어요")
    assert "자동차가" in toks
    assert "하고" not in toks  # 불용어 제거
    assert "싶어요" not in toks


def test_decision_gating_boundaries():
    assert S._decision(S.TAU_REUSE) == "reuse"
    assert S._decision(S.TAU_REUSE - 0.001) == "review"
    assert S._decision(S.TAU_NEAR) == "review"
    assert S._decision(S.TAU_NEAR - 0.001) == "register"
    assert S._decision(0.0) == "register"


def test_lexical_scores_range_and_overlap():
    _, meta = S._load_assets()
    sc = S._lexical_scores("두더지가 랜덤한 곳에서 튀어나오게", meta)
    assert sc.shape == (len(meta),)
    assert sc.min() >= 0.0 and sc.max() <= 1.0


def test_lexical_scores_empty_query_all_zero():
    _, meta = S._load_assets()
    assert float(S._lexical_scores("", meta).sum()) == 0.0


# ── 폴백(벡터 off) 경로: 결정적 ──────────────────────────────
@pytest.fixture
def no_vector(monkeypatch):
    """임베딩 자산을 감춰 부분일치 단독 폴백을 강제."""
    monkeypatch.setattr(S, "_emb", None)
    monkeypatch.setattr(S, "_meta", None)
    monkeypatch.setattr(S, "_load_assets", lambda: (None, _META_CACHE))
    monkeypatch.setattr(S, "_embed_query", lambda t: None)
    monkeypatch.setattr(S, "_combo_meta", None)  # 통합 캐시 무효화(테스트 간 누수 방지)
    monkeypatch.setattr(S, "_combo_ver", -1)


_, _META_CACHE = S._load_assets()  # 원본 메타 확보(폴백 픽스처에서 재사용)


def test_search_empty_query_returns_not_ok(no_vector):
    r = S.search("   ")
    assert r["ok"] is False


def test_search_fallback_reports_vector_disabled(no_vector):
    r = S.search("두더지가 랜덤한 곳에서 튀어나오게", top=5)
    assert r["ok"] is True
    assert r["vector"]["enabled"] is False
    assert all(x["vec"] == 0.0 for x in r["results"])  # 벡터 미사용
    assert r["decision"] in ("reuse", "review", "register")


def test_search_results_sorted_desc(no_vector):
    r = S.search("숫자를 천 단위 콤마로 예쁘게", top=8)
    scores = [x["score"] for x in r["results"]]
    assert scores == sorted(scores, reverse=True)


def test_search_coding_type_filter(no_vector):
    r = S.search("좋아요 버튼을 누르면 하트 색이 바뀌게", coding_type="react", top=10)
    assert all(x["coding_type"] == "react" for x in r["results"])


def test_coverage_percentages_sum_100(no_vector):
    cov = S.coverage(["카드 100개를 똑같이 만들고 싶어요", "완전 처음보는 xyz 질문"])
    total = cov["percent"]["reuse"] + cov["percent"]["review"] + cov["percent"]["register"]
    assert abs(total - 100.0) < 0.01
    assert cov["reuse_rate"] + cov["register_rate"] == pytest.approx(100.0, abs=0.01)
    assert cov["total"] == 2


# ── 활성(벡터 on) 경로: 임베딩 몽키패치로 결정적 ─────────────
@pytest.fixture
def fake_vector(monkeypatch):
    """첫 청크와 동일 방향 질문벡터를 반환 → 그 청크가 top1(코사인≈1)."""
    emb, meta = S._load_assets()
    if emb is None:
        pytest.skip("chunk_emb.npy 자산 없음")
    monkeypatch.setattr(S, "_embed_query", lambda t: emb[0:1].copy())
    monkeypatch.setattr(S, "_combo_meta", None)  # 통합 캐시 무효화(테스트 간 누수 방지)
    monkeypatch.setattr(S, "_combo_ver", -1)
    return emb, meta


def test_search_vector_on_top1_is_high(fake_vector):
    # 어휘 토큰이 없는 질문(불용어만) → 결합점수=벡터만 → 자기 자신이 top1
    r = S.search("이거 하고", top=3)
    assert r["vector"]["enabled"] is True
    assert r["results"][0]["vec"] >= 0.99  # 자기 자신과 코사인≈1
    assert r["top1_score"] >= S.W_VEC * 0.99


def test_load_assets_normalized_no_nan(fake_vector):
    emb, _ = fake_vector
    norms = np.linalg.norm(emb, axis=1)
    assert not np.isnan(emb).any()
    # 0벡터 외에는 단위벡터
    nz = norms > 0
    assert np.allclose(norms[nz], 1.0, atol=1e-4)


# ── intent 필터(EDU-27 재사용 게이트 kind 한정 검색) ─────────
def test_search_intent_filter_only_matching(no_vector):
    r = S.search("버튼 누르면 LED 켜지게 해줘", intent="explain_concept", top=10)
    assert r["ok"] is True
    assert all(x.get("intent") == "explain_concept" for x in r["results"])


def test_search_intent_filter_excludes_others(no_vector):
    base = S.search("버튼 누르면 LED 켜지게 해줘", top=10)
    filtered = S.search("버튼 누르면 LED 켜지게 해줘", intent="implement_request", top=10)
    assert filtered["ok"] is True
    # base 자산은 학습노트(explain_concept) 위주 — implement_request 필터 결과는
    # 전부 해당 intent 이거나(등록물 존재 시) 비어 있어야 한다.
    assert all(x.get("intent") == "implement_request" for x in filtered["results"])
    assert len(filtered["results"]) <= len(base["results"])


def test_coverage_accepts_intent_kwarg(no_vector):
    cov = S.coverage(["버튼 누르면 LED 켜지게 해줘"], intent="implement_request")
    assert cov["ok"] is True and cov["total"] == 1
