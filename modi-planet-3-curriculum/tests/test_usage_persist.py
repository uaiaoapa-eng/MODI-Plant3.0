"""#133: 사용량 영속 기록 — usage_turns 테이블 + rag-search /api/usage/add + /chat 이중쓰기.

- store_mysql.insert_usage_turn 이 올바른 컬럼/값으로 INSERT 한다(fake conn).
- rag_demo_app POST /api/usage/add 가 성공/실패를 {"ok":...} 로 반환한다.
- server.py /chat finally 가 pop_turn_usage() 회수 직후 rag-search 로 이중쓰기하되,
  실패해도(타임아웃/500) /chat 스트림은 정상 완료 + 세션 락이 해제된다(fail-open).
- RAG_UPSTREAM 미설정이면 POST 시도 자체를 하지 않는다.
- QUOTA_ENABLED=false(집행 킬스위치 꺼짐)여도 usage_turns 기록은 발생한다(집행/분석 역할 분리).
- 토큰 전부 0인 턴도 그대로 기록된다(재사용으로 절감 측정 대상 — 엣지 케이스).
"""
from __future__ import annotations

import os
import sys

import pytest

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store_mysql  # noqa: E402

try:
    from fastapi.testclient import TestClient
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)

from agent.quota import InMemoryQuotaStore, TokenUsage  # noqa: E402  (skip 블록 뒤에 와야 함)


# ── store_mysql.insert_usage_turn (fake conn) ──────────────────────────
class _FakeCursor:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        assert sql.strip().startswith("INSERT INTO usage_turns")
        self._db["rows"].append(params)


class _FakeConn:
    def __init__(self):
        self.db = {"rows": []}

    def cursor(self):
        return _FakeCursor(self.db)


def test_insert_usage_turn_inserts_all_columns_in_order():
    conn = _FakeConn()
    store_mysql.insert_usage_turn(conn, {
        "ts": "2026-07-13T12:00:00+09:00", "subject": "u:abc", "user_id": "abc",
        "session_id": "s1", "mode": "quick", "coding_type": "react",
        "input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 1,
        "cache_creation_tokens": 2, "weighted_tokens": 111, "trace_id": "tr1",
    })
    # 비용 계산에 쓰이는 앞부분은 **순서까지** 고정한다 — 여기가 밀리면 토큰이 엉뚱한
    # 컬럼으로 들어가 금액이 조용히 틀어진다.
    # 뒤에 붙는 관측 컬럼(started_at 이후)은 개수가 늘어나므로 여기서 세지 않는다.
    # 그쪽 검증은 tests/test_observability_ledger.py 가 맡는다.
    row = conn.db["rows"][0]
    assert row[:12] == (
        "2026-07-13T12:00:00+09:00", "u:abc", "abc", "s1", "quick", "react",
        10, 20, 1, 2, 111, "tr1",
    )
    assert row[12:14] == ("", ""), "llm_mode/reuse_tier 는 안 주면 '미상'(빈 값)"


def test_insert_usage_turn_defaults_missing_optional_fields():
    """토큰 전부 0인 턴(직접서브 등)도 기록 — 필수는 ts/subject 뿐, 나머지는 기본값."""
    conn = _FakeConn()
    store_mysql.insert_usage_turn(conn, {"ts": "2026-07-13T00:00:00+09:00", "subject": "s:sid"})
    row = conn.db["rows"][0]
    assert row[0:2] == ("2026-07-13T00:00:00+09:00", "s:sid")
    # 비용 컬럼은 전부 기본값(빈 문자열/0). 뒤에 붙는 관측 컬럼은 개수가 늘어나므로
    # 목록을 통째로 비교하지 않는다 — 컬럼이 하나 늘 때마다 깨지는 단언은 신호가 아니라 잡음이다.
    assert row[2:14] == ("", "", "", "", 0, 0, 0, 0, 0, "", "", "")
    assert "ok" in row[14:], "status 기본값이 'ok' 여야 한다"
    assert None in row[14:], "started_at 은 빈 값이 아니라 NULL 이어야 한다"


# ── rag_demo_app POST /api/usage/add ────────────────────────────────────
try:
    import rag_demo_app as app_mod
    _RAG_DEMO_APP_OK = True
except Exception as e:
    _RAG_DEMO_APP_OK = False
    _RAG_DEMO_APP_SKIP_REASON = str(e)


@pytest.fixture
def rag_demo_client(monkeypatch):
    if not _RAG_DEMO_APP_OK:
        pytest.skip(f"rag_demo_app import 불가(의존성/자산 미설치): {_RAG_DEMO_APP_SKIP_REASON}")
    return TestClient(app_mod.app)


