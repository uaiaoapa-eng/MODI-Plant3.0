"""RAG 백엔드 모드(local sqlite | mysql_redis) 통합 모드 테스트.

- local  : 실제 로컬 자산으로 /api/query·/api/search 동작(해피패스).
- mysql  : MySQL 미가동 환경이므로 programmable fake 연결로 /api/query 가 MySQL 분기
           SQL(src_key/knowledge_chunks 등)을 실제로 타고 카드까지 조립되는지, 그리고
           DB 다운 시 검색이 graceful 하게 degrade 되는지 검증.
- /chat  : RAG 그래프와 무관(orch 전용)하므로 두 모드 어디서도 영향 없음을 확인.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    from fastapi.testclient import TestClient

    import ontology_lib
    import registry_lib
    import search_lib
    import server
    import store_mysql
except Exception as e:  # 의존성 미설치 환경에서는 스킵(다른 서버 테스트와 동일 정책)
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)

_DERIVE_Q = "카드 100개를 똑같이 만들고 싶어요"


# ---- programmable fake MySQL (rel 리터럴로 캔 응답 라우팅) ------------------

class _Cur:
    def __init__(self, log: list) -> None:
        self._log = log
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params) -> None:
        self._log.append((sql, params))
        if "'realized_by'" in sql:
            self._rows = [{"title": "카드 반복 만들기", "content": "loop 로 카드 100개",
                           "coding_type": "blockly", "session_id": "s1"}]
        elif "'relates_to'" in sql:
            self._rows = [{"key": "condition", "label": "조건", "weight": 1.0}]
        elif "'prerequisite'" in sql:
            self._rows = [{"key": "variable_use", "label": "변수 사용", "level": 0}]
        elif "'uses'" in sql:
            self._rows = [{"label": "dial"}]
        else:
            self._rows = []

    def fetchall(self) -> list:
        return list(self._rows)


class _Conn:
    _is_mysql = True

    def __init__(self) -> None:
        self.log: list = []

    def cursor(self) -> _Cur:
        return _Cur(self.log)

    def close(self) -> None:
        pass


def _set_mode(monkeypatch, mode: str, conn=None) -> None:
    monkeypatch.setattr(ontology_lib, "RAG_BACKEND", mode)
    monkeypatch.setattr(search_lib, "RAG_BACKEND", mode)
    if mode == "mysql_redis" and conn is not None:
        monkeypatch.setattr(store_mysql, "open_conn", lambda: conn)


@pytest.fixture
def client():
    registry_lib.reset()
    yield TestClient(server.app)
    registry_lib.reset()


class _FakeOrch:
    _user_id = ""

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}


# ---- local 모드 -----------------------------------------------------------

def test_query_local_mode(client, monkeypatch):
    _set_mode(monkeypatch, "local")
    d = client.get("/api/query", params={"question": _DERIVE_Q, "grade": 4}).json()
    assert d["ok"] is True
    assert d["primary"]["key"]
    assert isinstance(d["cards"], list)


def test_search_local_mode(client, monkeypatch):
    _set_mode(monkeypatch, "local")
    s = client.get("/api/search", params={"q": "좋아요 누르면 하트 빨개지게", "top": 3}).json()
    assert s["ok"] is True
    assert s["decision"] in ("reuse", "review", "register")


# ---- mysql_redis 모드 (programmable fake) ----------------------------------

def test_query_mysql_mode_uses_mysql_sql_and_assembles_cards(client, monkeypatch):
    conn = _Conn()
    _set_mode(monkeypatch, "mysql_redis", conn)
    d = client.get("/api/query", params={"question": _DERIVE_Q, "grade": 4}).json()
    assert d["ok"] is True
    # MySQL 원천 분기 SQL 이 실제 실행됨(sqlite 컬럼명이면 실패)
    joined = " || ".join(sql for sql, _ in conn.log)
    assert "src_key" in joined
    assert "knowledge_chunks" in joined
    # realized_by → 카드 조립까지 도달
    assert d["cards"] and d["cards"][0]["title"] == "카드 반복 만들기"
    assert d["modi_modules"] == ["dial"]


def test_mysql_mode_db_down_search_is_graceful(monkeypatch):
    """MySQL 미연결 시 _load_meta_from_db 는 예외를 삼키고 []를 반환(검색 폴백)."""
    _set_mode(monkeypatch, "mysql_redis")

    def boom():
        raise ConnectionError("mysql unreachable")

    monkeypatch.setattr(store_mysql, "open_conn", boom)
    assert search_lib._load_meta_from_db() == []


# ---- /chat 는 두 모드 모두 영향 없음 --------------------------------------

@pytest.mark.parametrize("mode", ["local", "mysql_redis"])
def test_chat_unaffected_by_rag_mode(monkeypatch, mode):
    _set_mode(monkeypatch, mode, _Conn())
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    client = TestClient(server.app)
    sid = f"busy-{mode}"
    assert server.session_locks.acquire(sid) is True  # 진행 중 상황 모사
    try:
        with client.stream("POST", "/chat", json={"session_id": sid, "message": "x"}) as resp:
            text = "".join(resp.iter_lines())
        assert "처리 중" in text  # busy 가드 정상(모드와 무관)
    finally:
        server.session_locks.release(sid)
