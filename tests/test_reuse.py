"""chat 재사용 게이팅(agent/reuse.py) 결정 로직 단위 검증(#44). LLM/검색 실물 불필요 — search mock."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import reuse as RU  # noqa: E402


def _fake_search(result):
    return lambda q, coding_type=None, top=8, user_id=None, **k: {"ok": True, "top1_score": (result[0]["score"] if result else 0), "results": result}


def test_gate_none_for_non_implement(monkeypatch):
    import search_lib
    monkeypatch.setattr(search_lib, "search", _fake_search([]))
    assert RU.gate("설명해줘", "question", "react") is None      # 코드 대상 아님(첫 컷)
    assert RU.gate("만들어", "design_explain", "react") is None


def test_gate_picks_code_candidate(monkeypatch):
    import search_lib
    cand = {"title": "코드: App.tsx", "coding_type": "react", "decision": "reuse",
            "score": 0.71, "payload": {"kind": "code", "files": {"App.tsx": "x"}}}
    # 앞에 learning_note 후보가 있어도 code kind 만 고른다
    note = {"title": "노트", "coding_type": "react", "decision": "reuse", "score": 0.8,
            "payload": {"kind": "learning_note"}}
    monkeypatch.setattr(search_lib, "search", _fake_search([note, cand]))
    g = RU.gate("좋아요 버튼 만들어줘", "implement_request", "react")
    assert g and g["kind"] == "code" and g["decision"] == "reuse"
    assert g["candidate"]["title"] == "코드: App.tsx"


def test_gate_coding_type_guard(monkeypatch):
    import search_lib
    cand = {"title": "코드", "coding_type": "blockly", "decision": "reuse", "score": 0.7,
            "payload": {"kind": "code", "files": {"a": "b"}}}
    monkeypatch.setattr(search_lib, "search", _fake_search([cand]))
    # 요청은 react 인데 후보가 blockly → 제외. near-miss 계측(#84 후속)으로
    # None 대신 decision="none" + top1 을 반환한다(프라임 분기는 안 탐).
    g = RU.gate("만들어줘", "implement_request", "react")
    assert g and g["decision"] == "none" and g["candidate"] is None
    assert g["top1"] == 0.7


def test_gate_nearmiss_keeps_top1(monkeypatch):
    """#84 후속: top5 에 code 후보가 없어도 top1 점수는 관측용으로 남긴다."""
    import search_lib
    note = {"title": "노트", "coding_type": "react", "decision": "review", "score": 0.55,
            "payload": {"kind": "learning_note"}}
    monkeypatch.setattr(search_lib, "search", _fake_search([note]))
    g = RU.gate("만들어줘", "implement_request", "react")
    assert g and g["decision"] == "none" and g["candidate"] is None
    assert g["top1"] == 0.55 and g["cand_score"] is None


def test_gate_candidate_carries_own_score(monkeypatch):
    """#84 후속: 후보 자신의 점수(cand_score)도 계측용으로 함께 반환."""
    import search_lib
    note = {"title": "노트", "coding_type": "react", "decision": "reuse", "score": 0.9,
            "payload": {"kind": "learning_note"}}
    cand = {"title": "코드", "coding_type": "react", "decision": "review", "score": 0.52,
            "payload": {"kind": "code", "files": {"a": "b"}}}
    monkeypatch.setattr(search_lib, "search", _fake_search([note, cand]))
    g = RU.gate("만들어줘", "implement_request", "react")
    assert g["decision"] == "review" and g["top1"] == 0.9 and g["cand_score"] == 0.52


def test_gate_register_tier_passes_through(monkeypatch):
    import search_lib
    cand = {"title": "코드", "coding_type": "react", "decision": "register", "score": 0.2,
            "payload": {"kind": "code", "files": {"a": "b"}}}
    monkeypatch.setattr(search_lib, "search", _fake_search([cand]))
    g = RU.gate("만들어줘", "implement_request", "react")
    # 후보는 반환하되 decision=register → 호출측이 프라임 안 씀
    assert g and g["decision"] == "register"


def test_prime_reuse_vs_review():
    payload = {"kind": "code", "files": {"App.tsx": "console.log(1)"}}
    r = RU.prime(payload, "reuse")
    assert "최소한만 수정" in r and "App.tsx" in r and "console.log(1)" in r
    v = RU.prime(payload, "review")
    assert "참고" in v and "App.tsx" in v
    assert RU.prime({"kind": "code", "files": {}}, "reuse") == ""  # 파일 없으면 빈 프라임


# ── kind 한정 검색(EDU-27 §13 실전 미발동 수정) ──────────────
def _capture_search(seen: dict, result):
    def f(q, coding_type=None, top=8, user_id=None, intent=None, **k):
        seen["intent"] = intent
        return {"ok": True, "top1_score": (result[0]["score"] if result else 0), "results": result}
    return f


def test_gate_passes_code_intent_filter(monkeypatch):
    import search_lib
    seen: dict = {}
    cand = {"score": 0.7, "decision": "reuse", "coding_type": "react",
            "payload": {"kind": "code", "files": {"App.js": "x"}}}
    monkeypatch.setattr(search_lib, "search", _capture_search(seen, [cand]))
    monkeypatch.setattr(RU, "_REUSE_KIND_SEARCH", True)
    g = RU.gate("좋아요 버튼", "implement_request", "react")
    assert seen["intent"] == "implement_request"   # 게이트가 code 자산 한정으로 검색
    assert g and g["decision"] == "reuse"


def test_gate_kind_filter_killswitch_off(monkeypatch):
    import search_lib
    seen: dict = {}
    monkeypatch.setattr(search_lib, "search", _capture_search(seen, []))
    monkeypatch.setattr(RU, "_REUSE_KIND_SEARCH", False)
    RU.gate("좋아요 버튼", "implement_request", "react")
    assert seen["intent"] is None                  # 킬스위치 OFF → 종전 무필터 검색