_USAGE_BODY = {
    "ts": "2026-07-13T12:00:00+09:00", "subject": "u:abc", "user_id": "abc",
    "session_id": "s1", "mode": "quick", "coding_type": "react",
    "input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 0,
    "cache_creation_tokens": 0, "weighted_tokens": 110, "trace_id": "",
}


def test_api_usage_add_success(rag_demo_client, monkeypatch):
    monkeypatch.setattr(store_mysql, "insert_usage_turn", lambda conn, row: None)
    monkeypatch.setattr(store_mysql, "connect", lambda: _NullConnCtx())
    r = rag_demo_client.post("/api/usage/add", json=_USAGE_BODY)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_api_usage_add_failure_returns_500_with_truncated_error(rag_demo_client, monkeypatch):
    def _boom(conn, row):
        raise RuntimeError("x" * 300)

    monkeypatch.setattr(store_mysql, "insert_usage_turn", _boom)
    monkeypatch.setattr(store_mysql, "connect", lambda: _NullConnCtx())
    r = rag_demo_client.post("/api/usage/add", json=_USAGE_BODY)
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert len(body["error"]) <= 200


class _NullConnCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


# ── server.py /chat finally 이중쓰기 배선 ────────────────────────────────
class _FakeOrch:
    """LLM 호출 없이 토큰 usage 를 pop_turn_usage 로 회수하는 가짜 오케스트레이터."""
    _user_id = ""

    def __init__(self, usage=None):
        self._usage = usage if usage is not None else TokenUsage(input=10, output=20)

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}

    def pop_turn_usage(self):
        u, self._usage = self._usage, TokenUsage()
        return u


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    monkeypatch.setattr(server, "quota_store", InMemoryQuotaStore())
    return TestClient(server.app)


def test_no_post_attempted_when_rag_upstream_unset(client, monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "")
    calls = []
    monkeypatch.setattr(server, "_usage_writeback_upstream", lambda body: calls.append(body))
    with client.stream("POST", "/chat", json={"session_id": "no-upstream", "message": "x"}) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text
    assert calls == []


def test_usage_persisted_with_correct_body_when_upstream_set(client, monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag-search:8100")
    calls = []
    monkeypatch.setattr(server, "_usage_writeback_upstream", lambda body: calls.append(body))
    with client.stream(
        "POST", "/chat",
        json={"session_id": "sess-ok", "message": "x", "mode": "quick", "coding_type": "react"},
        params={"user_id": "u1"},
    ) as resp:
        text = "".join(resp.iter_lines())
    assert "hi" in text
    assert len(calls) == 1
    body = calls[0]
    assert body["subject"] == "u:u1"
    assert body["user_id"] == "u1"
    assert body["session_id"] == "sess-ok"
    assert body["mode"] == "quick"
    assert body["coding_type"] == "react"
    assert body["input_tokens"] == 10
    assert body["output_tokens"] == 20
    assert body["weighted_tokens"] == 10 + 20 * server.QUOTA_WEIGHT_OUTPUT
    assert "ts" in body and body["ts"]
    assert "trace_id" in body


def test_chat_completes_and_lock_released_when_upstream_post_fails(client, monkeypatch):
    """rag-search POST 실패(타임아웃/500)에도 /chat 스트림은 정상 완료되고 세션 락이 풀린다."""
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag-search:8100")

    def _boom(body):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(server, "_usage_writeback_upstream", _boom)
    captured = []
    monkeypatch.setattr(
        server, "capture_chat_exception",
        lambda exc, **tags: captured.append((exc, tags)))

    with client.stream("POST", "/chat", json={"session_id": "sess-boom", "message": "x"}) as resp:
        text = "".join(resp.iter_lines())

    assert "hi" in text
    assert server.session_locks.is_busy("sess-boom") is False
    assert any(tags.get("stage") == "usage_persist" for _, tags in captured)


def test_usage_persisted_even_when_quota_disabled():
    """QUOTA_ENABLED=false(집행 킬스위치 기본 꺼짐)가 usage_turns 기록을 막지 않는다."""
    assert server.QUOTA_ENABLED is False


def test_usage_persisted_when_quota_disabled_but_upstream_set(client, monkeypatch):
    monkeypatch.setattr(server, "QUOTA_ENABLED", False)
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag-search:8100")
    calls = []
    monkeypatch.setattr(server, "_usage_writeback_upstream", lambda body: calls.append(body))
    with client.stream("POST", "/chat", json={"session_id": "sess-quota-off", "message": "x"}) as resp:
        "".join(resp.iter_lines())
    assert len(calls) == 1  # 집행이 꺼져 있어도 분석 기록은 발생(역할 분리)


def test_zero_token_turn_is_still_persisted(client, monkeypatch):
    """토큰 전부 0인 턴(재사용으로 LLM 미호출 등)도 기록한다 — 절감 측정 대상."""
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch(usage=TokenUsage()))
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag-search:8100")
    calls = []
    monkeypatch.setattr(server, "_usage_writeback_upstream", lambda body: calls.append(body))
    with client.stream("POST", "/chat", json={"session_id": "sess-zero", "message": "x"}) as resp:
        "".join(resp.iter_lines())
    assert len(calls) == 1
    assert calls[0]["input_tokens"] == 0
    assert calls[0]["output_tokens"] == 0
    assert calls[0]["weighted_tokens"] == 0


