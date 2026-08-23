"""HTTP 에러 응답 구조화 테스트 — server.py (#132).

검증 범위:
- 전역 예외 핸들러: 미처리 예외가 통일 스키마({"ok":false,"error":{"code","message"}})로
  변환되고, 내부 예외 문자열이 응답에 실리지 않는다.
- load_project(404/500)·rag_ui(404)가 error_response() 스키마로 통일됐고,
  기존 프론트 호환을 위한 "error" 키가 그대로 남아있다(additive).
- /chat SSE 경로는 이 이슈에서 손대지 않았음을 스모크로 확인한다(#128/#131 영역 보존).
"""
import pytest

try:
    from fastapi.testclient import TestClient
    import server
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


class _FakeOrch:
    """토큰 2개 내고 끝나는 가짜 오케스트레이터(LLM 호출 없음)."""
    _user_id = ""

    def chat_stream(self, *a, **k):
        yield {"type": "token", "text": "hi"}
        yield {"type": "done"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_orchestrator", lambda sid, uid="": _FakeOrch())
    monkeypatch.setattr(server, "auto_save", lambda sid, orch: None)
    # FastAPI TestClient 는 기본적으로 서버 예외를 그대로 재발생시켜 테스트를 실패시킨다.
    # 전역 핸들러가 실제로 응답을 만드는지 보려면 raise_server_exceptions=False 가 필요.
    return TestClient(server.app, raise_server_exceptions=False)


def test_load_project_not_found_unified_schema(client):
    """존재하지 않는 프로젝트 로드 시 code 필드 + 기존 error 키 공존, 상태코드 404."""
    r = client.get("/projects/does-not-exist.json", params={"user_id": "no-such-user"})
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert "error" in body  # additive: 기존 error 키 유지
    assert body["error"]["code"] == "not_found"
    # 내부 예외 문자열/경로가 실리지 않는다
    assert "Errno" not in str(body) and "/home/" not in str(body) and "Traceback" not in str(body)


def test_load_project_corrupted_file_hides_exception_detail(client, tmp_path, monkeypatch):
    """손상된 세션 파일 → 500 + INTERNAL, 예외 문자열(str(e))은 응답에 없음."""
    monkeypatch.setattr(server, "_user_dir", lambda uid: str(tmp_path))
    bad_file = tmp_path / "broken.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    r = client.get("/projects/broken.json", params={"user_id": "u1"})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "internal"
    assert "detail" not in body["error"]  # INTERNAL 은 detail 항상 생략
    body_str = str(body)
    assert "broken.json" not in body_str or "Expecting" not in body_str
    assert "Expecting" not in body_str  # json.JSONDecodeError 메시지 비노출


def test_reference_instantiate_not_found_unified_schema(client):
    r = client.post("/reference/no-such-template/instantiate", params={"user_id": "u1"})
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_restore_session_not_found_unified_schema(client):
    r = client.post("/session/no-such-session/restore", params={"user_id": "u1"})
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_global_handler_hides_internal_exception_string(client):
    """엔드포인트에서 미처리 예외가 터지면 전역 핸들러가 통일 스키마로 감싼다.

    예외 메시지(파일 경로 등 내부 정보)는 응답에 실리지 않는다.
    """
    server._reference_cache.invalidate("list")

    def _boom():
        raise RuntimeError("/etc/secret/internal-path leaked")

    server_module_fn = server._read_reference_list
    try:
        server._read_reference_list = _boom  # type: ignore[assignment]
        r = client.get("/reference")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "internal"
        assert "leaked" not in str(body)
        assert "/etc/secret" not in str(body)
    finally:
        server._read_reference_list = server_module_fn
        server._reference_cache.invalidate("list")


def test_rag_ui_missing_file_unified_not_found(client, monkeypatch):
    """RAG UI 로컬 파일도 없고 업스트림도 없을 때 404 + 통일 스키마(#132 code 앵커 1092)."""
    if not hasattr(server, "rag_ui"):
        pytest.skip("RAG 라우트 미등록 환경(로컬 모듈 없음)")
    real_exists = server.os.path.exists
    monkeypatch.setattr(
        server.os.path, "exists",
        lambda p: False if str(p).endswith("rag_demo.html") else real_exists(p))
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "")

    r = client.get("/rag")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_chat_sse_smoke_unaffected_by_error_refactor(client):
    """#132 변경이 /chat SSE 경로(#128/#131 영역)에 영향 없는지 스모크."""
    with client.stream("POST", "/chat", json={"session_id": "smoke-1", "message": "hi"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_lines())
    assert "hi" in text
    assert '"type": "done"' in text or '"type":"done"' in text
