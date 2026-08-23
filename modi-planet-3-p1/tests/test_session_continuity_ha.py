"""서버가 죽거나 레플리카가 바뀌어도 대화 context 가 이어지는지 고정하는 회귀 테스트.

배경 (이중화 3 레플리카 도입):
  edu-agent 를 edu-agent-{1,2,3} 3 레플리카로 띄우고 nginx sticky(user_id 해시)로
  붙인다. 이때 "서버가 죽어도 다시 살면 이전 대화를 이어간다" 가 성립하려면 두 가지가
  동시에 참이어야 한다.

    (A) 프로세스 재시작 — 인메모리 `sessions` 가 통째로 사라져도, 다음 요청이
        디스크 파일에서 **대화 내역까지** 복원한다.
    (B) 레플리카 교차 — 레플리카 A 가 저장한 턴을, 이미 옛 상태를 캐시하고 있는
        레플리카 B 가 이어받는다(공유 볼륨 + mtime 무효화).

기존 테스트는 phase/task_plan 보존(test_session_restore_phase.py)과 should_reload 의
순수 로직(test_session_store.py)만 덮었고, **대화 내역(messages) 왕복**과 **캐시
무효화가 실제 get_orchestrator 경로에서 동작하는지**는 비어 있었다. 이 파일이 그 구멍을
막는다. LLM/토큰을 쓰지 않는다.
"""
import json
import os

import pytest

try:
    import server
    from agent.models import Phase
    from agent.session_store import InMemorySessionStore, should_reload
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _orch(session_id="s1"):
    return server.StreamOrchestrator(api_key="", session_id=session_id)


def _talk(orch, pairs):
    """대화 몇 턴을 state 에 심는다. (user 발화, assistant 응답) 목록."""
    for user_text, ai_text in pairs:
        orch.state._messages.append({"role": "user", "content": user_text})
        orch.state._messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": ai_text}]}
        )


def _texts(orch):
    """복원 결과를 비교하기 쉽게 (role, 텍스트) 목록으로 납작하게 만든다."""
    out = []
    for m in orch.state._messages:
        c = m["content"]
        if isinstance(c, str):
            out.append((m["role"], c))
        else:
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    out.append((m["role"], blk["text"]))
    return out


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    """server 의 전역 세션 캐시·저장 경로를 테스트용으로 갈아끼운다.

    monkeypatch 라 테스트가 끝나면 원복된다(다른 테스트 오염 방지).
    """
    monkeypatch.setattr(server, "SAVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "sessions", InMemorySessionStore(), raising=False)
    # 프록시(MySQL 하이드레이트) 경로를 타지 않게 — 이 테스트는 '디스크' 연속성을 본다.
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "", raising=False)
    return tmp_path


# ──────────────────────────────────────────────────────────────────────────────
# (A) 프로세스 재시작 — 인메모리 캐시가 날아가도 대화가 이어진다
# ──────────────────────────────────────────────────────────────────────────────

def test_messages_survive_save_restore_roundtrip(tmp_path):
    """대화 내역이 저장→복원 왕복에서 순서·역할·내용 그대로 살아남아야 한다.

    이게 깨지면 서버 재시작 후 학생이 "방금 말한 거 이어서" 를 할 수 없다.
    """
    orch = _orch()
    _talk(orch, [
        ("빨간 LED 깜빡이게 만들어줘", "몇 초 간격으로 깜빡이면 좋을까요?"),
        ("1초", "1초 간격으로 만들었어요."),
    ])

    data = server._build_save_data("s1", orch)
    f = tmp_path / "s1.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    restored = _orch()
    server._restore_state_from_file(restored, str(f))

    assert _texts(restored) == [
        ("user", "빨간 LED 깜빡이게 만들어줘"),
        ("assistant", "몇 초 간격으로 깜빡이면 좋을까요?"),
        ("user", "1초"),
        ("assistant", "1초 간격으로 만들었어요."),
    ]


def test_context_continues_after_process_restart(isolated_sessions):
    """프로세스 재시작 모사: 인메모리 세션을 통째로 비워도 대화가 복원돼야 한다.

    get_orchestrator 는 캐시 미스면 디스크에서 복원한다. 재시작 = 캐시가 빈 상태이므로
    새 InMemorySessionStore 로 갈아끼우는 것으로 정확히 모사된다.
    """
    orch = server.get_orchestrator("s1", "userA")
    _talk(orch, [("초음파로 거리 재는 거 만들어줘", "거리를 어디에 표시할까요?")])
    orch.state.project.phase = Phase.IMPLEMENT
    orch.state.generated_code_map = {"App.tsx": "x"}
    server.auto_save("s1", orch)

    # ── 여기서 프로세스가 죽는다 ──
    server.sessions = InMemorySessionStore()

    revived = server.get_orchestrator("s1", "userA")

    assert _texts(revived) == [
        ("user", "초음파로 거리 재는 거 만들어줘"),
        ("assistant", "거리를 어디에 표시할까요?"),
    ]
    # 코드를 만든 세션이 설계로 되돌아가면 수정 요청을 못 받는다.
    assert revived.state.project.phase == Phase.IMPLEMENT
    assert revived.state.generated_code_map == {"App.tsx": "x"}


