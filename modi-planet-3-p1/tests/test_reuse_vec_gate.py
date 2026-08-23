"""vec 기준 재사용 게이트 승격 단위 검증(EDU-27).

실트래픽 22건 검증: combined(=0.4·vec+0.15·lex+0.45·con) 는 con(개념 centroid 신호)이
"같은 UI 프리미티브 계열"이면 무관 쌍도 부풀려, 진짜 재사용(combined .539/.563)과 오탐
("로그인 화면" 요청에 "버튼 LED 제어" 코드, combined .526)을 못 가른다(차이 0.013).
반면 vec 성분은 완벽 분리(진짜쌍 .604/.628, 가짜쌍 .442) — gate() 가 review 판정을
vec ≥ TAU_REUSE_VEC 기준으로 reuse 로 승격하는지 agent.reuse._search 를 monkeypatch 해
네트워크 없이 검증한다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import reuse as RU  # noqa: E402


def _fake_search(candidate: dict | None):
    results = [candidate] if candidate else []

    def f(user_input, coding_type=None, user_id=None, top=5, intent=None):
        return {"ok": True, "top1_score": (candidate.get("score") if candidate else 0),
                "results": results}
    return f


def _candidate(decision: str, vec, score: float = 0.5) -> dict:
    c = {"title": "코드", "coding_type": "any", "decision": decision, "score": score,
         "payload": {"kind": "code", "files": {"a.py": "x"}}}
    if vec is not None:
        c["vec"] = vec
    return c


def test_true_reuse_promotes_review_to_reuse(monkeypatch):
    """실측: "피하기 게임 만들어줘" — vec=0.604, combined=0.539 → review 였던 판정을 reuse 로 승격."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("review", 0.604, 0.539)))
    g = RU.gate("피하기 게임 만들어줘", "implement_request", "any")
    assert g["decision"] == "reuse"
    assert g["vec_promoted"] is True


def test_false_positive_stays_review(monkeypatch):
    """실측: 로그인 화면 요청 → 버튼 LED 제어 코드. vec=0.442 는 임계 미달 → review 유지."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("review", 0.442, 0.526)))
    g = RU.gate("로그인 화면 만들어줘", "implement_request", "any")
    assert g["decision"] == "review"
    assert g["vec_promoted"] is False


def test_killswitch_off_keeps_combined_decision(monkeypatch):
    """REUSE_VEC_GATE=0 이면 vec 이 임계 이상이어도 종전 combined 판정(review) 그대로."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("review", 0.604, 0.539)))
    monkeypatch.setattr(RU, "_REUSE_VEC_GATE", False)
    g = RU.gate("피하기 게임 만들어줘", "implement_request", "any")
    assert g["decision"] == "review"
    assert g["vec_promoted"] is False


def test_register_tier_not_promoted(monkeypatch):
    """register(콜드) 는 vec 이 높아도 승격 대상이 아니다 — combined<0.48 은 near 도 미달."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("register", 0.70, 0.30)))
    g = RU.gate("만들어줘", "implement_request", "any")
    assert g["decision"] == "register"
    assert g["vec_promoted"] is False


def test_missing_vec_field_keeps_decision(monkeypatch):
    """vec 필드가 없는 후보(프록시/로컬 방어) — 예외 없이 종전 decision 유지."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("review", None, 0.55)))
    g = RU.gate("만들어줘", "implement_request", "any")
    assert g["decision"] == "review"
    assert g["vec_promoted"] is False
    assert g["vec"] is None


def test_existing_reuse_decision_unaffected(monkeypatch):
    """이미 reuse 인 후보는 그대로 reuse — 승격 로직이 회귀를 만들지 않는다."""
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("reuse", 0.9, 0.9)))
    g = RU.gate("만들어줘", "implement_request", "any")
    assert g["decision"] == "reuse"
    assert g["vec_promoted"] is False  # 이미 reuse 였으므로 "승격"은 아님(관측상 구분)


def test_reuse_block_preserves_vec_fields(monkeypatch):
    """_reuse_block 의 _reuse_flag 재구성이 vec/vec_promoted 를 보존한다(PR#89 후속).

    회귀 배경: 재구성 dict 가 화이트리스트 방식이라 gate 의 신규 관측 필드를 떨어뜨려
    reuse_vec_promoted 스코어가 프로덕션에서 미발행됐다(실트래픽 c43f8d33 확인).
    """
    import agent.orchestrator_stream as OS
    from agent.models import Phase

    orch = OS.StreamOrchestrator(api_key="")
    orch.state.project.phase = Phase.IMPLEMENT
    orch._user_id = None
    orch._reuse_flag = None
    orch._ontology_primed = None
    monkeypatch.setattr(RU, "ontology_suggest", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(RU, "_search", _fake_search(_candidate("review", 0.604, 0.539)))
    orch._reuse_block("피하기 게임 만들어줘", "any")
    assert orch._reuse_flag is not None
    assert orch._reuse_flag["decision"] == "reuse"      # vec 승격 반영
    assert orch._reuse_flag["vec_promoted"] is True     # 관측 필드 보존(누락 회귀 방지)
    assert orch._reuse_flag["vec"] == 0.604
