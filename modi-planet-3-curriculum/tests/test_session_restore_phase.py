"""세션 저장→복원 시 phase 보존 회귀 테스트 (LLM/토큰 소모 없음).

버그(수정됨): _build_save_data 는 phase 를 orch.get_phase() 로 저장하는데 이 값은
한글 라벨("설계"/"구현"/"검증")이다. 반면 _restore_state_from_file 의 phase_map 은
영어 키("design"/"implement"/"verify")만 알아서, "구현"이 매칭 안 돼 DESIGN 으로
폴백됐다. 그 결과 코드까지 다 만든 세션을 디스크에서 복원하면 설계 단계로 되돌아가
유저가 수정을 요청해도 코드를 못 고치고 설계 대화만 하게 됐다.

여기서는 실제 save/restore 함수를 그대로 태워 왕복이 phase 를 보존하는지 검증한다
(get_phase 라벨이나 phase_map 을 나중에 또 바꾸면 이 테스트가 잡는다).
"""
import json

import pytest

try:
    import server
    from agent.models import Phase
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가(의존성 미설치): {e}", allow_module_level=True)


def _new_orch():
    return server.StreamOrchestrator(api_key="", session_id="t")


def _roundtrip(orch, tmp_path):
    """실제 저장 포맷으로 직렬화 → 새 orch 로 복원해서 돌려준다."""
    data = server._build_save_data("t", orch)
    f = tmp_path / "sess.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    restored = _new_orch()
    server._restore_state_from_file(restored, str(f))
    return restored


@pytest.mark.parametrize("phase", [Phase.DESIGN, Phase.IMPLEMENT, Phase.VERIFY])
def test_save_restore_preserves_phase(tmp_path, phase):
    """저장→복원 왕복이 모든 phase 를 그대로 보존해야 한다(특히 IMPLEMENT)."""
    orch = _new_orch()
    orch.state.project.phase = phase
    orch.state.generated_code_map = {"App.tsx": "export default function App(){return null}"}

    restored = _roundtrip(orch, tmp_path)

    assert restored.state.project.phase == phase
    # 코드가 있는데 설계로 되돌아가면(=이 버그) 복원 세션이 코드를 못 고친다.
    assert restored.state.generated_code_map  # 코드도 함께 보존


def test_implement_session_stays_editable_after_restore(tmp_path):
    """핵심 회귀: 코드를 만든(IMPLEMENT) 세션을 복원해도 설계로 떨어지지 않아야 한다."""
    orch = _new_orch()
    orch.state.project.phase = Phase.IMPLEMENT
    orch.state.generated_code_map = {"App.tsx": "x"}

    # 저장 포맷은 canonical enum 값을 쓴다(표시용 한글 라벨 get_phase()와 분리 — 예전엔 한글을
    # 저장해서 복원 시 못 알아보던 게 버그의 근원이었음).
    assert server._build_save_data("t", orch)["phase"] == "implement"

    restored = _roundtrip(orch, tmp_path)
    assert restored.state.project.phase == Phase.IMPLEMENT


_TASK = {"id": 1, "name": "노트 낙하", "description": "", "files": [], "status": "pending"}


@pytest.mark.parametrize(
    "saved_task_plan",
    [
        {"tasks": [_TASK], "progress": "0/1"},  # 정식 dict 포맷
        [_TASK],  # 레거시/백필 유래: tasks 리스트가 바로 저장된 형태 (EDU-AGENT-B)
    ],
)
def test_restore_accepts_dict_and_list_task_plan(tmp_path, saved_task_plan):
    """task_plan 이 dict 든 레거시 list 든 복원이 크래시 없이 태스크를 채워야 한다.

    버그(EDU-AGENT-B, #81): 업스트림(MySQL 원천)에서 내려받은 세션 파일의 task_plan 이
    list 여서 task_data.get("tasks") 가 AttributeError 로 터졌고 /restore 가 500 을 냈다.
    """
    f = tmp_path / "sess.json"
    f.write_text(
        json.dumps({"phase": "implement", "task_plan": saved_task_plan}, ensure_ascii=False),
        encoding="utf-8",
    )
    orch = _new_orch()
    server._restore_state_from_file(orch, str(f))
    assert [t.name for t in orch.state.project.task_plan.tasks] == ["노트 낙하"]


@pytest.mark.parametrize("weird", [{"tasks": None}, {}, "oops", 3, [None, "x"]])
def test_restore_tolerates_malformed_task_plan(tmp_path, weird):
    """task_plan 이 이상한 형태여도 복원 자체는 성공하고 태스크만 비운다."""
    f = tmp_path / "sess.json"
    f.write_text(json.dumps({"phase": "design", "task_plan": weird}), encoding="utf-8")
    orch = _new_orch()
    server._restore_state_from_file(orch, str(f))
    assert orch.state.project.task_plan.tasks == []


@pytest.mark.parametrize(
    "saved,expected",
    [
        ("구현", Phase.IMPLEMENT),
        ("설계", Phase.DESIGN),
        ("검증", Phase.VERIFY),
        ("implement", Phase.IMPLEMENT),  # 영어로 저장된 파일도 계속 동작
        ("design", Phase.DESIGN),
        ("verify", Phase.VERIFY),
    ],
)
def test_restore_accepts_korean_and_english_phase(tmp_path, saved, expected):
    """phase_map 이 한글·영어 라벨을 모두 인식해야 한다."""
    f = tmp_path / "sess.json"
    f.write_text(
        json.dumps({"phase": saved, "generated_code": {"App.tsx": "x"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    orch = _new_orch()
    server._restore_state_from_file(orch, str(f))
    assert orch.state.project.phase == expected
