"""#67 T3: 재사용/온톨로지 계측이 Langfuse trace 스코어로 전파되는지 검증.

_emit_turn_scores 가 매 턴 재사용 결정(CATEGORICAL)·온톨로지 프라임(BOOLEAN) 스코어를
찍어야 Langfuse 에서 reuse/review/register·프라임 코호트를 분리할 수 있다(TAU 튜닝 전제).
실제 Langfuse 대신 스코어 호출을 기록하는 가짜 클라이언트를 주입한다.
"""
import agent.orchestrator_stream as OS


class _FakeClient:
    def __init__(self):
        self.scores = []

    def score_current_trace(self, name, value, data_type=None, **kw):
        self.scores.append({"name": name, "value": value, "data_type": data_type})

    # _emit_turn_scores 는 score_current_trace 만 쓴다.


def _emit_with(monkeypatch, reuse_flag, ontology_primed, mode="quick", direct_served=None):
    fake = _FakeClient()
    monkeypatch.setattr(OS, "get_client", lambda: fake)
    orch = OS.StreamOrchestrator(api_key="")
    orch._reuse_flag = reuse_flag
    orch._ontology_primed = ontology_primed
    orch._direct_served = direct_served
    orch._emit_turn_scores(mode)
    return {s["name"]: s for s in fake.scores}


def test_reuse_decision_and_ontology_scores_always_emitted(monkeypatch):
    scores = _emit_with(monkeypatch, reuse_flag=None, ontology_primed=None)
    # 미발동이어도 코호트 분리를 위해 항상 찍힌다.
    assert scores["재사용 결정 (reuse_decision)"]["value"] == "none"
    assert scores["재사용 결정 (reuse_decision)"]["data_type"] == "CATEGORICAL"
    assert scores["온톨로지 프라임 (ontology_primed)"]["value"] == 0
    assert scores["온톨로지 프라임 (ontology_primed)"]["data_type"] == "BOOLEAN"


def test_reuse_decision_reflects_gate(monkeypatch):
    scores = _emit_with(
        monkeypatch,
        reuse_flag={"decision": "reuse", "top1": 0.71, "kind": "code", "source_title": "t"},
        ontology_primed={"concept": "조건문", "prerequisites": 2, "artifacts": 1},
    )
    assert scores["재사용 결정 (reuse_decision)"]["value"] == "reuse"
    assert scores["온톨로지 프라임 (ontology_primed)"]["value"] == 1


def test_review_decision(monkeypatch):
    scores = _emit_with(monkeypatch, reuse_flag={"decision": "review"}, ontology_primed=None)
    assert scores["재사용 결정 (reuse_decision)"]["value"] == "review"


def test_nearmiss_top1_scores_emitted(monkeypatch):
    """#84 후속: 게이트 검색이 돈 턴은 판정과 무관하게 top1/cand_score NUMERIC 이 찍힌다."""
    scores = _emit_with(
        monkeypatch,
        reuse_flag={"decision": "register", "top1": 0.55, "cand_score": 0.41},
        ontology_primed=None,
    )
    assert scores["재사용 결정 (reuse_decision)"]["value"] == "register"
    assert scores["재사용 top1 (reuse_top1)"]["value"] == 0.55
    assert scores["재사용 top1 (reuse_top1)"]["data_type"] == "NUMERIC"
    assert scores["재사용 후보점수 (reuse_cand_score)"]["value"] == 0.41


def test_no_gate_no_top1_scores(monkeypatch):
    """게이트 검색이 안 돈 턴(비코드 턴 등)은 top1 스코어를 찍지 않는다."""
    scores = _emit_with(monkeypatch, reuse_flag=None, ontology_primed=None)
    assert "재사용 top1 (reuse_top1)" not in scores
    assert "재사용 후보점수 (reuse_cand_score)" not in scores


def test_nearmiss_without_candidate(monkeypatch):
    """code 후보가 없는 near-miss(decision=none)도 top1 은 남고 cand_score 는 안 찍힌다."""
    scores = _emit_with(
        monkeypatch,
        reuse_flag={"decision": "none", "top1": 0.3, "cand_score": None},
        ontology_primed=None,
    )
    assert scores["재사용 결정 (reuse_decision)"]["value"] == "none"
    assert scores["재사용 top1 (reuse_top1)"]["value"] == 0.3
    assert "재사용 후보점수 (reuse_cand_score)" not in scores


def test_docs_restored_score_emitted(monkeypatch):
    """#92: 직접서브 턴의 docs_restored 가 NUMERIC 스코어로 발행된다(UI 필터·코호트용)."""
    scores = _emit_with(
        monkeypatch,
        reuse_flag={"decision": "reuse", "top1": 0.62},
        ontology_primed=None,
        direct_served={"score": 95, "accept": True, "docs_restored": 7},
    )
    assert scores["직접서브 문서복원 (docs_restored)"]["value"] == 7
    assert scores["직접서브 문서복원 (docs_restored)"]["data_type"] == "NUMERIC"


def test_docs_restored_absent_key_not_emitted(monkeypatch):
    """#92: docs_restored 키가 없는(구버전) _direct_served 는 스코어를 찍지 않는다."""
    scores = _emit_with(
        monkeypatch,
        reuse_flag={"decision": "reuse", "top1": 0.62},
        ontology_primed=None,
        direct_served={"score": 85, "accept": False},
    )
    assert "직접서브 문서복원 (docs_restored)" not in scores
    # 기존 스코어는 그대로 발행(회귀 없음).
    assert scores["직접서브 만족도 (direct_serve_score)"]["value"] == 85


def test_docs_restored_no_verdict_not_emitted(monkeypatch):
    """#92: 만족도 검증이 안 돈 턴(_direct_served=None)은 직접서브 계열 스코어가 전무하다."""
    scores = _emit_with(monkeypatch, reuse_flag=None, ontology_primed=None, direct_served=None)
    assert "직접서브 문서복원 (docs_restored)" not in scores
    assert "직접서브 (direct_served)" not in scores
