"""청크 임베딩 캐시 빌드 (배포/재현용).

온톨로지 DB(build_ontology.py 산출)의 학습노트 청크를 BGE-m3로 임베딩해
data/chunk_emb.npy + chunk_meta.json 으로 저장한다. 하이브리드 검색의 벡터 자산.

  data/chunk_emb.npy   (N,1024) float32   ← search_lib 가 로드
  data/chunk_meta.json [{chunk_id,title,content,concept_key,coding_type}]  (index 정합)

실행:  python scripts/build_embeddings.py            # 없으면 빌드, 있으면 스킵
       python scripts/build_embeddings.py --force    # 항상 재빌드
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

from embed_bge import embed
from ontology_lib import connect

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMB = os.path.join(BASE, "data", "chunk_emb.npy")
META = os.path.join(BASE, "data", "chunk_meta.json")


def build() -> tuple[int, int]:
    conn = connect()
    rows = conn.execute(
        "SELECT c.chunk_id, c.title, c.content, c.concept_key, c.coding_type, c.chunk_type,"
        "       c.session_id, c.intent, c.domain, c.difficulty, c.modi_keys, c.payload,"
        "       s.user_id"
        "  FROM chunks c LEFT JOIN sessions s ON s.session_id = c.session_id"
        # 학습노트는 개념 배정된 것 + 재사용 청크(code/design_doc)는 개념 무관 전부 포함(#3)
        " WHERE c.concept_key IS NOT NULL OR c.chunk_type IN ('code','design_doc')"
    ).fetchall()
    conn.close()
    meta = [dict(r) for r in rows]
    # JSON 컬럼(modi_keys/payload)은 sqlite 에 문자열로 저장 → 객체로 복원해 chunk_meta.json
    # 에 담는다(backfill 이 MySQL/Redis 로 재직렬화, 검색 결과가 객체로 클라이언트 전달, #44).
    for r in meta:
        for k in ("modi_keys", "payload"):
            if isinstance(r.get(k), str) and r[k]:
                try:
                    r[k] = json.loads(r[k])
                except json.JSONDecodeError:
                    pass
    if not meta:
        raise SystemExit("청크가 없습니다. 먼저 build_ontology.py 를 실행하세요.")
    texts = [f"{r['title']}. {(r['content'] or '')[:120]}" for r in meta]
    print(f"청크 {len(texts)}개 임베딩 중… (BGE-m3, 최초 1회)")
    vecs = embed(texts, batch=32).astype(np.float32)
    os.makedirs(os.path.dirname(EMB), exist_ok=True)
    np.save(EMB, vecs)
    with open(META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print(f"저장 완료 → {EMB} {vecs.shape} · {META}")
    return len(meta), vecs.shape[1]


def main() -> None:
    force = "--force" in sys.argv
    if not force and os.path.exists(EMB) and os.path.exists(META):
        print(f"임베딩 캐시 존재 → 스킵 ({EMB}). 재빌드는 --force")
        return
    build()


if __name__ == "__main__":
    main()
