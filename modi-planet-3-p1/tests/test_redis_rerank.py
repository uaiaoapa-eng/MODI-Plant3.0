"""온프렘 Redis 백엔드 centroid+alias 재랭킹 로직 단위 검증 (#38).

Redis Stack 없이(순수 로직) search_lib._redis_search 가 KNN 후보를 개념 centroid +
alias 보너스로 재랭킹해, 순수 KNN top1(잘못된 개념)을 올바른 개념으로 뒤집는지 확인.
vector_redis / embed_bge 를 sys.modules 에 주입해 외부 의존 없이 돌린다.
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import search_lib as S  # noqa: E402

DIM = 4
# 질문 임베딩: 개념 A centroid 와 정렬(csim=1), 개념 B 와 직교(csim=0).
Q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
CENT = {"concept_a": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        "concept_b": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)}
# 후보: 오답(B) 청크가 원시 KNN 유사도는 더 높음(0.9 > 0.8) → centroid 없으면 B 가 top1.
CANDIDATES = [
    {"chunk_id": "b1", "title": "제목b", "content": "xyz", "coding_type": "any",
     "concept_key": "concept_b", "source": "base", "vec": 0.9},
    {"chunk_id": "a1", "title": "제목a", "content": "xyz", "coding_type": "any",
     "concept_key": "concept_a", "source": "base", "vec": 0.8},
]


def _fake_modules():
    vr = types.ModuleType("vector_redis")

    def load_centroids():
        keys = list(CENT)
        mat = np.vstack([CENT[k] for k in keys]).astype(np.float32)
        return keys, mat

    def search(qvec, *, coding_type=None, user_id=None, difficulty=None,
               domain=None, intent=None, top=8):
        return [dict(c) for c in CANDIDATES][:top]  # 얕은 복사(원본 보존)

    vr.load_centroids = load_centroids
    vr.search = search

    eb = types.ModuleType("embed_bge")
    eb.embed_one = lambda text: Q
    return vr, eb


@pytest.fixture
def redis_backend(monkeypatch):
    vr, eb = _fake_modules()
    monkeypatch.setitem(sys.modules, "vector_redis", vr)
    monkeypatch.setitem(sys.modules, "embed_bge", eb)
    monkeypatch.setattr(S, "_redis_cent_keys", None, raising=False)  # 캐시 초기화
    monkeypatch.setattr(S, "_redis_cent_mat", None, raising=False)
    yield S


def test_centroid_rerank_flips_knn_top1(redis_backend):
    """순수 KNN 이면 B(0.9)가 top1이지만, centroid 재랭킹으로 A 가 top1이 돼야 한다."""
    res = S._redis_search("빙글빙글 손잡이", coding_type=None, top=2, user_id=None)
    assert res is not None and res["ok"]
    assert res["vector"]["backend"] == "redis"
    # primary_concept = 전 개념 argmax(csim+alias) = concept_a
    assert res["primary_concept"]["key"] == "concept_a"
    # 재랭킹 후 top 결과 개념도 concept_a (W_CONCEPT 보정이 vec 0.9 vs 0.8 역전)
    assert res["results"][0]["concept_key"] == "concept_a"
    assert res["results"][0]["con"] > 0  # 개념 centroid 신호 반영


def test_score_matches_local_combined_formula(redis_backend):
    """후보 점수가 로컬과 동형: W_VEC*vec + W_LEX*lex + W_CONCEPT*(csim+W_CLEX*alias)."""
    res = S._redis_search("질문", coding_type=None, top=2, user_id=None)
    by_key = {r["concept_key"]: r for r in res["results"]}
    a = by_key["concept_a"]
    # concept_a: csim=1, alias_bonus=0(시드에 없는 key), lex=0(질문 토큰 미포함)
    expected = S.W_VEC * 0.8 + S.W_LEX * 0.0 + S.W_CONCEPT * (1.0 + S.W_CLEX * 0.0)
    assert a["score"] == pytest.approx(round(expected, 3), abs=1e-3)


def test_fallback_to_none_on_backend_error(monkeypatch):
    """Redis 임포트/검색 실패 시 None 반환 → search() 가 로컬로 폴백."""
    broken = types.ModuleType("vector_redis")

    def boom(*a, **k):
        raise RuntimeError("redis down")

    broken.search = boom
    broken.load_centroids = boom
    eb = types.ModuleType("embed_bge")
    eb.embed_one = lambda text: Q
    monkeypatch.setitem(sys.modules, "vector_redis", broken)
    monkeypatch.setitem(sys.modules, "embed_bge", eb)
    monkeypatch.setattr(S, "_redis_cent_keys", None, raising=False)
    monkeypatch.setattr(S, "_redis_cent_mat", None, raising=False)
    assert S._redis_search("q", None, 2, None) is None