# ── scripts/apply_schema.py: deploy/schema.sql 재적용 경로 (#149) ────────
import apply_schema as AS  # noqa: E402


class _FakeSchemaCursor:
    """DDL 로그를 남기는 가짜 커서.

    ensure_columns() 가 information_schema 를 조회하므로 fetchone 도 흉내낸다.
    existing_columns 에 있으면 "이미 있음"(ALTER 안 함), 없으면 "없음"(ALTER)을 답한다.
    """

    def __init__(self, log, existing_columns=None):
        self._log = log
        self._existing = existing_columns if existing_columns is not None else set()
        self._last = {"n": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        assert ";" not in sql  # 개별 문으로 이미 분리되어 넘어와야 함
        self._log.append(sql.strip())
        if "information_schema.COLUMNS" in sql:
            key = tuple(params or ())
            self._last = {"n": 1 if key in self._existing else 0}

    def fetchone(self):
        return self._last


class _FakeSchemaConn:
    def __init__(self, existing_columns=None):
        self.log = []
        self.committed = False
        self.closed = False
        # 기본은 "컬럼이 이미 다 있다" — 대부분의 테스트가 DDL 재적용만 보기 때문.
        self._existing = (existing_columns if existing_columns is not None
                          else {(t, c) for t, c, _d, _i in AS._ADD_COLUMNS})

    def cursor(self):
        return _FakeSchemaCursor(self.log, self._existing)

    def commit(self):
        self.committed = True

    def rollback(self):  # pragma: no cover - 실패 경로 방어
        pass

    def close(self):
        self.closed = True


def test_split_statements_extracts_all_ddl_from_real_schema_sql():
    """실제 deploy/schema.sql 의 모든 테이블이 쪼개져 나오는가.

    개수를 세지 않고 **이름**으로 확인한다 — 개수 단언은 테이블이 늘 때마다 깨지고,
    정작 "어떤 테이블이 빠졌나"는 안 알려 준다.
    """
    with open(AS.DEFAULT_SCHEMA_PATH, encoding="utf-8") as f:
        text = f.read()
    stmts = AS.split_statements(text)
    created = {t for t in ("sessions", "knowledge_chunks", "ontology_nodes",
                           "ontology_edges", "usage_turns", "usage_reports", "ops_events")
               if any(s.upper().startswith("CREATE TABLE") and t in s for s in stmts)}
    assert created == {"sessions", "knowledge_chunks", "ontology_nodes", "ontology_edges",
                       "usage_turns", "usage_reports", "ops_events"}
    assert any(s.upper().startswith("CREATE DATABASE") for s in stmts)
    assert any("usage_turns" in s for s in stmts)
    assert all(";" not in s for s in stmts)


def test_apply_schema_executes_every_statement_against_injected_conn():
    conn = _FakeSchemaConn()
    n = AS.apply_schema(conn=conn)
    # log 에는 DDL 뿐 아니라 ensure_columns 의 information_schema 조회도 남는다.
    ddl = [s for s in conn.log if "information_schema" not in s]
    assert n == len(ddl)
    assert any("usage_turns" in s for s in conn.log)
    # conn 을 주입한 경우 commit/close 는 apply_schema 가 아니라 호출측 책임
    assert conn.committed is False
    assert conn.closed is False


def test_apply_schema_is_idempotent_when_rerun():
    """재실행해도 완전히 같은 문 집합을 다시 보낸다 — 실제 MySQL에선 IF NOT EXISTS 라 무해."""
    conn1, conn2 = _FakeSchemaConn(), _FakeSchemaConn()
    AS.apply_schema(conn=conn1)
    AS.apply_schema(conn=conn2)
    assert conn1.log == conn2.log


def test_apply_schema_default_path_connects_commits_and_closes(monkeypatch):
    conn = _FakeSchemaConn()
    monkeypatch.setattr(AS, "_connect_no_database", lambda: conn)
    n = AS.apply_schema()
    ddl = [s for s in conn.log if "information_schema" not in s]
    assert n == len(ddl)
    assert conn.committed is True
    assert conn.closed is True


def test_apply_schema_rolls_back_and_closes_on_failure(monkeypatch):
    class _BoomCursor(_FakeSchemaCursor):
        def execute(self, sql, params=()):
            raise RuntimeError("boom")

    class _BoomConn(_FakeSchemaConn):
        def cursor(self):
            return _BoomCursor(self.log)

    conn = _BoomConn()
    monkeypatch.setattr(AS, "_connect_no_database", lambda: conn)
    with pytest.raises(RuntimeError):
        AS.apply_schema()
    assert conn.committed is False
    assert conn.closed is True


def test_main_cli_reports_applied_statement_count(monkeypatch, capsys):
    conn = _FakeSchemaConn()
    monkeypatch.setattr(AS, "_connect_no_database", lambda: conn)
    rc = AS.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "applied" in out
    ddl = [s for s in conn.log if "information_schema" not in s]
    assert str(len(ddl)) in out


# ──────────────────────────────────────────────────────────────────────────────
# 나중에 추가된 컬럼 — CREATE TABLE IF NOT EXISTS 로는 절대 안 생긴다
# ──────────────────────────────────────────────────────────────────────────────
#
# 2026-08-21 에 테이블 단위로 정확히 이 함정을 밟았다(usage_turns 부재 → 비용 데이터 0건).
# 컬럼 단위로 같은 일이 반복되지 않게 고정한다.

def test_ensure_columns_adds_missing_ones():
    """운영 DB 처럼 '테이블은 있는데 새 컬럼만 없는' 상태에서 ALTER 가 나가야 한다."""
    conn = _FakeSchemaConn(existing_columns=set())
    added = AS.ensure_columns(conn)
    # 등재된 컬럼이 **전부** ALTER 되는지를 본다. 목록을 하드코딩하면 컬럼을 더할 때마다
    # 깨지고, 정작 "등재했는데 ALTER 가 안 나갔다"는 진짜 버그는 못 잡는다.
    expected = {f"{t}.{c}" for t, c, _, _ in AS._ADD_COLUMNS}
    assert set(added) == expected
    alters = [s for s in conn.log if s.upper().startswith("ALTER TABLE")]
    assert len(alters) == len(expected)


def test_ensure_columns_is_idempotent():
    """이미 있으면 ALTER 를 내면 안 된다 — 매 배포마다 도는 코드다."""
    conn = _FakeSchemaConn()          # 기본 = 컬럼이 이미 다 있음
    assert AS.ensure_columns(conn) == []
    assert not [s for s in conn.log if s.upper().startswith("ALTER TABLE")]


def test_new_columns_also_in_create_table():
    """신규 설치 경로(CREATE TABLE)에도 같은 컬럼이 있어야 한다.

    ALTER 경로에만 있으면 새로 깐 DB 와 기존 DB 의 스키마가 갈라진다.
    """
    with open(AS.DEFAULT_SCHEMA_PATH, encoding="utf-8") as f:
        text = f.read()
    create = [s for s in AS.split_statements(text)
              if s.upper().startswith("CREATE TABLE") and "usage_turns" in s][0]
    assert "llm_mode" in create and "reuse_tier" in create


def test_schema_has_no_compound_statements():
    """★ split_statements 는 세미콜론으로 쪼갠다 — CREATE PROCEDURE 같은 복합문이
    들어오면 조용히 깨진 SQL 을 실행한다. 애초에 못 들어오게 막는다."""
    with open(AS.DEFAULT_SCHEMA_PATH, encoding="utf-8") as f:
        # 주석은 실행되지 않으므로 제외한다 — 왜 쓰면 안 되는지 설명하는 주석 자체가
        # 걸리면 문서를 못 남긴다.
        body = "\n".join(ln for ln in f.read().splitlines()
                          if not ln.lstrip().startswith("--")).upper()
    for banned in ("CREATE PROCEDURE", "CREATE FUNCTION", "CREATE TRIGGER", "DELIMITER"):
        assert banned not in body, f"{banned} 는 split_statements 가 다루지 못한다"
