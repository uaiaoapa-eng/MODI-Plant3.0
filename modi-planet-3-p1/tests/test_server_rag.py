"""server.py 통합 RAG 엔드포인트 wiring 테스트 (TestClient).

/api/search · /api/register · /api/coverage · /api/query · /rag/health 가
메인 서버에서 user_id(uuid) 인증을 공유하며 동작하는지 검증.
의존성(langfuse 등) 미설치 환경에서는 모듈 스킵(test_server_endpoints 와 동일 정책).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    from fastapi.testclient import TestClient

    import registry_lib
    import server
except Exception as e:
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


@pytest.fixture
def client():
    registry_lib.reset()
    yield TestClient(server.app)
    registry_lib.reset()


def test_rag_health(client):
    b = client.get("/rag/health").json()
    assert b["status"] == "ok"
    assert "vector_enabled" in b and "registered" in b


def test_rag_ui_served(client):
    r = client.get("/rag")
    assert r.status_code == 200
    assert "검색" in r.text  # 통합 UI 서빙


def test_search_and_coverage(client):
    s = client.get("/api/search", params={"q": "좋아요 누르면 하트 빨개지게", "top": 3}).json()
    assert s["ok"] is True
    assert s["decision"] in ("reuse", "review", "register")
    cov = client.get("/api/coverage").json()
    assert abs(sum(cov["percent"].values()) - 100.0) < 0.01


def test_register_writeback_and_user_scope(client):
    q = "완전 새로운 오로라 감지 센서 장치"
    assert client.get("/api/search", params={"q": q, "top": 1}).json()["decision"] == "register"
    # uuid 인증(query user_id)으로 등록
    reg = client.post("/api/register", params={"user_id": "uX"},
                      json={"question": q, "title": "오로라 감지", "content": "자기장·광량 융합 추정"}).json()
    assert reg["ok"] is True
    # 본인은 등록물 노출, 남은 미노출
    mine = client.get("/api/search", params={"q": q, "top": 2, "user_id": "uX"}).json()
    other = client.get("/api/search", params={"q": q, "top": 2, "user_id": "uY"}).json()
    assert any(r["source"] == "registered" for r in mine["results"])
    assert all(r["source"] == "base" for r in other["results"])


def test_query_derive(client):
    d = client.get("/api/query", params={"question": "카드 100개를 똑같이 만들고 싶어요", "grade": 4}).json()
    assert d["ok"] is True
    assert "primary" in d


def test_registry_stats_local_empty(client):
    b = client.get("/api/registry/stats").json()
    assert b == {"ok": True, "count": 0, "last_registered_at": None,
                 "backend": "local", "upstream": False}


def test_registry_stats_local_after_register(client):
    client.post("/api/register", params={"user_id": "uX"},
               json={"question": "q", "title": "t", "content": "c"})
    b = client.get("/api/registry/stats").json()
    assert b["ok"] is True
    assert b["count"] == 1
    assert b["upstream"] is False


def test_registry_stats_proxy_upstream_failure_falls_back_local(client, monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")
    monkeypatch.setattr(server, "_rag_registry_stats_upstream", lambda: None)
    r = client.get("/api/registry/stats")
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert b["count"] == 0
    assert b["upstream"] is True
