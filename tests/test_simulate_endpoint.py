"""/api/simulate 엔드포인트 검증 — LLM 없이 /chat 온톨로지 RAG 분기·프라임 재현(옵션 A).

핵심: 코드 턴/비코드 턴 분기, 구조 필드, 그리고 LLM(오케스트레이터 chat_stream)을
절대 호출하지 않음(비용 0).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)


def test_simulate_code_turn_has_ontology():
    """구현 턴: code_action=True, 온톨로지(개념·선수학습·MODI·카드) + 주입 프라임 반환."""
    r = client.post("/api/simulate", json={
        "message": "자동차가 벽 보고 스스로 멈추게", "phase": "implement", "coding_type": "blockly"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["intent"] == "implement_request"
    assert d["code_action"] is True and d["injected"] is True
    onto = d["ontology"]
    assert onto["primary"] and onto["primary"]["key"] == "distance_sensing"
    assert onto["prerequisites"], "선수학습 경로가 있어야 함"
    assert onto["modi_modules"], "MODI 하드웨어 관계가 있어야 함(요구 #2)"
    assert d["prime_block"] and "[온톨로지 제안" in d["prime_block"]


def test_simulate_non_code_turn_skips():
    """대화/질문 턴: code_action=False, injected=False, 프라임 없음."""
    r = client.post("/api/simulate", json={
        "message": "고마워", "phase": "implement", "has_code": True})
    d = r.json()
    assert d["code_action"] is False
    assert d["injected"] is False
    assert d["prime_block"] is None


def test_simulate_design_phase_no_rag():
    """설계 phase 는 온톨로지 RAG 미발동(전환은 LLM 도구가 판단)."""
    r = client.post("/api/simulate", json={
        "message": "센서로 거리 재는 앱", "phase": "design", "mode": "design"})
    d = r.json()
    assert d["intent"] == "design_explain"
    assert d["code_action"] is False


def test_simulate_never_invokes_llm(monkeypatch):
    """엔드포인트는 오케스트레이터 chat_stream(LLM)을 절대 호출하지 않는다(비용 0 보장)."""
    from agent.orchestrator_stream import StreamOrchestrator

    def _boom(*a, **k):
        raise AssertionError("simulate 가 LLM(chat_stream)을 호출하면 안 됨")

    monkeypatch.setattr(StreamOrchestrator, "chat_stream", _boom)
    r = client.post("/api/simulate", json={
        "message": "벽 보고 멈추는 자동차 만들어줘", "phase": "implement", "coding_type": "blockly"})
    assert r.status_code == 200
    assert r.json()["code_action"] is True
