"""backfill_registered_from_file 검증 (#120 옵션 A).

파일 스토어 registered → MySQL 보강이:
  - 기존 자연키(session_id,title,chunk_type)는 UPDATE, 신규는 INSERT (insert_chunk #116 dedup),
  - sessions 에 없는 세션만 스텁 생성(있는 세션은 upsert 하지 않음 → 제목/raw 보존),
  - 임베딩을 Redis reg:{id} 에서 가져오고(없으면 None) 재실행에 멱등해야 한다.

MySQL/Redis 가 테스트 환경에 없으므로, insert_chunk 가 실행하는 SELECT/UPDATE/INSERT 와
sessions/COUNT 조회를 in-memory 로 흉내내고 store_mysql 의 실제 insert_chunk/upsert_session
을 그대로 태워 동작으로 검증한다(가짜 SQL 파서는 test_store_mysql_dedup 와 동형).
"""
from __future__ import annotations

import array
import contextlib
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import backfill_registered_from_file as B  # noqa: E402
import store_mysql  # noqa: E402

_COLS_RE = re.compile(r"INSERT INTO knowledge_chunks \(([^)]+)\)")
_SET_RE = re.compile(r"SET (.+?) WHERE chunk_id", re.S)


class _FakeCursor:
    def __init__(self, db) -> None:
        self._db = db
        self._one = None
        self._all = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=()) -> None:
        s = sql.strip()
        if s.startswith("SELECT session_id FROM sessions"):
            self._all = [{"session_id": x} for x in self._db["sessions"]]
        elif s.startswith("INSERT INTO sessions"):
            self._db["sessions"].add(params[0])  # upsert_session: session_id 가 첫 파라미터
        elif s.startswith("SELECT session_id,title,chunk_type FROM knowledge_chunks"):
            self._all = [{"session_id": r["session_id"], "title": r.get("title"),
                          "chunk_type": r.get("chunk_type")}
                         for r in self._db["rows"].values() if r.get("source") == "registered"]
        elif s.startswith("SELECT COUNT(*)"):
            self._one = {"n": sum(1 for r in self._db["rows"].values()
                                  if r.get("source") == "registered")}
        elif s.startswith("SELECT chunk_id"):
            sid, title, ctype, source = params
            self._one = next(
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
            raise AssertionError(f"예상치 못한 SQL: {s[:50]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all or []


class _FakeConn:
    def __init__(self, db) -> None:
        self.db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass


def _fake_M(db, calls):
    """store_mysql 대역: connect 는 공유 conn, insert_chunk/upsert_session 은 실제 구현."""
    m = types.ModuleType("store_mysql_fake")

    @contextlib.contextmanager
    def connect():
        yield _FakeConn(db)

    def upsert_session(conn, s):
        calls.append(s["session_id"])          # 어떤 세션에 대해 upsert 됐는지 스파이
        return store_mysql.upsert_session(conn, s)

    m.connect = connect
    m.upsert_session = upsert_session
    m.insert_chunk = store_mysql.insert_chunk  # #116 dedup 을 실제로 태운다
    return m


def _fake_V(embeddings, dim=4):
    """vector_redis 대역: reg:{id} 해시의 embedding 을 FLOAT32 바이트로 돌려준다."""
    v = types.ModuleType("vector_redis_fake")
    v.PREFIX = "kchunk:"
    v.DIM = dim

    class _C:
        def hget(self, key, field):
            emb = embeddings.get(key.split(":")[-1])
            if emb is None:
                return None
            return array.array("f", [float(x) for x in emb]).tobytes()

    v.client = lambda: _C()
    return v


def _seed_existing(db, calls):
    """기존 MySQL 상태: 세션 s1 present + registered 1건(s1/노트A/learning_note, content=v0)."""
    db["sessions"].add("s1")
    M = _fake_M(db, calls)
    with M.connect() as conn:
        M.insert_chunk(conn, {"session_id": "s1", "title": "노트A",
                              "chunk_type": "learning_note", "source": "registered",
                              "content": "v0"})


def _rows():
    return [
        {"id": "10", "session_id": "s1", "title": "노트A",
         "chunk_type": "learning_note", "content": "v1"},   # 기존 자연키 → UPDATE
        {"id": "11", "session_id": "s1", "title": "노트B",
         "chunk_type": "design_doc", "content": "d1"},       # 신규(세션 present)
        {"id": "12", "session_id": "s2", "title": "노트C",
         "chunk_type": "code", "content": "c1"},             # 신규(세션 누락 → 스텁)
    ]


def test_backfill_inserts_updates_and_stubs_missing_session():
    db = {"rows": {}, "auto": 4000, "sessions": set()}
    calls: list[str] = []
    _seed_existing(db, calls)
    calls.clear()  # 시딩 중 호출은 제외
    M = _fake_M(db, calls)
    V = _fake_V({"10": [1, 2, 3, 4], "11": [5, 6, 7, 8]})  # id 12 는 임베딩 없음

    st = B.backfill(M, V, _rows(), log=lambda *a: None)

    assert st["planned_update"] == 1 and st["planned_insert"] == 2
    assert st["inserted"] == 2 and st["updated"] == 1
    assert st["stub_created"] == 1 and st["emb_missing"] == 1
    assert st["total_registered"] == 3            # 기존 1 + 신규 2
    assert calls == ["s2"]                        # 누락 세션만 스텁, present(s1) 은 안 건드림
    assert db["sessions"] == {"s1", "s2"}
    # 기존 행이 UPDATE 되어 내용/임베딩 갱신
    a = next(r for r in db["rows"].values() if r["title"] == "노트A")
    assert a["content"] == "v1" and a["embedding"] is not None


def test_backfill_is_idempotent_on_rerun():
    db = {"rows": {}, "auto": 4000, "sessions": set()}
    calls: list[str] = []
    _seed_existing(db, calls)
    M = _fake_M(db, calls)
    V = _fake_V({"10": [1, 2, 3, 4], "11": [5, 6, 7, 8]})

    B.backfill(M, V, _rows(), log=lambda *a: None)
    calls.clear()
    st2 = B.backfill(M, V, _rows(), log=lambda *a: None)  # 재실행

    assert st2["inserted"] == 0 and st2["updated"] == 3   # 전부 UPDATE
    assert st2["stub_created"] == 0                       # s2 이제 present
    assert st2["total_registered"] == 3                   # 중복 안 쌓임(#116)
    assert calls == []                                    # 스텁 upsert 없음


def test_dry_run_writes_nothing():
    db = {"rows": {}, "auto": 4000, "sessions": set()}
    calls: list[str] = []
    _seed_existing(db, calls)
    calls.clear()
    M = _fake_M(db, calls)
    V = _fake_V({"10": [1, 2, 3, 4], "11": [5, 6, 7, 8]})

    st = B.backfill(M, V, _rows(), dry_run=True, log=lambda *a: None)

    assert st["planned_update"] == 1 and st["planned_insert"] == 2 and st["stub_sessions"] == 1
    assert st["inserted"] == 0 and st["stub_created"] == 0
    assert len(db["rows"]) == 1 and db["sessions"] == {"s1"}  # DB 불변
    assert calls == []


def test_embedding_dim_mismatch_is_treated_as_missing():
    db = {"rows": {}, "auto": 4000, "sessions": set()}
    calls: list[str] = []
    _seed_existing(db, calls)
    M = _fake_M(db, calls)
    V = _fake_V({"10": [1, 2, 3], "11": [5, 6, 7, 8]})  # id 10 차원(3)≠DIM(4)

    st = B.backfill(M, V, _rows(), log=lambda *a: None)
    assert st["emb_missing"] == 2  # id 10(차원불일치) + id 12(부재)
