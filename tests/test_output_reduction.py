"""#68 O2/O3: 수정 턴 출력 절감 동작 검증.

O2: generate_code 가 기존과 완전히 동일한 코드를 다시 뱉으면(안 바뀐 파일 전체 재출력)
    재저장·dirty 표시 없이 되돌려 보내 edit_code 로 유도한다.
O3: 수정 턴(이미 코드 있음)에도 _reuse_block 이 재사용 코호트를 기록한다(전체 코드 프라임은
    콜드 전용). _REUSE_ON_MODIFY=0 이면 종전대로 즉시 (None, None).
"""
from agent.models import Phase
from agent.context import SessionState
from agent.tools import handle_tool_call
import agent.orchestrator_stream as OS
from agent import prime_service as PS  # #27 리팩토링: 재사용 킬스위치의 단일 소스


def _state_with_code(files: dict) -> SessionState:
    st = SessionState()
    st.project.phase = Phase.IMPLEMENT
    for path, code in files.items():
        st.generated_code_map[path] = code
    st.begin_turn()  # dirty 델타 추적 기준선
    return st


# ---------- O2: 안 바뀐 파일 재출력 차단 ----------

def test_generate_code_unchanged_is_noop():
    code = "export default function App(){return <div>hi</div>}"
    st = _state_with_code({"App.tsx": code})
    result = handle_tool_call("generate_code",
                              {"file_path": "App.tsx", "code": code, "description": "동일"}, st)
    assert "동일" in result and "edit_code" in result
    # 재저장/변경표시가 없어야 한다 — 이 턴에 코드가 안 바뀐 것으로 잡힌다.
    assert not st.code_changed_this_turn()


def test_generate_code_changed_still_applies():
    st = _state_with_code({"App.tsx": "old"})
    result = handle_tool_call("generate_code",
                              {"file_path": "App.tsx", "code": "new code", "description": "변경"}, st)
    assert "생성되었습니다" in result
    assert st.generated_code_map["App.tsx"] == "new code"
    assert st.code_changed_this_turn()


def test_new_file_generate_applies():
    st = _state_with_code({"App.tsx": "x"})
    result = handle_tool_call("generate_code",
                              {"file_path": "Extra.tsx", "code": "brand new", "description": "새 파일"}, st)
    assert "생성되었습니다" in result
    assert st.generated_code_map["Extra.tsx"] == "brand new"


# ---------- O3: 수정 턴 재사용 게이트 완화 ----------

def _orch_on_modify(monkeypatch, gate_ret):
    """generated_code_map 이 차 있는(수정 턴) 오케스트레이터 + 가짜 gate 로 _reuse_block 호출."""
    orch = OS.StreamOrchestrator(api_key="")
    orch.state.project.phase = Phase.IMPLEMENT
    orch.state.generated_code_map["App.tsx"] = "existing"
    orch._user_id = None
    orch._reuse_flag = None
    orch._ontology_primed = None
    # 온톨로지 제안은 이 테스트에서 제외(검색 인프라 불필요) → 항상 미발동.
    import agent.reuse as R
    monkeypatch.setattr(R, "ontology_suggest", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(R, "gate", lambda *a, **k: gate_ret)
    prime_calls = []
    monkeypatch.setattr(R, "prime", lambda payload, decision: prime_calls.append((payload, decision)) or "PRIME_BLOCK")
    block, msg = orch._reuse_block("색을 바꿔줘", "react")
    return orch, block, msg, prime_calls


def test_modify_turn_records_reuse_cohort_without_heavy_prime(monkeypatch):
    monkeypatch.setattr(PS, "REUSE_ON_MODIFY", True)
    gate_ret = {"decision": "reuse", "top1": 0.8, "kind": "code",
                "candidate": {"title": "이전 앱", "payload": {"files": {"App.tsx": "..."}}}}
    orch, block, msg, prime_calls = _orch_on_modify(monkeypatch, gate_ret)
    # 재사용 코호트는 기록되어야 한다(#67 T3 확장).
    assert orch._reuse_flag is not None
    assert orch._reuse_flag["decision"] == "reuse"
    # 전체 코드 프라임(_prime)은 수정 턴에서 호출되지 않는다(출력 인플레 방지).
    assert prime_calls == []
    # 온톨로지도 미발동이라 주입 블록 없음.
    assert block is None


def test_modify_turn_kill_switch_off(monkeypatch):
    monkeypatch.setattr(PS, "REUSE_ON_MODIFY", False)
    gate_ret = {"decision": "reuse", "top1": 0.8, "kind": "code",
                "candidate": {"title": "이전 앱", "payload": {"files": {"App.tsx": "..."}}}}
    orch, block, msg, prime_calls = _orch_on_modify(monkeypatch, gate_ret)
    # 킬스위치 OFF → 종전대로 즉시 반환, 아무 검색/기록도 안 한다(무회귀).
    assert (block, msg) == (None, None)
    assert orch._reuse_flag is None
    assert prime_calls == []


def test_cold_build_still_injects_full_prime(monkeypatch):
    monkeypatch.setattr(PS, "REUSE_ON_MODIFY", True)
    orch = OS.StreamOrchestrator(api_key="")
    orch.state.project.phase = Phase.IMPLEMENT
    # 콜드 빌드: 코드 없음.
    orch._user_id = None
    orch._reuse_flag = None
    orch._ontology_primed = None
    import agent.reuse as R
    monkeypatch.setattr(R, "ontology_suggest", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(R, "gate", lambda *a, **k: {"decision": "reuse", "top1": 0.9, "kind": "code",
                        "candidate": {"title": "t", "payload": {"files": {"App.tsx": "code"}}}})
    monkeypatch.setattr(R, "prime", lambda payload, decision: "FULL_CODE_PRIME")

    # #EDU-27 시드편집 ON(#84 이후 기본 OFF라 명시 활성화): 콜드 reuse 는 heavy 프라임 대신
    # 후보 코드를 세션에 시드하고 edit 유도 블록을 붙인다 → diff 편집 유도(출력토큰 절감).
    monkeypatch.setattr(PS, "REUSE_SEED_EDIT", True)
    block, msg = orch._reuse_block("투두앱 만들어줘", "react")
    assert "FULL_CODE_PRIME" not in block
    assert "재사용 편집" in block  # REUSE_SEED_HEAD 주입
    assert orch.state.generated_code_map.get("App.tsx") == "code"  # 후보 코드가 세션에 시드됨
    assert getattr(orch, "_reuse_seeded", False) is True
    assert orch._reuse_flag["decision"] == "reuse"

    # 킬스위치 OFF: 종전 heavy 프라임(전체재작성 유도) 폴백(무회귀).
    monkeypatch.setattr(PS, "REUSE_SEED_EDIT", False)
    orch2 = OS.StreamOrchestrator(api_key="")
    orch2.state.project.phase = Phase.IMPLEMENT
    orch2._user_id = None
    orch2._reuse_flag = None
    orch2._ontology_primed = None
    block2, _ = orch2._reuse_block("투두앱 만들어줘", "react")
    assert block2 is not None and "FULL_CODE_PRIME" in block2
