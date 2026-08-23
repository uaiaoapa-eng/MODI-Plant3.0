"""prime_service 단위 검증 — /chat 온톨로지 RAG 분기·프라임의 단일 소스.

계층:
  1) resolve_intent : 규칙(정규식 Router) 기반 intent 분류 (LLM 미사용).
  2) build_prime    : intent → 분기(code_action) → 코드 턴이면 프라임 조립.
비코드 턴에서는 온톨로지/게이트를 아예 호출하지 않는다(분기 계약).
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

from agent import prime_service as PS  # noqa: E402
from agent.models import Phase  # noqa: E402


# ── 1) resolve_intent 규칙 표 ───────────────────────────────────────────────
@pytest.mark.parametrize("message,mode,phase,has_code,expected_code_action", [
    ("자동차가 벽 보고 멈추게 만들어줘", "quick", Phase.IMPLEMENT, False, True),   # 신규 구현
    ("색을 빨강으로 바꿔줘", "quick", Phase.IMPLEMENT, True, True),                 # 수정
    ("이거 어떻게 동작해?", "quick", Phase.IMPLEMENT, True, False),                  # 질문
    ("고마워", "quick", Phase.IMPLEMENT, True, False),                              # 잡담
    ("안녕", "design", Phase.DESIGN, False, False),                                 # 설계 대화(코드 아님)
])
def test_resolve_intent_branch(message, mode, phase, has_code, expected_code_action):
    intent = PS.resolve_intent(message, mode, phase, has_code=has_code)
    assert PS.code_action(intent, phase) is expected_code_action


def test_design_phase_never_code_action():
    """설계 phase 는 항상 design_explain → 온톨로지 RAG 미발동(전환은 LLM 도구가 판단)."""
    intent = PS.resolve_intent("이제 만들자", "design", Phase.DESIGN, has_code=False)
    assert intent == "design_explain"
    assert PS.code_action(intent, Phase.DESIGN) is False


def test_implement_request_with_existing_code_becomes_modify():
    """이미 산출물이 있으면 '기능 추가'는 신규가 아니라 수정으로 정규화."""
    intent = PS.resolve_intent("기능 추가해줘", "quick", Phase.IMPLEMENT, has_code=True)
    assert intent == "modify_request"


# ── 2) build_prime 분기 계약 ────────────────────────────────────────────────
def test_non_code_turn_skips_ontology(monkeypatch):
    """비코드 턴이면 injected=False 이고 온톨로지/게이트를 호출하지 않는다."""
    calls = {"assemble": 0}

    def _spy(*a, **k):
        calls["assemble"] += 1
        return PS.PrimeResult()

    monkeypatch.setattr(PS, "assemble_prime", _spy)
    bundle = PS.build_prime("고마워", "blockly", phase=Phase.IMPLEMENT, has_code=True)
    assert bundle.code_action is False
    assert bundle.injected is False
    assert calls["assemble"] == 0, "비코드 턴에서 프라임 조립을 호출하면 안 됨"


def test_code_turn_calls_assemble(monkeypatch):
    """코드 턴이면 assemble_prime 를 태우고 그 결과를 번들에 담는다."""
    sentinel = PS.PrimeResult(block="BLOCK", status_msg="MSG",
                              ontology={"primary": {"key": "loop"}}, reuse_gate=None)
    monkeypatch.setattr(PS, "assemble_prime", lambda *a, **k: sentinel)
    bundle = PS.build_prime("반복하는 코드 만들어줘", "blockly",
                            phase=Phase.IMPLEMENT, has_code=False)
    assert bundle.code_action is True
    assert bundle.injected is True
    assert bundle.prime_block == "BLOCK"
    assert bundle.ontology == {"primary": {"key": "loop"}}


def test_assemble_reuse_on_modify_killswitch(monkeypatch):
    """REUSE_ON_MODIFY off + 수정 턴이면 즉시 빈 프라임(무회귀 킬스위치)."""
    monkeypatch.setattr(PS, "REUSE_ON_MODIFY", False)
    res = PS.assemble_prime("색 바꿔줘", "blockly", is_modify=True, user_id="")
    assert res.block is None and res.ontology is None and res.reuse_gate is None


def test_reuse_gate_surfaced(monkeypatch):
    """reuse.gate 가 reuse/review 를 반환하면 build_prime.reuse_gate 로 표면화된다."""
    import agent.reuse as RU
    monkeypatch.setattr(RU, "ontology_suggest", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(RU, "gate", lambda *a, **k: {
        "decision": "reuse", "kind": "code", "top1": 0.9,
        "candidate": {"title": "이전 결과", "payload": {"files": {"a.py": "x"}}}})
    res = PS.assemble_prime("비슷한거 만들어줘", "react", is_modify=False, user_id="")
    assert res.reuse_gate and res.reuse_gate["decision"] == "reuse"
    # #84 이후 기본(시드편집 OFF): reuse 콜드는 참고 코드 프라임(참고프라임+자유generate)을 붙인다.
    assert res.block and "재사용 컨텍스트" in res.block
    # 시드편집 ON(킬스위치): heavy 프라임 대신 세션 시드+edit 유도(assemble_prime 은 reuse_gate 로만 표면화).
    monkeypatch.setattr(PS, "REUSE_SEED_EDIT", True)
    res2 = PS.assemble_prime("비슷한거 만들어줘", "react", is_modify=False, user_id="")
    assert res2.block is None
