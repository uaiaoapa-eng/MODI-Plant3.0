"""vector_redis 통합 스모크 — Redis Stack(HNSW) 인덱스·태그필터·KNN.

Redis Stack 가 로컬/CI에서 접속 가능할 때만 실행(아니면 스킵).
온프렘 검증용: docker compose -f docker-compose.rag-onprem.yml up -d 후 실행.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import redis as _redis

    import vector_redis as V
except Exception as e:
    pytest.skip(f"redis 클라이언트 없음: {e}", allow_module_level=True)

# RediSearch(모듈) 가 있는 Redis Stack 인지 확인 — 일반 redis면 스킵.
try:
    _c = V.client()
    _c.ping()
    _c.ft(V.INDEX).info()  # 없으면 ResponseError(unknown index) → Stack은 맞음
    _STACK = True
except _redis.ResponseError as e:
    _STACK = "unknown index" in str(e).lower() or "no such index" in str(e).lower()
except Exception:
    _STACK = False

if not _STACK:
    pytest.skip("Redis Stack 미가용 — vector_redis 스모크 스킵", allow_module_level=True)


@pytest.fixture
def idx():
    try:
        V.client().ft(V.INDEX).dropindex(delete_documents=True)
    except Exception:
        pass
    V.ensure_index()
    yield V
    try:
        V.client().ft(V.INDEX).dropindex(delete_documents=True)
    except Exception:
        pass


def _unit(i: int):
    v = [0.0] * V.DIM
    v[i % V.DIM] = 1.0
    return v


def test_upsert_and_knn(idx):
    V.upsert("base:1", _unit(0), title="반복문 색바꾸기", coding_type="blockly", source="base")
    V.upsert("base:2", _unit(1), title="다이얼 센서", coding_type="hardware", source="base")
    hits = V.search(_unit(0), top=2)
    assert hits and hits[0]["title"] == "반복문 색바꾸기"
    assert hits[0]["vec"] >= 0.99  # 코사인≈1


def test_coding_type_filter(idx):
    V.upsert("base:1", _unit(0), title="a", coding_type="blockly", source="base")
    V.upsert("base:2", _unit(0), title="b", coding_type="react", source="base")
    hits = V.search(_unit(0), coding_type="react", top=5)
    assert all(h["coding_type"] == "react" for h in hits)


def test_user_scope_filter(idx):
    V.upsert("base:1", _unit(0), title="공용", coding_type="any", source="base")
    V.upsert("reg:1", _unit(0), title="내등록", source="registered", user_id="uA")
    mine = V.search(_unit(0), user_id="uA", top=5)
    other = V.search(_unit(0), user_id="uB", top=5)
    assert any(h["source"] == "registered" for h in mine)
    assert all(h["source"] == "base" for h in other)


def test_centroid_roundtrip():
    """개념 centroid upsert → load → clear 멱등 (#38 온프렘 재랭킹 자산)."""
    V.clear_centroids()
    V.upsert_centroid("concept_a", _unit(0))
    V.upsert_centroid("concept_b", _unit(1))
    keys, mat = V.load_centroids()
    assert set(keys) == {"concept_a", "concept_b"}
    assert mat.shape == (2, V.DIM)
    # 로드된 centroid 는 정규화(단위벡터) → 자기 자신과 코사인≈1
    idx_a = keys.index("concept_a")
    assert float(mat[idx_a] @ mat[idx_a]) == pytest.approx(1.0, abs=1e-3)
    removed = V.clear_centroids()
    assert removed == 2
    assert V.load_centroids()[0] == []  # 삭제 후 개념 키 없음
