"""store_mysql.insert_chunk dedup 검증 (#116).

registered 되먹임은 자연키 (session_id, title, chunk_type, source) 로 dedup 되어
writeback 재실행(파일 스토어 소실로 _seen 이 비었을 때 포함)에도 MySQL 에 중복
행이 쌓이지 않아야 한다. base(chunk_id 명시)는 dedup 없이 id 를 보존해야 한다.

MySQL 이 테스트 환경에 없으므로 insert_chunk 가 실제로 실행하는 SELECT/UPDATE/INSERT
를 in-memory 테이블로 흉내내, 삽입/갱신 결과를 동작으로 검증한다.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import store_mysql  # noqa: E402

_COLS_RE = re.compile(r"INSERT INTO knowledge_chunks \(([^)]+)\)")
_SET_RE = re.compile(r"SET (.+?) WHERE chunk_id", re.S)


class _FakeCursor:
    """knowledge_chunks 를 dict 로 흉내내는 pymysql DictCursor 대역."""

    def __init__(self, db) -> None:
        self._db = db
        self._fetch = None
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=()) -> None:
        s = sql.strip()
        if s.startswith("SELECT chunk_id"):
            sid, title, ctype, source = params
            self._fetch = next(
                ({"chunk_id": cid} for cid, r in self._db["rows"].items()
                 if r["session_id"] == sid and r["title"] == title
                 and r["chunk_type"] == ctype and r["source"] == source),
                None,
            )
        elif s.startswith("UPDATE knowledge_chunks"):
            set_cols = [c.split("=")[0].strip()
                        for c in _SET_RE.search(s).group(1).split(",")]
            *vals, eid = params
            self._db["rows"][eid].update(dict(zip(set_cols, vals)))
        elif s.startswith("INSERT INTO knowledge_chunks"):
            cols = [c.strip() for c in _COLS_RE.search(s).group(1).split(",")]
            row = dict(zip(cols, params))
            if "chunk_id" in row:
                cid = row["chunk_id"]
            else:
                self._db["auto"] += 1
                cid = self._db["auto"]
                row["chunk_id"] = cid
            self._db["rows"][cid] = row
            self.lastrowid = cid
        else:  # pragma: no cover - 방어
            raise AssertionError(f"예상치 못한 SQL: {s[:40]}")

    def fetchone(self):
        return self._fetch


class _FakeConn:
    def __init__(self) -> None:
        self.db = {"rows": {}, "auto": 0}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    @property
    def rows(self) -> dict:
        return self.db["rows"]


def _registered(**over) -> dict:
    base = {"session_id": "s1", "title": "노트A", "chunk_type": "learning_note",
            "source": "registered", "content": "v1", "embedding": [0.1, 0.2]}
    base.update(over)
    return base


def test_registered_reinsert_is_idempotent_and_updates():
    """같은 자연키 registered 재삽입 → 새 행 없이 기존 행 UPDATE(내용 갱신)."""
    conn = _FakeConn()
    id1 = store_mysql.insert_chunk(conn, _registered(content="v1"))
    id2 = store_mysql.insert_chunk(conn, _registered(content="v2"))
    assert id1 == id2                         # 같은 자연키 → 같은 chunk_id
    assert len(conn.rows) == 1                # 중복 삽입 없음(#116 핵심)
    assert conn.rows[id1]["content"] == "v2"  # UPDATE 로 내용 갱신됨


def test_registered_distinct_natural_keys_are_separate_rows():
    """session/title 이 같아도 chunk_type 이 다르면 별개 행(자연키에 chunk_type 포함)."""
    conn = _FakeConn()
    a = store_mysql.insert_chunk(conn, _registered(chunk_type="learning_note"))
    b = store_mysql.insert_chunk(conn, _registered(chunk_type="design_doc"))
    c = store_mysql.insert_chunk(conn, _registered(title="노트B"))
    assert len({a, b, c}) == 3
    assert len(conn.rows) == 3


def test_base_with_explicit_id_never_dedups():
    """base 는 chunk_id 를 보존하고 dedup 하지 않는다(realized_by 조인 정합)."""
    conn = _FakeConn()
    a = store_mysql.insert_chunk(conn, {
        "chunk_id": 5, "session_id": "s1", "title": "t",
        "chunk_type": "learning_note", "source": "base", "content": "x"})
    b = store_mysql.insert_chunk(conn, {
        "chunk_id": 6, "session_id": "s1", "title": "t",
        "chunk_type": "learning_note", "source": "base", "content": "y"})
    assert a == 5 and b == 6                   # 명시 id 보존
    assert len(conn.rows) == 2                 # 같은 자연키라도 base 는 두 행 유지
