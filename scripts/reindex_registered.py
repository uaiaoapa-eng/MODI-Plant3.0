"""복구 전용 — MySQL knowledge_chunks(source=registered) → Redis HNSW 재색인.

backfill_onprem.py 는 콜드스타트 시더라 Redis 인덱스를 리셋(reset_index)한 뒤 base 와
*파일 기반* registered(data/registered.jsonl/.npy)만 재색인한다. 라이브 운영처럼 registered
가 파일이 아니라 MySQL+Redis 에만 쌓인 경우, backfill_onprem 재실행은 그 registered 벡터를
검색 인덱스에서 떨어뜨린다(원천 MySQL 은 보존됨). 이 스크립트는 원천(MySQL, embedding JSON)
을 읽어 Redis 로 다시 색인해 검색 가능 상태를 정확히 복원한다.

- 원천(MySQL)은 읽기만 — 수정하지 않는다.
- 멱등: 같은 chunk_id 는 HSET 으로 덮어써 재실행해도 중복이 생기지 않는다.
- backfill_onprem/backfill_writeback 의 로직은 건드리지 않는다(#106 Non-goals).

실행(rag-search 컨테이너 안 — DATABASE_URL·VECTOR_REDIS_URL 이 설정된 곳):
    PYTHONPATH=scripts python scripts/reindex_registered.py --dry-run
    PYTHONPATH=scripts python scripts/reindex_registered.py
"""
from __future__ import annotations

import argparse
import json

import numpy as np

# registered 원천을 읽을 컬럼 + 소유자 스코핑용 sessions.user_id(JOIN).
_SELECT = (
    "SELECT c.chunk_id, c.session_id, c.coding_type, c.concept_key, c.intent, "
    "       c.domain, c.difficulty, c.title, c.content, c.payload, c.embedding, "
    "       s.user_id "
    "FROM knowledge_chunks c JOIN sessions s ON c.session_id = s.session_id "
    "WHERE c.source = 'registered'"
)


def _embedding_list(raw) -> list | None:
    """MySQL JSON 컬럼(pymysql 은 str 로 반환) → float 리스트. 비었으면 None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else None
    return list(raw) if raw else None


def main() -> int:
    ap = argparse.ArgumentParser(description="MySQL registered → Redis 재색인(복구)")
    ap.add_argument("--dry-run", action="store_true",
                    help="색인 없이 대상 건수·샘플만 출력")
    args = ap.parse_args()

    import store_mysql as M
    import vector_redis as V

    with M.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT)
            rows = cur.fetchall()

    print(f"registered 청크 {len(rows)}건 발견")

    if args.dry_run:
        for r in rows[:10]:
            emb = _embedding_list(r["embedding"])
            print(f"[dry] chunk_id={r['chunk_id']} user={r['user_id']!r} "
                  f"emb_len={len(emb) if emb else 0} title={str(r['title'])[:30]!r}")
        print(f"... 총 {len(rows)}건 (dry-run — 색인하지 않음)")
        return 0

    V.ensure_index()
    reindexed = skipped = 0
    for r in rows:
        emb = _embedding_list(r["embedding"])
        if not emb or len(emb) != V.DIM:
            skipped += 1
            continue
        V.upsert(
            f"reg:{r['chunk_id']}", np.asarray(emb, dtype=np.float32),
            title=r["title"] or "", content=r["content"] or "",
            coding_type=r["coding_type"] or "any", source="registered",
            concept_key=r["concept_key"] or "", user_id=r["user_id"] or "",
            domain=r["domain"] or "", difficulty=r["difficulty"] or "",
            intent=r["intent"] or "", payload=r["payload"],
            session_id=r["session_id"] or "",
        )
        reindexed += 1

    print(f"재색인 완료: {reindexed}건 색인, {skipped}건 스킵(빈 embedding/차원 불일치). "
          f"Redis num_docs={V.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
