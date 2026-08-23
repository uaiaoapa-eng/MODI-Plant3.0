"""rag_demo_app 검색/커버리지 엔드포인트 wiring 테스트 (TestClient).

LLM/네트워크 없이 /api/search · /api/coverage · /health 응답 스키마만 검증.
데이터 자산이나 의존성이 없으면 모듈 스킵.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    from fastapi.testclient import TestClient

    import rag_demo_app as app_mod
except Exception as e:
    pytest.skip(f"rag_demo_app import 불가(의존성/자산 미설치): {e}", allow_module_level=True)


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def test_health_reports_vector_flag(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "vector_enabled" in body


def test_search_endpoint_schema(client):
    r = client.get("/api/search", params={"q": "로봇이 앞에 장애물 있으면 알아서 서게", "top": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["decision"] in ("reuse", "review", "register")
    assert len(d["results"]) <= 5
    for x in d["results"]:
        assert {"title", "score", "decision", "concept_key"} <= set(x)


def test_search_empty_query(client):
    r = client.get("/api/search", params={"q": "  "})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_coverage_endpoint_schema(client):
    r = client.get("/api/coverage")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["total"] == len(app_mod.DEFAULT_COVERAGE_QUERIES)
    s = d["percent"]["reuse"] + d["percent"]["review"] + d["percent"]["register"]
    assert abs(s - 100.0) < 0.01


# ── /api/writeback (#58): chat 빌드 결과 되먹임 ──

def test_writeback_dispatches_to_registry(client, monkeypatch):
    """묶음을 받아 register_learning_notes + register_result 로 위임하고 건수를 합산한다."""
    calls = {}

    def fake_notes(session_id, user_id, coding_type, notes, modi_keys=None):
        calls["notes"] = dict(session_id=session_id, user_id=user_id,
                              coding_type=coding_type, modi_keys=modi_keys, n=len(notes))
        return len(notes)

    def fake_result(session_id, user_id, coding_type, *, design_doc=None,
                    code_map=None, modi_keys=None, goal=None, learning_notes=None):
        calls["result"] = dict(design_doc=design_doc, code_map=code_map, goal=goal,
                               learning_notes=learning_notes)
        return 2

    monkeypatch.setattr(app_mod.registry_lib, "register_learning_notes", fake_notes)
    monkeypatch.setattr(app_mod.registry_lib, "register_result", fake_result)

    r = client.post("/api/writeback", json={
        "session_id": "s1", "user_id": "u1", "coding_type": "react",
        "learning_notes": [{"title": "루프", "body": "for 문"}],
        "design_doc": {"project_name": "카드앱", "features": [{"name": "카드"}]},
        "code_map": {"app.js": "console.log(1)"},
        "goal": "카드 100개",
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "notes_added": 1, "result_added": 2}
    assert calls["notes"]["session_id"] == "s1"
    assert calls["result"]["goal"] == "카드 100개"
    assert calls["result"]["code_map"] == {"app.js": "console.log(1)"}
    # EDU-27 직접서브 문서복원: writeback 이 학습노트도 register_result 로 전달해야
    # code payload 에 동봉(chunk_fields.docs_payload)될 수 있다.
    assert calls["result"]["learning_notes"] == [{"title": "루프", "body": "for 문"}]


def test_writeback_skips_result_without_design_or_code(client, monkeypatch):
    """설계문서·코드가 없으면 register_result 는 호출하지 않는다(노트만 등록)."""
    monkeypatch.setattr(app_mod.registry_lib, "register_learning_notes", lambda *a, **k: 0)
    called = {"result": False}

    def fake_result(*a, **k):
        called["result"] = True
        return 0

    monkeypatch.setattr(app_mod.registry_lib, "register_result", fake_result)
    r = client.post("/api/writeback", json={"session_id": "s1", "learning_notes": []})
    assert r.status_code == 200
    assert r.json()["result_added"] == 0
    assert called["result"] is False


# ── /api/registry/stats (#115): 메인 앱(:18080) 위임 대상 ──

def test_registry_stats_reports_backend(client, monkeypatch):
    """backend 를 그대로 노출해 앱이 프록시를 local 로 오인하지 않게 한다(#115)."""
    monkeypatch.setattr(app_mod.registry_lib, "stats", lambda: {
        "count": 7, "last_registered_at": None, "backend": "local"})
    r = client.get("/api/registry/stats")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["backend"] == "local"
    assert d["count"] == 7            # local 은 파일 stats count 그대로


def _fake_vector_redis(client_factory):
    """엔드포인트의 지연 `import vector_redis` 가 집어들 가짜 모듈(redis 미설치 무관)."""
    import types

    mod = types.ModuleType("vector_redis")
    mod.PREFIX = "kchunk:"
    mod.client = client_factory
    return mod


def test_registry_stats_mysql_redis_counts_redis_registered(client, monkeypatch):
    """mysql_redis 면 count 를 Redis 등록 문서(kchunk:reg:*)에서 센다 — 파일 count 무시."""
    monkeypatch.setattr(app_mod.registry_lib, "stats", lambda: {
        "count": 999, "last_registered_at": None, "backend": "mysql_redis"})

    class _FakeClient:
        def scan_iter(self, match=None):
            assert match == "kchunk:reg:*"
            yield from ("kchunk:reg:1", "kchunk:reg:2", "kchunk:reg:3")

    monkeypatch.setitem(sys.modules, "vector_redis", _fake_vector_redis(lambda: _FakeClient()))
    r = client.get("/api/registry/stats")
    assert r.status_code == 200
    d = r.json()
    assert d["backend"] == "mysql_redis"
    assert d["count"] == 3            # 파일 stats 의 999 가 아니라 Redis 실측


def test_registry_stats_redis_down_falls_back_to_file(client, monkeypatch):
    """Redis 조회 실패 시 500 대신 파일 stats 로 폴백(가용성 우선, 앱 위임과 동형)."""
    monkeypatch.setattr(app_mod.registry_lib, "stats", lambda: {
        "count": 42, "last_registered_at": None, "backend": "mysql_redis"})

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setitem(sys.modules, "vector_redis", _fake_vector_redis(_boom))
    r = client.get("/api/registry/stats")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 42           # 폴백: 파일 stats count 유지


# ── /api/chunks (EDU-27 직접서브 문서복원 — 세션 조인 폴백 최소 엔드포인트) ──

class _FakeChunksConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_chunks_endpoint_prefers_mysql(client, monkeypatch):
    """MySQL 조회가 성공하면 그 결과를 그대로 반환(registry_lib 폴백 미사용)."""
    class _FakeMysql:
        @staticmethod
        def connect():
            return _FakeChunksConn()

        @staticmethod
        def get_chunks_by_session(conn, session_id, kinds):
            assert session_id == "s1" and kinds == ["learning_note", "design_doc"]
            return [{"chunk_type": "learning_note", "title": "T", "content": "C",
                    "payload": {"what": "w"}}]

    monkeypatch.setitem(sys.modules, "store_mysql", _FakeMysql)
    registry_called = {"v": False}
    monkeypatch.setattr(app_mod.registry_lib, "get_by_session",
                        lambda *a, **k: registry_called.__setitem__("v", True) or [])

    r = client.get("/api/chunks", params={"session_id": "s1", "kind": "learning_note,design_doc"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["chunks"] == [{"chunk_type": "learning_note", "title": "T", "content": "C",
                           "payload": {"what": "w"}}]
    assert registry_called["v"] is False


def test_chunks_endpoint_falls_back_to_registry_on_mysql_failure(client, monkeypatch):
    """MySQL 조회가 실패/빈 결과면 registry_lib(인프로세스 스토어)로 폴백한다."""
    class _FakeMysqlBroken:
        @staticmethod
        def connect():
            raise RuntimeError("no mysql in this env")

    monkeypatch.setitem(sys.modules, "store_mysql", _FakeMysqlBroken)
    monkeypatch.setattr(
        app_mod.registry_lib, "get_by_session",
        lambda session_id, kinds: [{"chunk_type": "design_doc", "title": "D", "content": "",
                                    "payload": {"project_name": "P"}}],
    )
    r = client.get("/api/chunks", params={"session_id": "s2"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["chunks"][0]["payload"]["project_name"] == "P"


def test_chunks_endpoint_no_hit_returns_empty_list(client, monkeypatch):
    class _FakeMysqlBroken:
        @staticmethod
        def connect():
            raise RuntimeError("no mysql in this env")

    monkeypatch.setitem(sys.modules, "store_mysql", _FakeMysqlBroken)
    monkeypatch.setattr(app_mod.registry_lib, "get_by_session", lambda *a, **k: [])
    r = client.get("/api/chunks", params={"session_id": "unknown"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "chunks": []}
