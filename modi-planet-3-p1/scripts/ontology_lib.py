"""온톨로지 공용 로직 (빌더/질의 서버 공유). stdlib만 사용."""

from __future__ import annotations

import json
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_PATH = os.path.join(BASE, "scripts", "ontology_seed.json")
DB_PATH = os.getenv("ONTOLOGY_DB", os.path.join(BASE, "data", "ontology.db"))
# 서빙 백엔드: local(sqlite ontology.db, 기본) | mysql_redis(온프렘 MySQL 원천).
# search_lib.RAG_BACKEND 와 동일 변수 — 그래프 도출도 벡터검색과 같은 원천을 보게 통일.
RAG_BACKEND = os.getenv("RAG_BACKEND", "local")

# 페르소나 학년 → 허용 개념 최대 레벨(선수학습 깊이). 낮은 학년엔 심화개념 도출 차단.
GRADE_LEVEL_CAP = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 99, 8: 99, 9: 99}


def load_seed() -> list[dict]:
    with open(SEED_PATH, encoding="utf-8") as f:
        return json.load(f)["concepts"]


def match_concepts(text: str, concepts: list[dict], top: int = 3) -> list[tuple[str, float]]:
    """텍스트를 개념에 부분일치로 매칭. 반환: [(concept_key, score)] 내림차순."""
    t = (text or "").lower()
    scored: list[tuple[str, float]] = []
    for c in concepts:
        hits = 0
        best_len = 0
        for a in c.get("aliases", []):
            al = a.lower()
            if al and al in t:
                hits += 1
                best_len = max(best_len, len(al))
        if hits:
            # 긴 alias가 걸리면 더 확실 → 길이도 가중
            scored.append((c["key"], hits + best_len / 100.0))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top]


def concept_levels(concepts: list[dict]) -> dict[str, int]:
    """선수학습 체인의 최대 깊이 = 개념 레벨(0=기초)."""
    by_key = {c["key"]: c for c in concepts}
    memo: dict[str, int] = {}

    def depth(k: str, seen: frozenset = frozenset()) -> int:
        if k in memo:
            return memo[k]
        if k in seen:  # 사이클 방어
            return 0
        pre = by_key.get(k, {}).get("prerequisite", [])
        d = 0 if not pre else 1 + max(depth(p, seen | {k}) for p in pre if p in by_key)
        memo[k] = d
        return d

    return {c["key"]: depth(c["key"]) for c in concepts}


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """sqlite ontology.db 연결. 빌드 도구(build_ontology/build_embeddings)와 로컬
    서빙이 공유. RAG_BACKEND 와 무관하게 항상 sqlite — 빌드는 로컬 자산 대상이므로."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def graph_conn():
    """서빙 경로용 백엔드 인지 연결. RAG_BACKEND=mysql_redis 면 MySQL 원천,
    아니면 sqlite. 반환 연결은 sqlite/pymysql 공히 conn.close() 로 정리한다.
    질의 함수(prerequisites/related/chunks_for/uses)가 _is_mysql 플래그로 분기한다."""
    if RAG_BACKEND == "mysql_redis":
        import store_mysql

        conn = store_mysql.open_conn()
        conn._is_mysql = True
        return conn
    return connect()


def _is_mysql(conn) -> bool:
    return getattr(conn, "_is_mysql", False)


def _rows(conn, sql: str, params) -> list[dict]:
    """백엔드 무관 조회 → list[dict]. sqlite 는 conn.execute, pymysql 은 cursor 사용."""
    if _is_mysql(conn):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def prerequisites(conn, key: str) -> list[dict]:
    """재귀 CTE로 선수학습 전체 체인 (MySQL 8.0 / sqlite 공통 지원)."""
    if _is_mysql(conn):
        sql = """
        WITH RECURSIVE chain(k) AS (
          SELECT dst_key FROM ontology_edges WHERE src_key=%s AND rel='prerequisite'
          UNION
          SELECT e.dst_key FROM ontology_edges e JOIN chain c ON e.src_key=c.k
           WHERE e.rel='prerequisite'
        )
        SELECT n.key_name AS `key`, n.label, n.level FROM chain
          JOIN ontology_nodes n ON n.key_name=chain.k
         ORDER BY n.level
        """
    else:
        sql = """
        WITH RECURSIVE chain(k) AS (
          SELECT dst FROM ontology_edges WHERE src=? AND rel='prerequisite'
          UNION
          SELECT e.dst FROM ontology_edges e JOIN chain c ON e.src=c.k
           WHERE e.rel='prerequisite'
        )
        SELECT n.key, n.label, n.level FROM chain
          JOIN ontology_nodes n ON n.key=chain.k
         ORDER BY n.level
        """
    return _rows(conn, sql, (key,))


def related(conn, key: str, top: int = 4) -> list[dict]:
    if _is_mysql(conn):
        sql = """
        SELECT n.key_name AS `key`, n.label, e.weight FROM ontology_edges e
          JOIN ontology_nodes n ON n.key_name=e.dst_key
         WHERE e.src_key=%s AND e.rel='relates_to'
         ORDER BY e.weight DESC LIMIT %s
        """
    else:
        sql = """
        SELECT n.key, n.label, e.weight FROM ontology_edges e
          JOIN ontology_nodes n ON n.key=e.dst
         WHERE e.src=? AND e.rel='relates_to'
         ORDER BY e.weight DESC LIMIT ?
        """
    return _rows(conn, sql, (key, top))


def chunks_for(conn, key: str, coding_type: str | None, limit: int = 6) -> list[dict]:
    """개념을 realize하는 학습노트 청크 (coding_type 필터).

    realized_by.dst(dst_key) → 청크 chunk_id. MySQL 은 knowledge_chunks(원천),
    sqlite 는 chunks(로컬 빌드) 테이블을 조인한다. dst_key 는 VARCHAR 이므로
    MySQL 에선 UNSIGNED 캐스트로 chunk_id(BIGINT) 와 정합.
    """
    if _is_mysql(conn):
        q = """
        SELECT c.title, c.content, c.coding_type, c.session_id
          FROM ontology_edges e
          JOIN knowledge_chunks c ON c.chunk_id=CAST(e.dst_key AS UNSIGNED)
         WHERE e.src_key=%s AND e.rel='realized_by'
        """
        ph = "%s"
    else:
        q = """
        SELECT c.title, c.content, c.coding_type, c.session_id
          FROM ontology_edges e
          JOIN chunks c ON c.chunk_id=e.dst
         WHERE e.src=? AND e.rel='realized_by'
        """
        ph = "?"
    args: list = [key]
    if coding_type and coding_type != "any":
        q += f" AND c.coding_type={ph}"
        args.append(coding_type)
    q += f" LIMIT {ph}"
    args.append(limit)
    return _rows(conn, q, tuple(args))


def uses(conn, key: str, limit: int = 8) -> list[str]:
    """개념이 uses 하는 MODI 모듈 라벨. (rag_demo_app 의 _uses 를 이관·백엔드 통일)"""
    if _is_mysql(conn):
        sql = ("SELECT n.label FROM ontology_edges e JOIN ontology_nodes n"
               " ON n.key_name=e.dst_key WHERE e.src_key=%s AND e.rel='uses' LIMIT %s")
    else:
        sql = ("SELECT n.label FROM ontology_edges e JOIN ontology_nodes n"
               " ON n.key=e.dst WHERE e.src=? AND e.rel='uses' LIMIT ?")
    return [r["label"] for r in _rows(conn, sql, (key, limit))]
