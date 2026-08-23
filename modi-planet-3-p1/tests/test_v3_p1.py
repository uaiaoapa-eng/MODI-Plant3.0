"""Focused P1 product-shell and Create adapter tests (no LLM calls)."""
from pathlib import Path

import pytest

try:
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    import server
except Exception as exc:  # pragma: no cover - dependency-less test environments
    pytest.skip(f"server import 불가(의존성 미설치): {exc}", allow_module_level=True)

from agent.create import CreateOrchestratorAdapter, SUPPORTED_CODING_TYPES
from agent.session_store import InMemorySessionStore
from curriculum import list_grade_bands


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "create_sessions", InMemorySessionStore())
    return TestClient(server.app)


@pytest.mark.parametrize("coding_type", ["react", "blockly", "hybrid"])
def test_create_adapter_forces_design_and_keeps_existing_coding_types(coding_type):
    adapter = CreateOrchestratorAdapter.start(coding_type)
    payload = adapter.legacy_chat_payload("만들자", runtime_error="preview failed")

    assert payload == {
        "session_id": adapter.session_id,
        "message": "만들자",
        "mode": "design",
        "coding_type": coding_type,
        "runtime_error": "preview failed",
    }


@pytest.mark.parametrize("coding_type", ["quick", "web", "hardware", "REACT", ""])
def test_create_adapter_rejects_new_or_legacy_aliases(coding_type):
    with pytest.raises(ValueError, match="coding_type"):
        CreateOrchestratorAdapter.start(coding_type)


def test_runtime_neutral_catalog_is_shell_only():
    bands = list_grade_bands()

    assert [band["id"] for band in bands] == ["elementary", "middle", "high"]
    for band in bands:
        assert band["lesson_count"] == 9
        assert len(band["lessons"]) == 9
        assert [lesson["lesson_no"] for lesson in band["lessons"]] == list(range(1, 10))
        assert all(set(lesson) == {"id", "lesson_no", "status"} for lesson in band["lessons"])
        assert all(lesson["status"] == "placeholder" for lesson in band["lessons"])


def test_home_exposes_only_learn_and_create(client):
    response = client.get("/api/v3/home")

    assert response.status_code == 200
    assert response.json()["product"] == {"name": "MODI Planet", "version": "3.0"}
    assert [mode["id"] for mode in response.json()["modes"]] == ["learn", "create"]
    assert "quick" not in response.text.lower()


def test_curriculum_routes_return_three_bands_and_nine_slots(client):
    all_bands = client.get("/api/v3/curriculum")
    elementary = client.get("/api/v3/curriculum/elementary")

    assert all_bands.status_code == 200
    assert len(all_bands.json()["grade_bands"]) == 3
    assert elementary.status_code == 200
    assert elementary.json()["label"] == "초등"
    assert len(elementary.json()["lessons"]) == 9
    assert client.get("/api/v3/curriculum/college").status_code == 404


@pytest.mark.parametrize("coding_type", sorted(SUPPORTED_CODING_TYPES))
def test_create_session_accepts_only_existing_coding_types(client, coding_type):
    response = client.post("/api/v3/create/sessions", json={"coding_type": coding_type})

    assert response.status_code == 201
    body = response.json()
    assert body["session_id"]
    assert body["mode"] == "design"
    assert body["coding_type"] == coding_type
    assert server.create_sessions.get(body["session_id"]).coding_type == coding_type


def test_create_session_rejects_unknown_coding_type(client):
    response = client.post("/api/v3/create/sessions", json={"coding_type": "web"})

    assert response.status_code == 422
    assert "react" in response.json()["detail"]


def test_create_chat_delegates_to_legacy_chat_with_forced_mode(client, monkeypatch):
    created = client.post(
        "/api/v3/create/sessions", json={"coding_type": "hybrid"}
    ).json()
    captured = {}

    async def fake_legacy_chat(*, req, request, user_id):
        captured.update({"request": req, "user_id": user_id, "path": request.url.path})
        return JSONResponse({"delegated": True})

    monkeypatch.setattr(server, "chat", fake_legacy_chat)
    response = client.post(
        created["chat_endpoint"],
        params={"user_id": "student-1"},
        json={
            "message": "센서와 웹을 연결하고 싶어",
            "runtime_error": "boom",
            "mode": "quick",
            "coding_type": "react",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"delegated": True}
    assert captured["request"].session_id == created["session_id"]
    assert captured["request"].mode == "design"
    assert captured["request"].coding_type == "hybrid"
    assert captured["request"].runtime_error == "boom"
    assert captured["user_id"] == "student-1"


def test_create_chat_rejects_unknown_session_without_calling_chat(client, monkeypatch):
    async def should_not_run(**kwargs):
        raise AssertionError("legacy chat must not run")

    monkeypatch.setattr(server, "chat", should_not_run)
    response = client.post(
        "/api/v3/create/sessions/missing/chat", json={"message": "hello"}
    )

    assert response.status_code == 404


def test_product_index_and_static_mount_are_wired(client):
    response = client.get("/")
    static_mount = next(route for route in server.app.routes if route.path == "/static")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MODI Planet" in response.text
    assert Path(static_mount.app.directory).resolve() == Path(server._WEB_DIR).resolve()
