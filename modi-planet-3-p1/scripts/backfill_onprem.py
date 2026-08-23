"""온프렘 백필 — 이미 검증된 로컬 자산을 MySQL + Redis 로 적재.

재현 파이프라인을 재사용(재구현 아님):
  build_ontology.py  → data/ontology.db (sessions·chunks·ontology 노드/엣지)
  build_embeddings.py→ data/chunk_emb.npy + chunk_meta.json (index 정합)
그 산출물을 읽어:
  1) MySQL: sessions · knowledge_chunks · ontology_nodes/edges 적재(원천)
  2) Redis Stack: base 청크 임베딩만 HNSW 인덱스에 갱신(검색)
     — registered 문서는 손대지 않는다(#114: 인덱스 전체 reset 이 라이브 registered 를
       검색에서 떨어뜨리던 문제 제거). 필요 시 scripts/reindex_registered.py 로 별도 복원.

실행(온프렘, DATABASE_URL·VECTOR_REDIS_URL 설정 후):
    PYTHONPATH=scripts python scripts/backfill_onprem.py
"""
from __future__ import annotations

import json
import os
import sqlite3

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "ontology.db")
EMB = os.path.join(BASE, "data", "chunk_emb.npy")
META = os.path.join(BASE, "data", "chunk_meta.json")


def _ensure_local_assets() -> None:
    """자산이 없으면(신규 서버·빈 볼륨) 검증된 스크립트로 자동 빌드 → 완전 자동 배포."""
    if not os.path.exists(DB):
        print("data/ontology.db 없음 → build_ontology 자동 실행", flush=True)
        import build_ontology
        build_ontology.main()
    if not (os.path.exists(EMB) and os.path.exists(META)):
        print("임베딩 캐시 없음 → build_embeddings 자동 실행", flush=True)
        import build_embeddings
        build_embeddings.main()
    if not os.path.exists(DB):
        raise SystemExit("온톨로지 빌드 실패 — projects/ 세션 데이터가 있는지 확인")


def _load_sqlite() -> tuple[list, list, list]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
    nodes = [dict(r) for r in conn.execute("SELECT * FROM ontology_nodes")]
    edges = [dict(r) for r in conn.execute("SELECT * FROM ontology_edges")]
    conn.close()
    return sessions, nodes, edges


def main() -> None:
    _ensure_local_assets()
    import store_mysql as M
    import vector_redis as V

    emb = np.load(EMB).astype(np.float32)
    meta = json.load(open(META, encoding="utf-8"))
    sessions, nodes, edges = _load_sqlite()

    # 1) MySQL 원천 적재 (멱등: base 청크 먼저 제거 후 재삽입)
    with M.connect() as conn:
        removed = M.clear_base_chunks(conn)
        if removed:
            print(f"기존 base 청크 {removed}건 제거(재실행 멱등)")
        removed_onto = M.clear_ontology(conn)
        if any(removed_onto.values()):
            print(f"기존 온톨로지 제거(스테일 엣지 방지, 재실행 멱등): {removed_onto}")
        for s in sessions:
            M.upsert_session(conn, {
                "session_id": s["session_id"], "user_id": s.get("user_id", ""),
                "title": s.get("title"), "coding_type": s.get("coding_type"),
                "phase": s.get("phase"), "raw": s,
            })
        for n in nodes:
            node_meta = n.get("meta")  # 주의: 바깥 `meta`(chunk_meta 리스트) 섀도잉 금지
            if isinstance(node_meta, str) and node_meta:
                try:
                    node_meta = json.loads(node_meta)
                except json.JSONDecodeError:
                    node_meta = None
            M.upsert_node(conn, n["key"], n["node_type"], n.get("label", ""),
                          n.get("level", 0), meta=node_meta)
        for e in edges:
            M.upsert_edge(conn, e["src"], e["dst"], e["rel"], e.get("weight", 1.0))
        for i, r in enumerate(meta):
            M.insert_chunk(conn, {
                # chunk_id 보존: realized_by.dst(원천 sqlite id) 와 정합시켜 죽은 링크 방지
                "chunk_id": r.get("chunk_id"),
                "session_id": r.get("session_id"), "chunk_type": "learning_note",
                "coding_type": r.get("coding_type"), "concept_key": r.get("concept_key"),
                "title": r.get("title"), "content": r.get("content", ""),
                # #44 부가 필드 (chunk_meta.json 에서 전달 — build_embeddings 가 채움)
                "intent": r.get("intent"), "domain": r.get("domain"),
                "difficulty": r.get("difficulty"), "modi_keys": r.get("modi_keys"),
                "payload": r.get("payload"),
                "embedding": emb[i].tolist(), "source": "base",
            })
        print("MySQL 적재:", M.counts(conn))

    # 2) Redis 벡터 인덱스 — base 문서만 갱신(#114). 인덱스 전체를 reset 하면 라이브
    #    registered(파일이 아니라 MySQL+Redis 에만 쌓임)가 검색에서 사라지므로, base 키
    #    (kchunk:base:*)만 지우고 재색인한다. registered(kchunk:reg:*)는 그대로 보존.
    V.ensure_index()
    _rc = V.client()
    _base_keys = list(_rc.scan_iter(match=f"{V.PREFIX}base:*"))
    if _base_keys:
        _rc.delete(*_base_keys)
    print(f"Redis base 문서 {len(_base_keys)}건 제거 후 재색인(registered 보존)")
    for i, r in enumerate(meta):
        V.upsert(f"base:{r.get('chunk_id', i)}", emb[i],
                 title=r.get("title", ""), content=r.get("content", ""),
                 coding_type=r.get("coding_type") or "any", source="base",
                 concept_key=r.get("concept_key") or "",
                 domain=r.get("domain") or "", difficulty=r.get("difficulty") or "",
                 intent=r.get("intent") or "", payload=r.get("payload"),
                 session_id=r.get("session_id") or "")

    # 2b) 개념 centroid 적재 (온프렘 Redis 재랭킹용, #38) — 로컬 _centroids 와 동형:
    #     행 정규화 → 개념별 평균 → 정규화. search_lib._redis_search 가 이를 로드해 재랭킹.
    row_norm = np.linalg.norm(emb, axis=1, keepdims=True)
    embn = np.divide(emb, row_norm, out=np.zeros_like(emb), where=row_norm > 0)
    idx_by_key: dict[str, list[int]] = {}
    for i, r in enumerate(meta):
        ck = r.get("concept_key")
        # centroid 는 학습노트(개념 설명)로만 — code/design 재사용 청크 제외(#3 gold 회귀 수정).
        if ck and r.get("chunk_type", "learning_note") == "learning_note":
            idx_by_key.setdefault(ck, []).append(i)
    V.clear_centroids()
    for k, idxs in idx_by_key.items():
        c = embn[idxs].mean(0)
        cn = np.linalg.norm(c)
        if cn > 0:
            c = c / cn
        V.upsert_centroid(k, c)
    print(f"Redis centroid 적재: {len(idx_by_key)} concepts")

    # 3) 등록물(source=registered)은 위에서 base 만 갱신했으므로 인덱스에 그대로 보존된다.
    #    (원천 MySQL 과 재동기가 필요하면 scripts/reindex_registered.py 로 별도 복원.)
    total = V.count()
    print(f"Redis 색인 완료: base {len(meta)} 재색인 + registered {total - len(meta)} 보존 "
          f"= {total} docs")


if __name__ == "__main__":
    main()