def test_restart_preserves_owner_so_next_save_lands_in_same_folder(isolated_sessions):
    """복원 시 소유자(user_id)도 살아나야 한다 — 안 그러면 재시작 후 저장 경로가 바뀐다."""
    orch = server.get_orchestrator("s1", "userA")
    _talk(orch, [("모터 돌려줘", "몇 초 돌릴까요?")])
    server.auto_save("s1", orch)

    server.sessions = InMemorySessionStore()
    revived = server.get_orchestrator("s1", "userA")

    assert revived._user_id == "userA"
    assert os.path.exists(server._session_path("userA", "s1"))


# ──────────────────────────────────────────────────────────────────────────────
# (B) 레플리카 교차 — 공유 볼륨 + mtime 무효화로 이어받는다
# ──────────────────────────────────────────────────────────────────────────────

def test_replica_picks_up_turn_saved_by_another_replica(isolated_sessions):
    """레플리카 B 가 옛 상태를 캐시한 뒤 A 가 새 턴을 저장하면, B 는 재로딩해야 한다.

    이게 이중화의 핵심 안전장치다. 실패하면 학생이 "방금 만든 게 사라졌다" 를 겪는다
    (B 가 stale 캐시를 그대로 서빙).

    같은 박스 3 레플리카는 ./projects 를 공유하므로 A 의 저장이 곧 B 의 디스크에 보인다.
    should_reload(mtime) 가 그 변화를 잡아 캐시를 버리는지를 get_orchestrator 경로로 검증.
    """
    # 레플리카 B: 1턴 시점 상태를 캐시에 적재
    b_cached = server.get_orchestrator("s1", "userA")
    _talk(b_cached, [("LED 켜줘", "어떤 색으로요?")])
    server.auto_save("s1", b_cached)
    assert server.sessions.get("s1") is b_cached

    # 레플리카 A: 같은 세션에 2턴을 더 쌓아 공유 볼륨에 저장
    a = _orch("s1")
    a._user_id = "userA"
    _talk(a, [("LED 켜줘", "어떤 색으로요?"), ("빨간색", "빨간 LED 를 켰어요.")])
    path = server._session_path("userA", "s1")
    # mtime 이 확실히 더 크도록 명시 지정(파일시스템 mtime 해상도 회피).
    server._write_session_json(server._build_save_data("s1", a), path)
    newer = os.path.getmtime(path) + 10
    os.utime(path, (newer, newer))

    # 레플리카 B 로 다음 요청이 들어온다 → stale 캐시를 버리고 A 의 저장을 이어받아야 한다.
    b_next = server.get_orchestrator("s1", "userA")

    assert _texts(b_next)[-1] == ("assistant", "빨간 LED 를 켰어요.")
    assert len(_texts(b_next)) == 4, "A 가 저장한 2턴(4메시지)을 B 가 이어받아야 한다"


def test_cached_session_reused_when_disk_not_newer(isolated_sessions):
    """반대 방향: 디스크가 더 최신이 아니면 굳이 재로딩하지 않는다(불필요한 I/O 방지)."""
    first = server.get_orchestrator("s1", "userA")
    _talk(first, [("안녕", "안녕하세요!")])
    server.auto_save("s1", first)

    again = server.get_orchestrator("s1", "userA")
    assert again is first, "디스크 변화가 없으면 같은 인스턴스를 재사용해야 한다"


def test_should_reload_contract_matches_shared_volume_assumption():
    """mtime 계약 재확인 — 이중화가 이 계약에 의존하므로 여기서도 못박는다.

    ⚠ 이 계약은 '공유 볼륨' 전제다. 레플리카를 서로 다른 박스로 흩으면 각자 다른
    디스크를 보게 되어 disk_mtime 비교가 무의미해지고 stale 서빙이 생긴다.
    (그래서 docker-compose.yml 은 3 레플리카를 같은 박스·같은 ./projects 로 묶는다.)
    """
    assert should_reload(None, 100.0) is True          # 적재 시점 미상 → 안전하게 재로딩
    assert should_reload(100.0, 200.0) is True         # 다른 레플리카가 더 최신 저장
    assert should_reload(200.0, 100.0) is False        # 내 캐시가 더 최신
    assert should_reload(100.0, 100.0) is False        # 동일 → 재로딩 불필요
    assert should_reload(100.0, 0.0) is False          # 디스크 파일 없음(신규)
