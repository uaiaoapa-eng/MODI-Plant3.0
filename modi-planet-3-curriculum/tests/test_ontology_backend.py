"""ontology_lib 백엔드 스위치(local sqlite | mysql_redis MySQL) 검증.

MySQL 은 테스트 환경에 없으므로 mysql 분기는 fake 연결로 SQL 배선을 결정적으로
검증하고, 로컬 sqlite 경로는 realized_by → chunks 조인 정합을 스모크로 확인한다.
(Track A: 백필 시 chunk_id 를 보존해 realized_by.dst 와 knowledge_chunks.chunk_id 정합)
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ontology_lib  # noqa: E402
import store_mysql  # noqa: E402

DB_PATH = ontology_lib.DB_PATH


class _FakeCursor:
    """pymysql DictCursor 흉내 — execute 된 (sql, params) 를 기록만 한다."""

    def __init__(self, log: list) -> None:
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params) -> None:
        self._log.append((sql, params))

    def fetchall(self) -> list:
        return []


class _FakeMySQLConn:
    _is_mysql = True

    def __init__(self) -> None:
        self.executed: list = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)

    def close(self) -> None:
        pass


# ---- graph_conn 디스패치 -------------------------------------------------

def test_graph_conn_local_returns_sqlite(monkeypatch):
    monkeypatch.setattr(ontology_lib, "RAG_BACKEND", "local")
    conn = ontology_lib.graph_conn()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert ontology_lib._is_mysql(conn) is False
    finally:
        conn.close()


def test_graph_conn_mysql_dispatches_and_flags(monkeypatch):
    class _Bare:
        def close(self):
            pass

    sentinel = _Bare()
    monkeypatch.setattr(ontology_lib, "RAG_BACKEND", "mysql_redis")
    monkeypatch.setattr(store_mysql, "open_conn", lambda: sentinel)
    conn = ontology_lib.graph_conn()
    assert conn is sentinel
    assert ontology_lib._is_mysql(conn) is True


# ---- mysql SQL 분기(원천 테이블/컬럼/placeholder) --------------------------

def test_prerequisites_mysql_sql():
    conn = _FakeMySQLConn()
    ontology_lib.prerequisites(conn, "loop")
    sql, params = conn.executed[-1]
    assert "src_key" in sql and "dst_key" in sql and "%s" in sql
    assert "WITH RECURSIVE" in sql
    assert params == ("loop",)


def test_related_mysql_sql():
    conn = _FakeMySQLConn()
    ontology_lib.related(conn, "loop", top=4)
    sql, params = conn.executed[-1]
    assert "key_name" in sql and "e.dst_key" in sql and "relates_to" in sql
    assert params == ("loop", 4)


def test_chunks_for_mysql_targets_knowledge_chunks():
    conn = _FakeMySQLConn()
    ontology_lib.chunks_for(conn, "loop", "blockly", limit=6)
    sql, params = conn.executed[-1]
    # realized_by.dst_key(VARCHAR) → knowledge_chunks.chunk_id(BIGINT) 캐스트 조인
    assert "knowledge_chunks" in sql
    assert "CAST(e.dst_key AS UNSIGNED)" in sql
    assert "c.coding_type=%s" in sql
    assert params == ("loop", "blockly", 6)


def test_chunks_for_mysql_any_skips_coding_filter():
    conn = _FakeMySQLConn()
    ontology_lib.chunks_for(conn, "loop", "any", limit=3)
    sql, params = conn.executed[-1]
    assert "c.coding_type=%s" not in sql  # coding_type 필터 미적용(SELECT 목록의 컬럼은 존재)
    assert params == ("loop", 3)


def test_uses_mysql_sql():
    conn = _FakeMySQLConn()
    ontology_lib.uses(conn, "loop", limit=8)
    sql, params = conn.executed[-1]
    assert "key_name" in sql and "e.dst_key" in sql and "'uses'" in sql
    assert params == ("loop", 8)


# ---- store_mysql.get_chunks_by_session (EDU-27 직접서브 문서복원 세션 조인) ----

class _FakeRowsCursor:
    """execute 된 (sql, params) 를 기록하고, 미리 정해둔 rows 를 fetchall 로 돌려준다."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params) -> None:
        self.executed = (sql, params)

    def fetchall(self) -> list:
        return self._rows


class _FakeRowsConn:
    def __init__(self, rows: list) -> None:
        self._cur = _FakeRowsCursor(rows)

    def cursor(self):
        return self._cur


def test_get_chunks_by_session_filters_by_kind():
    import json

    rows = [{"chunk_type": "learning_note", "title": "T", "content": "C",
            "payload": json.dumps({"kind": "learning_note", "what": "w"})}]
    conn = _FakeRowsConn(rows)
    out = store_mysql.get_chunks_by_session(conn, "sess1", ["learning_note", "design_doc"])
    sql, params = conn._cur.executed
    assert "session_id=%s" in sql and "chunk_type IN" in sql
    assert params == ("sess1", "learning_note", "design_doc")
    # payload 가 JSON 문자열이면 dict 로 역직렬화된다.
    assert out[0]["payload"] == {"kind": "learning_note", "what": "w"}


def test_get_chunks_by_session_no_kind_filter():
    conn = _FakeRowsConn([])
    out = store_mysql.get_chunks_by_session(conn, "sess1")
    sql, params = conn._cur.executed
    assert "chunk_type IN" not in sql
    assert params == ("sess1",)
    assert out == []


def test_get_chunks_by_session_payload_already_dict():
    """DictCursor 가 이미 dict 로 파싱해 준 payload(JSON 컬럼 드라이버 차이)도 그대로 통과."""
    rows = [{"chunk_type": "design_doc", "title": "D", "content": "",
            "payload": {"kind": "design_doc", "project_name": "P"}}]
    conn = _FakeRowsConn(rows)
    out = store_mysql.get_chunks_by_session(conn, "sess2", None)
    assert out[0]["payload"]["project_name"] == "P"


# ---- 로컬 sqlite 정합 스모크 ----------------------------------------------

@pytest.mark.skipif(not os.path.exists(DB_PATH), reason="로컬 ontology.db 미빌드")
def test_local_realized_by_join_intact():
    """realized_by 엣지가 있는 개념은 chunks_for 로 실제 카드가 나와야 한다
    (죽은 링크면 조인 0건). 로컬 chunks 는 원천 chunk_id 공간을 그대로 쓴다."""
    conn = ontology_lib.connect()
    try:
        row = conn.execute(
            "SELECT src FROM ontology_edges WHERE rel='realized_by' LIMIT 1"
        ).fetchone()
        assert row is not None, "realized_by 엣지가 하나도 없음"
        cards = ontology_lib.chunks_for(conn, row["src"], "any")
        assert cards, f"'{row['src']}' realized_by 조인이 0건 — 죽은 링크"
        assert "content" in cards[0] and "title" in cards[0]
    finally:
        conn.close()
