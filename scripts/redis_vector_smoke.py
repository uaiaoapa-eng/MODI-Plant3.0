"""Redis Stack 벡터 검색 스모크 테스트.

전체(Aurora+앱) 배선 전에, RediSearch HNSW 벡터 + 태그 필터(=하이브리드 검색의
핵심)가 온프렘 docker-compose 환경에서 동작하는지만 격리 검증한다.

의존성: redis>=5.0.0 (이미 설치됨). numpy/torch 불필요 — float32는 stdlib array로 팩.
BGE-m3 실임베딩은 이 단계에서 붙이지 않는다(인프라 검증이 목적).

실행:
    docker compose -f docker-compose.redis-test.yml up -d
    python scripts/redis_vector_smoke.py
"""

from __future__ import annotations

import array
import os

import redis
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

REDIS_URL = os.getenv("VECTOR_REDIS_URL", "redis://localhost:6380/0")
INDEX = "idx:chunks"
PREFIX = "chunk:"
DIM = 8  # 실전은 1024(BGE-m3). 검증은 손으로 만든 8차원으로 순위/필터를 눈으로 확인.


def f32(vec: list[float]) -> bytes:
    """float 리스트 → RediSearch가 요구하는 float32 바이트."""
    return array.array("f", vec).tobytes()


def unit(*idx: int) -> list[float]:
    """지정 축만 1인 DIM 차원 벡터 (유사도를 예측 가능하게 만들기 위함)."""
    v = [0.0] * DIM
    for i in idx:
        v[i] = 1.0
    return v


def main() -> None:
    r = redis.from_url(REDIS_URL)
    print(f"[1] 연결: {REDIS_URL} → ping={r.ping()}")

    # 기존 인덱스 정리
    try:
        r.ft(INDEX).dropindex(delete_documents=True)
    except redis.ResponseError:
        pass

    # [2] 인덱스 생성: 벡터(HNSW/COSINE) + 태그 필터 + 제목
    schema = (
        TagField("chunk_type"),
        TagField("domain"),
        TextField("title"),
        VectorField(
            "embedding",
            "HNSW",
            {"TYPE": "FLOAT32", "DIM": DIM, "DISTANCE_METRIC": "COSINE"},
        ),
    )
    r.ft(INDEX).create_index(
        schema,
        definition=IndexDefinition(prefix=[PREFIX], index_type=IndexType.HASH),
    )
    print(f"[2] 인덱스 생성: {INDEX} (HNSW/COSINE, DIM={DIM})")

    # [3] 세분화 청크 몇 개 적재 (chunk_type/domain 메타 포함)
    docs = [
        # id,          chunk_type,      domain,     vector,        title
        ("ln_loop",    "learning_note", "blockly",  unit(0),       "반복문으로 색 바꾸기"),
        ("ln_sensor",  "learning_note", "hardware", unit(0, 1),    "다이얼 센서값 구간 매핑"),
        ("code_dial",  "code",          "hardware", unit(1),       "dial→색 선택 파이썬"),
        ("design_game","design_doc",    "blockly",  unit(2),       "색맞추기 게임 설계"),
    ]
    for cid, ctype, dom, vec, title in docs:
        r.hset(
            PREFIX + cid,
            mapping={
                "chunk_type": ctype,
                "domain": dom,
                "title": title,
                "embedding": f32(vec),
            },
        )
    print(f"[3] 적재: {len(docs)}개 청크")

    # [4-a] 순수 KNN: unit(0,1)과 가까운 순 → ln_sensor(정확일치) > ln_loop/code_dial
    q_vec = f32(unit(0, 1))
    knn = (
        Query("*=>[KNN 3 @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("title", "chunk_type", "domain", "score")
        .dialect(2)
    )
    res = r.ft(INDEX).search(knn, query_params={"vec": q_vec})
    print("\n[4-a] KNN(무필터) top3 — 유사도 오름차순(작을수록 가까움):")
    for d in res.docs:
        print(f"   score={float(d.score):.3f}  [{d.chunk_type}/{d.domain}] {d.title}")

    # [4-b] 하이브리드: domain=hardware 필터 + KNN → hardware 청크만
    hyb = (
        Query("(@domain:{hardware})=>[KNN 3 @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("title", "chunk_type", "domain", "score")
        .dialect(2)
    )
    res2 = r.ft(INDEX).search(hyb, query_params={"vec": q_vec})
    print("\n[4-b] 하이브리드(domain=hardware 필터 + KNN):")
    for d in res2.docs:
        print(f"   score={float(d.score):.3f}  [{d.chunk_type}/{d.domain}] {d.title}")
    assert all(d.domain == "hardware" for d in res2.docs), "필터가 안 먹음!"

    # [4-c] 멀티 카테고리 팬아웃: chunk_type별 top1 (포괄 카드 조립의 뼈대)
    print("\n[4-c] 카테고리별 팬아웃(포괄 카드):")
    for ctype in ("learning_note", "code", "design_doc"):
        fan = (
            Query(f"(@chunk_type:{{{ctype}}})=>[KNN 1 @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("title", "score")
            .dialect(2)
        )
        fr = r.ft(INDEX).search(fan, query_params={"vec": q_vec})
        top = fr.docs[0].title if fr.docs else "(없음)"
        print(f"   {ctype:14s} → {top}")

    print("\n✅ Redis Stack 벡터 + 태그 필터 + 팬아웃 정상 동작.")


if __name__ == "__main__":
    main()
