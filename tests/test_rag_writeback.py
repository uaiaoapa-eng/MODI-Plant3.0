"""server._rag_feedback 의 프록시/로컬 분기 테스트 (#58).

프록시 모드(RAG_UPSTREAM 설정)에서는 빌드 결과를 rag-search /api/writeback 으로
POST 하고 registry_lib 를 인프로세스로 부르지 않아야 한다(메인앱엔 torch 없음).
로컬 모드에서는 기존처럼 registry_lib 를 직접 호출한다.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵(다른 서버 테스트와 동일 정책)
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


class _Doc:
    """design_doc 스텁 — .features/.pages 로 실질 유무 판정, model_dump() 로 직렬화."""

    def __init__(self, features=None, pages=None, data=None):
        self.features = features or []
        self.pages = pages or []
        self._data = data or {}

    def model_dump(self):
        return dict(self._data)


def _make_state(*, design_doc, code_map=None, notes=None, coding_type="react",
                title="", modi_modules=None):
    return types.SimpleNamespace(
        modi_modules=modi_modules,
        project=types.SimpleNamespace(design_doc=design_doc),
        generated_code_map=code_map or {},
        coding_type=coding_type,
        learning_notes=notes or [],
        title=title,
    )


def test_rag_feedback_proxy_posts_writeback(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")
    sent = {}
    monkeypatch.setattr(server, "_rag_writeback_upstream",
                        lambda bundle: sent.update(bundle=bundle))

    doc = _Doc(features=[{"name": "카드"}],
               data={"project_name": "카드앱", "description": "카드 100개",
                     "features": [{"name": "카드"}]})
    state = _make_state(design_doc=doc, code_map={"app.js": "x"},
                        notes=[{"title": "t", "body": "b"}], coding_type="react")

    server._rag_feedback("s1", "u1", state)

    b = sent["bundle"]
    assert b["session_id"] == "s1" and b["user_id"] == "u1"
    assert b["coding_type"] == "react"
    assert b["design_doc"]["project_name"] == "카드앱"
    assert b["code_map"] == {"app.js": "x"}
    assert b["goal"] == "카드 100개"  # design_doc.description 우선
    assert b["learning_notes"] == [{"title": "t", "body": "b"}]


def test_rag_feedback_local_calls_registry(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "")
    import registry_lib

    calls = []
    monkeypatch.setattr(registry_lib, "register_learning_notes",
                        lambda *a, **k: calls.append("notes") or 0)
    monkeypatch.setattr(registry_lib, "register_result",
                        lambda *a, **k: calls.append("result") or 0)
    # 로컬 모드면 upstream 헬퍼는 절대 안 불려야 한다.
    monkeypatch.setattr(server, "_rag_writeback_upstream",
                        lambda bundle: calls.append("UPSTREAM"))

    doc = _Doc(features=[{"name": "f"}], data={"project_name": "P", "features": [{"name": "f"}]})
    state = _make_state(design_doc=doc, code_map={"a.js": "x"}, notes=[{"title": "t", "body": "b"}])

    server._rag_feedback("s1", "u1", state)

    assert "notes" in calls and "result" in calls
    assert "UPSTREAM" not in calls


def test_rag_feedback_proxy_swallows_errors(monkeypatch):
    """되먹임 실패가 상위 저장 경로를 깨지 않게 예외를 삼킨다."""
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")

    def _boom(bundle):
        raise RuntimeError("network down")

    monkeypatch.setattr(server, "_rag_writeback_upstream", _boom)
    doc = _Doc(features=[{"name": "f"}], data={"project_name": "P", "features": [{"name": "f"}]})
    state = _make_state(design_doc=doc, notes=[{"title": "t", "body": "b"}])

    # 예외를 던지지 않아야 한다.
    server._rag_feedback("s1", "u1", state)


class _FakeClient:
    """httpx.Client 스텁 — with 블록에서 get()만 흉내(#103 stats 위임 테스트)."""

    def __init__(self, get):
        self._get = get

    def __call__(self, timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self._get(url)


def test_registry_stats_upstream_success(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")
    body = {"ok": True, "count": 3, "last_registered_at": "2026-07-09T00:00:00Z",
           "backend": "mysql_redis", "upstream": True}

    def _get(url):
        assert url == "http://rag:8100/api/registry/stats"
        return types.SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(server.httpx, "Client", _FakeClient(_get))
    assert server._rag_registry_stats_upstream() == body


def test_registry_stats_upstream_failure_returns_none(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")

    def _boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(server.httpx, "Client", _FakeClient(_boom))
    assert server._rag_registry_stats_upstream() is None


class _FakeLF:
    """Langfuse 클라이언트 스텁 — score_current_trace 호출만 기록(#104)."""

    def __init__(self):
        self.scores = []

    def score_current_trace(self, name, value, data_type=None, **kw):
        self.scores.append({"name": name, "value": value, "data_type": data_type})


def test_rag_feedback_scores_register_ok_on_success(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")
    monkeypatch.setattr(server, "_rag_writeback_upstream", lambda bundle: None)
    fake = _FakeLF()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    doc = _Doc(features=[{"name": "f"}], data={"project_name": "P", "features": [{"name": "f"}]})
    server._rag_feedback("s1", "u1", _make_state(design_doc=doc, notes=[{"title": "t", "body": "b"}]))

    by = {s["name"]: s for s in fake.scores}
    assert by["등록 성공 (register_ok)"]["value"] == 1
    assert by["등록 성공 (register_ok)"]["data_type"] == "BOOLEAN"
    assert "등록 스킵사유 (register_skip_reason)" not in by  # 성공 시 사유 미기록


def test_rag_feedback_scores_skip_reason_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag:8100")

    def _boom(bundle):
        raise RuntimeError("network down")

    monkeypatch.setattr(server, "_rag_writeback_upstream", _boom)
    fake = _FakeLF()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    doc = _Doc(features=[{"name": "f"}], data={"project_name": "P", "features": [{"name": "f"}]})
    # 예외를 전파하지 않아야 한다(상위 저장 경로 보호).
    server._rag_feedback("s1", "u1", _make_state(design_doc=doc, notes=[{"title": "t", "body": "b"}]))

    by = {s["name"]: s for s in fake.scores}
    assert by["등록 성공 (register_ok)"]["value"] == 0
    assert by["등록 스킵사유 (register_skip_reason)"]["value"] == "RuntimeError"
    assert by["등록 스킵사유 (register_skip_reason)"]["data_type"] == "CATEGORICAL"
