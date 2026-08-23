"""턴 중간 체크포인트 회귀 테스트 — 서버가 턴 도중 죽어도 산출물이 남는가.

가설(2026-08-21 실측으로 확인):
    `auto_save` 는 `/chat` 의 `finally` 에서만 돈다. 그래서 턴이 끝나기 전에 프로세스가
    죽으면(OOM·SIGKILL) **그 턴에서 만든 코드가 통째로 유실**된다. 재현 결과:

        턴 진행 중: 코드 1개, blockly 17자
        💥 프로세스 사망(finally 미실행)
        복원 후:   코드 0개, blockly 0자   ← 파일 자체가 없음

    노출 창이 크다 — 실제 생성 턴이 94~160초인데, 산출물이 확정된 뒤에도 학습노트·
    흐름도 같은 후처리가 한참 이어지기 때문이다. 그 구간에서 죽으면 학생은 90초를
    기다리고 아무것도 못 얻는다.

해결:
    산출물 확정 신호(`blockly_ready` / `code_validated`)가 스트림에 나온 즉시 디스크에
    확정한다. 그 뒤 죽어도 결과가 남고, 다음 요청이 복원해 이어간다.
"""
import json
import pytest

try:
    import server
    from agent.session_store import InMemorySessionStore
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가: {e}", allow_module_level=True)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SAVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "sessions", InMemorySessionStore(), raising=False)
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "", raising=False)
    return tmp_path


def _mid_turn_orch(session_id="s1", user="userA"):
    """산출물까지 만들었지만 아직 턴이 안 끝난 상태의 오케스트레이터."""
    orch = server.get_orchestrator(session_id, user)
    orch.state._messages.append({"role": "user", "content": "LED 깜빡이 만들어줘"})
    orch.state.generated_code_map = {"App.tsx": "90초 걸려 만든 코드"}
    orch.state.blockly_xml = "<xml>비싼 산출물</xml>"
    return orch


# ──────────────────────────────────────────────────────────────────────────────
# 가설 재현 — 체크포인트가 없으면 유실된다
# ──────────────────────────────────────────────────────────────────────────────

def test_without_checkpoint_artifacts_are_lost(iso):
    """체크포인트를 부르지 않으면 산출물이 사라진다(= 고치기 전 동작)."""
    _mid_turn_orch()
    server.sessions = InMemorySessionStore()          # 💥 프로세스 사망

    revived = server.get_orchestrator("s1", "userA")
    assert revived.state.generated_code_map == {}
    assert revived.state.blockly_xml == ""


# ──────────────────────────────────────────────────────────────────────────────
# 수정 검증 — 체크포인트가 산출물을 살린다
# ──────────────────────────────────────────────────────────────────────────────

def test_checkpoint_preserves_artifacts_across_crash(iso):
    """★ 핵심: 체크포인트 후 죽어도 코드·블록이 복원돼야 한다."""
    orch = _mid_turn_orch()
    server.checkpoint_save("s1", orch)                # 산출물 확정 시점
    server.sessions = InMemorySessionStore()          # 💥 프로세스 사망

    revived = server.get_orchestrator("s1", "userA")
    assert revived.state.generated_code_map == {"App.tsx": "90초 걸려 만든 코드"}
    assert revived.state.blockly_xml == "<xml>비싼 산출물</xml>"


def test_checkpoint_preserves_conversation_too(iso):
    """대화 내역도 함께 남아야 학생이 '이어서' 할 수 있다."""
    orch = _mid_turn_orch()
    server.checkpoint_save("s1", orch)
    server.sessions = InMemorySessionStore()

    revived = server.get_orchestrator("s1", "userA")
    texts = [m["content"] for m in revived.state._messages if m["role"] == "user"]
    assert "LED 깜빡이 만들어줘" in texts


def test_checkpoint_noop_before_any_artifact(iso):
    """산출물이 없으면(되묻기 턴 등) 파일을 만들지 않는다 — 빈 세션 파일 양산 방지."""
    orch = server.get_orchestrator("s2", "userA")
    orch.state._messages.append({"role": "user", "content": "안녕"})
    server.checkpoint_save("s2", orch)
    assert not (iso / "userA" / "s2.json").exists()


def test_checkpoint_failure_never_raises(iso, monkeypatch):
    """★ 체크포인트가 실패해도 예외가 새어 스트림을 끊으면 안 된다."""
    orch = _mid_turn_orch()

    def _boom(*a, **k):
        raise OSError("디스크 꽉 참")

    monkeypatch.setattr(server, "_write_session_json", _boom)
    server.checkpoint_save("s1", orch)   # 예외가 나가면 이 줄에서 테스트가 깨진다


def test_checkpoint_updates_mtime_to_avoid_needless_reload(iso):
    """저장 직후 적재 시점을 갱신해, 같은 레플리카가 곧바로 재로딩하지 않게 한다."""
    orch = _mid_turn_orch()
    server.checkpoint_save("s1", orch)
    assert orch._loaded_mtime > 0
    assert server.get_orchestrator("s1", "userA") is orch, "불필요한 재로딩이 일어났다"


def test_checkpoint_does_not_call_rag_writeback(iso, monkeypatch):
    """핫 경로라 되먹임·MySQL 왕복은 하지 않는다(턴 종료 시 auto_save 가 마무리)."""
    called = []
    monkeypatch.setattr(server, "_rag_feedback", lambda *a, **k: called.append("rag"))
    monkeypatch.setattr(server, "_session_writeback_upstream",
                        lambda *a, **k: called.append("mysql"))
    server.checkpoint_save("s1", _mid_turn_orch())
    assert called == [], f"체크포인트가 네트워크 왕복을 했다: {called}"


# ──────────────────────────────────────────────────────────────────────────────
# 배선 — 어떤 이벤트에서 체크포인트가 걸리나
# ──────────────────────────────────────────────────────────────────────────────

def test_checkpoint_events_cover_both_output_types():
    """blockly(하드웨어)·react(웹) 양쪽 산출물 신호를 모두 덮어야 한다."""
    assert "blockly_ready" in server._CHECKPOINT_EVENTS
    assert "code_validated" in server._CHECKPOINT_EVENTS


def test_checkpoint_not_triggered_by_chatty_events():
    """token/status 같은 고빈도 이벤트마다 디스크를 때리면 스트림이 느려진다."""
    for noisy in ("token", "status", "agent_step", "agent_step_update", "done"):
        assert noisy not in server._CHECKPOINT_EVENTS


def test_saved_file_is_valid_json_with_artifacts(iso):
    """복원 경로가 읽을 수 있는 형식이어야 한다(깨진 JSON 이면 격리되고 유실)."""
    orch = _mid_turn_orch()
    server.checkpoint_save("s1", orch)
    data = json.loads((iso / "userA" / "s1.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "s1"
    assert data["generated_code"] == {"App.tsx": "90초 걸려 만든 코드"}
    assert data["blockly_xml"] == "<xml>비싼 산출물</xml>"
