"""정합 전용(1회성) — data/registered.jsonl(파일 스토어 잔존분) → MySQL knowledge_chunks(source=registered).

#120: 과거 로컬(파일) 모드 등록물이 Redis 인덱스(kchunk:reg:*)에는 색인됐지만 MySQL 원천에는
적재되지 않아 desync(Redis reg≈3071 vs MySQL registered≈413)가 남았다. 조사 결과 파일이 완전한
상위집합이고(자연키 기준 MySQL 은 부분집합 — mysql-only=0) MySQL 에만 없는 재사용물이 ≈2657건
(learning_note/design_doc/code, 26 실유저)이다. 파괴적 옵션 B(Redis 제거)는 이 유효 벡터를
없애므로 채택하지 않고(옵션 A), 파일을 MySQL 로 1회 보강해 MySQL 을 완전한 원천으로 만든다.

임베딩은 파일 jsonl 엔 없고 Redis reg 키(FLOAT32)에 100% 존재하므로, 파일 'id' ↔ Redis
reg:{id} 조인(실측 3071/3071, title 일치)으로 가져온다.

- 멱등: insert_chunk(#116)가 registered 를 자연키(session_id,title,chunk_type,source)로 dedup
  → 재실행 시 기존 행은 UPDATE, 신규만 INSERT(중복이 쌓이지 않음). 파일 'id' 는 chunk_id 로
  강제하지 않아(MySQL AUTO_INCREMENT 유지) base id 와 충돌하지 않는다.
- FK: knowledge_chunks.session_id → sessions. 파일에 있으나 sessions 에 없는 세션은 스텁으로
  upsert_session 후 적재(#120 사용자 결정). 이미 있는 세션은 건드리지 않는다(제목/raw 덮어쓰기
  방지 — _mysql_register 의 무조건 upsert 와 다른 점).
- Redis/파일은 읽기만 — 삭제하지 않는다(비파괴). reset_index/backfill_onprem 로직은 안 건드림.

실행(rag-search 컨테이너 안 — DATABASE_URL·VECTOR_REDIS_URL 이 설정된 곳):
    PYTHONPATH=scripts python scripts/backfill_registered_from_file.py --dry-run
    PYTHONPATH=scripts python scripts/backfill_registered_from_file.py
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

REGISTERED_FILE = os.getenv("REGISTERED_FILE", "data/registered.jsonl")


def load_file_rows(path: str) -> list[dict]:
    """registered.jsonl 로드. 깨진 줄은 건너뛴다(로그 없이 — 원본 보존, 읽기 전용)."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def natural_key(row: dict) -> tuple[str, str, str]:
    """insert_chunk 과 동일한 자연키(session_id, title, chunk_type). source 는 항상 registered."""
    return (row.get("session_id") or "",
            (row.get("title") or "").strip(),
            row.get("chunk_type") or "learning_note")


def embedding_for(V, rid) -> list | None:
    """Redis reg:{id} 해시의 FLOAT32 embedding → float 리스트. 없거나 차원 불일치면 None."""
    if rid is None:
        return None
    raw = V.client().hget(f"{V.PREFIX}reg:{rid}", "embedding")
    if not raw:
        return None
    vec = np.frombuffer(raw, dtype=np.float32)
    if len(vec) != V.DIM:
        return None
    return vec.tolist()


def _session_ids(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT session_id FROM sessions")
        return {r["session_id"] for r in cur.fetchall()}


def _registered_nk(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT session_id,title,chunk_type FROM knowledge_chunks WHERE source='registered'")
        return {(r["session_id"], (r["title"] or "").strip(), r["chunk_type"])
                for r in cur.fetchall()}


def backfill(M, V, rows: list[dict], *, dry_run: bool = False, log=print) -> dict:
    """파일 행을 MySQL registered 로 보강. 반환: 계측 dict.

    dry_run: DB 를 쓰지 않고 계획(스텁 생성/INSERT/UPDATE 예상 건수)만 계산해 반환.
    """
    with M.connect() as conn:
        db_sids = _session_ids(conn)
        existing_nk = _registered_nk(conn)

    # session_id 없는 행은 FK 를 만족할 수 없고 스텁할 대상도 없어 제외(방어 — 실측 0건 기대).
    usable = [r for r in rows if r.get("session_id")]
    skipped_no_session = len(rows) - len(usable)

    file_sids = {r["session_id"] for r in usable}
    missing_sids = {s for s in file_sids if s not in db_sids}
    will_update = sum(1 for r in usable if natural_key(r) in existing_nk)
    will_insert = len(usable) - will_update

    log(f"파일 행 {len(rows)} (적재대상 {len(usable)}, session_id 없음 {skipped_no_session})")
    log(f"세션: 파일 {len(file_sids)} / 기존 present {len(file_sids & db_sids)} / "
        f"스텁 생성 대상(누락) {len(missing_sids)}")
    log(f"청크 계획: UPDATE(기존 자연키) {will_update} / INSERT(신규) {will_insert}")

    stats = {"rows": len(rows), "usable": len(usable),
             "skipped_no_session": skipped_no_session,
             "stub_sessions": len(missing_sids),
             "planned_update": will_update, "planned_insert": will_insert}

    if dry_run:
        stats.update(inserted=0, updated=0, stub_created=0, emb_missing=0, dry_run=True)
        return stats

    # 1) 누락 세션 스텁 생성(있는 세션은 건드리지 않음 → 제목/raw 보존).
    stub_seed: dict[str, dict] = {}
    for r in usable:
        s = r["session_id"]
        if s in missing_sids and s not in stub_seed:
            stub_seed[s] = r
    stub_created = 0
    with M.connect() as conn:
        for s, r in stub_seed.items():
            M.upsert_session(conn, {
                "session_id": s, "user_id": r.get("user_id") or "",
                "title": r.get("title"), "coding_type": r.get("coding_type"), "raw": {},
            })
            stub_created += 1
    log(f"세션 스텁 생성: {stub_created}건")

    # 2) 청크 적재(임베딩은 Redis reg:{id} 에서). insert_chunk 가 자연키 dedup 담당.
    inserted = updated = emb_missing = 0
    seen = set(existing_nk)
    with M.connect() as conn:
        for r in usable:
            emb = embedding_for(V, r.get("id"))
            if emb is None:
                emb_missing += 1
            c = {**r, "source": "registered", "embedding": emb}
            k = natural_key(r)
            M.insert_chunk(conn, c)
            if k in seen:
                updated += 1
            else:
                inserted += 1
                seen.add(k)
    log(f"적재 완료: INSERT {inserted} / UPDATE {updated} / 임베딩 없음 {emb_missing}")

    # 3) 검증 계측(최종 registered 총계).
    with M.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM knowledge_chunks WHERE source='registered'")
        total = cur.fetchone()["n"]
    log(f"MySQL registered 총계: {total}")

    stats.update(inserted=inserted, updated=updated, stub_created=stub_created,
                 emb_missing=emb_missing, total_registered=total, dry_run=False)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="파일 registered → MySQL 보강(#120 옵션 A, 1회성)")
    ap.add_argument("--dry-run", action="store_true", help="적재 없이 대상 건수·계획만 출력")
    ap.add_argument("--file", default=REGISTERED_FILE, help="registered.jsonl 경로")
    args = ap.parse_args()

    import store_mysql as M
    import vector_redis as V

    rows = load_file_rows(args.file)
    print(f"파일 registered 로드: {args.file}")
    backfill(M, V, rows, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
