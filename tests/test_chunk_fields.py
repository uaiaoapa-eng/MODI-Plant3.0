"""#44 청크 부가 필드(payload/domain/difficulty/intent/modi_keys) 파생 + 되먹임 + 필터 검증.

torch 없이 결정적으로 동작(임베딩 0벡터 = 부분일치 경로). 등록 스토어는 tmp 로 격리.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import chunk_fields as CF  # noqa: E402

try:
    import registry_lib as R  # noqa: E402
    import search_lib as S  # noqa: E402
except Exception as e:  # pragma: no cover
    pytest.skip(f"import 불가: {e}", allow_module_level=True)


# ----- 순수 파생 함수 -----

def test_derive_intent():
    assert CF.derive_intent("learning_note") == "explain_concept"
    assert CF.derive_intent("code") == "implement_request"
    assert CF.derive_intent(None) == "explain_concept"  # 기본


def test_derive_difficulty_bands():
    assert CF.derive_difficulty(0) == "easy"
    assert CF.derive_difficulty(1) == "easy"
    assert CF.derive_difficulty(2) == "medium"
    assert CF.derive_difficulty(3) == "medium"
    assert CF.derive_difficulty(4) == "hard"
    assert CF.derive_difficulty(None) is None


def test_derive_domain():
    assert CF.derive_domain("blockly") == "blockly"
    assert CF.derive_domain("react") == "react"
    assert CF.derive_domain("hybrid") == "hybrid"
    assert CF.derive_domain(None) == "general"


def test_note_payload_dict_and_str():
    p = CF.note_payload({"title": "T", "what": "W", "why": "Y", "where": "Z"},
                        coding_type="blockly", concept_key="loop")
    assert p["kind"] == "learning_note" and p["title"] == "T" and p["what"] == "W"
    assert p["coding_type"] == "blockly" and p["concept_key"] == "loop"
    ps = CF.note_payload("그냥 문자열 노트")
    assert ps["kind"] == "learning_note" and ps["title"].startswith("그냥")


def test_modi_module_keys():
    assert CF.modi_module_keys({"modules": [{"key": "led"}, {"key": "button"}, {"key": "led"}]}) == ["button", "led"]
    assert CF.modi_module_keys([{"key": "motor"}]) == ["motor"]
    assert CF.modi_module_keys(None) == []


# ----- 되먹임 등록 + 검색 필드/필터 (tmp 격리) -----

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "REG_JSONL", str(tmp_path / "registered.jsonl"))
    monkeypatch.setattr(R, "REG_EMB", str(tmp_path / "registered_emb.npy"))
    monkeypatch.setattr(R, "_embed", lambda t: np.zeros(R.DIM, dtype=np.float32))
    monkeypatch.setattr(R, "_meta", None)
    monkeypatch.setattr(R, "_emb", None)
    monkeypatch.setattr(S, "_embed_query", lambda t: None)
    monkeypatch.setattr(S, "_combo_meta", None)
    monkeypatch.setattr(S, "_combo_ver", -1)
    return R


def test_writeback_fills_new_fields(store):
    """learning_note 되먹임 등록 시 payload/domain/intent/modi_keys 가 채워진다(#44)."""
    added = store.register_learning_notes(
        "s1", "uA", "blockly",
        [{"title": "앞 장애물 감지 후 정지", "what": "센서로 거리 측정", "why": "충돌 방지", "where": "로봇"}],
        modi_keys=["button", "led"],
    )
    assert added == 1
    _, meta = store.assets()
    row = meta[-1]
    assert row["intent"] == "explain_concept"
    assert row["domain"] == "blockly"
    assert row["modi_keys"] == ["button", "led"]
    assert row["payload"]["kind"] == "learning_note"
    assert row["payload"]["what"] == "센서로 거리 측정"
    assert row["outcome"] == "success"


def test_quality_gate_skips_trivial(store):
    """본문이 사실상 빈 노트는 품질 게이트로 등록되지 않는다."""
    added = store.register_learning_notes("s2", "u", "react",
                                          [{"title": "제목만", "what": "", "why": "", "where": ""}])
    assert added == 0 and store.count() == 0


def test_quality_gate_rejects_bad_outcome(store):
    res = store.register("q", "실패한 결과", "빌드 실패 로그", outcome="failed")
    assert res["ok"] is False


def test_search_result_carries_payload(store):
    store.register_learning_notes("s3", "uB", "react",
        [{"title": "무지개 그라디언트 배경", "what": "색상환 보간", "why": "부드러운 전환", "where": "캔버스"}])
    r = S.search("무지개 그라디언트 배경 그리기", top=5)
    hit = next(x for x in r["results"] if x["source"] == "registered" and "무지개" in x["title"])
    assert hit["intent"] == "explain_concept"
    assert hit["domain"] == "react"
    assert hit["payload"]["kind"] == "learning_note"


def test_payload_builders():
    dp = CF.design_doc_payload({"project_name": "P", "features": [{"name": "f"}]})
    assert dp["kind"] == "design_doc" and dp["project_name"] == "P"
    cp = CF.code_payload({"a.tsx": "code"})
    assert cp["kind"] == "code" and cp["files"]["a.tsx"] == "code"
    assert "docs" not in cp  # docs 없으면 키 자체가 없다(회귀 없음)


# ── EDU-27 직접서브 문서복원: writeback 동봉 payload 상한 ──

def test_docs_payload_none_when_empty():
    assert CF.docs_payload(None, None) is None
    assert CF.docs_payload([], None) is None


def test_docs_payload_caps_note_count_and_body_length():
    notes = [{"title": f"T{i}", "what": "w" * 3000, "why": "y" * 3000, "where": "z" * 3000}
             for i in range(10)]
    d = CF.docs_payload(notes, None)
    assert d is not None
    assert len(d["learning_notes"]) == CF.MAX_DOCS_NOTES  # 6개로 컷
    note = d["learning_notes"][0]
    assert len(note["what"]) == CF.MAX_NOTE_CHARS
    assert len(note["why"]) == CF.MAX_NOTE_CHARS
    assert len(note["where"]) == CF.MAX_NOTE_CHARS
    assert d["design_doc"] is None


def test_docs_payload_handles_string_notes():
    d = CF.docs_payload(["그냥 문자열 노트"], {"project_name": "P"})
    assert d["learning_notes"][0]["what"] == "그냥 문자열 노트"
    assert d["design_doc"]["project_name"] == "P"


def test_code_payload_embeds_docs_when_present():
    docs = {"learning_notes": [{"title": "t", "what": "w", "why": "y", "where": "z"}],
           "design_doc": None}
    cp = CF.code_payload({"a.tsx": "code"}, docs=docs)
    assert cp["docs"] == docs


def test_register_result_design_doc_and_code(store):
    """설계문서·코드 write-back 등록 + 중복 제거 + 검색 payload 전달(#44 확장)."""
    dd = {"project_name": "우주 게임", "description": "별을 모으는 게임",
          "features": [{"name": "점수판", "description": "점수", "priority": "mvp"}],
          "pages": [{"name": "메인", "description": "시작화면"}]}
    code = {"App.tsx": "export default function App(){return null}", "style.css": "body{}"}
    n = store.register_result("sx", "uZ", "react", design_doc=dd, code_map=code, modi_keys=["led"])
    assert n == 2
    # 매 턴 재호출돼도 중복 등록 안 됨
    assert store.register_result("sx", "uZ", "react", design_doc=dd, code_map=code) == 0
    r = S.search("별을 모으는 우주 게임 점수판", top=8)
    dc = next((x for x in r["results"] if (x.get("payload") or {}).get("kind") == "design_doc"), None)
    assert dc is not None
    assert dc["payload"]["features"][0]["name"] == "점수판"
    assert dc["intent"] == "design_review"


def test_register_result_quality_gate(store):
    """실질 내용 없는 설계문서(빈 features/pages)는 등록 안 함."""
    assert store.register_result("sy", "u", "react", design_doc={"project_name": "빈 것"}) == 0


def test_search_dedups_identical_notes(monkeypatch):
    """코퍼스에 같은 노트가 여러 세션에 복제돼도 검색 결과엔 1건만(#44 중복 제거)."""
    meta = [
        {"chunk_id": 1, "title": "자동차의 눈", "content": "거리 감지 반복", "concept_key": "distance_sensing",
         "coding_type": "blockly", "source": "base"},
        {"chunk_id": 2, "title": "자동차의 눈", "content": "거리 감지 반복", "concept_key": "distance_sensing",
         "coding_type": "blockly", "source": "base"},  # 완전 복제
        {"chunk_id": 3, "title": "다른 노트", "content": "무관", "concept_key": "loop",
         "coding_type": "blockly", "source": "base"},
    ]
    monkeypatch.setattr(S, "_combined", lambda: (None, meta))
    monkeypatch.setattr(S, "_embed_query", lambda t: None)
    r = S.search("자동차 거리 감지", top=10)
    titles = [x["title"] for x in r["results"]]
    assert titles.count("자동차의 눈") == 1  # 복제 6건이어도 1건만 노출


def test_persona_difficulty_and_domain_filter(monkeypatch):
    """difficulty/domain 필터가 노출을 실제 값으로 좁힌다(순위 로직은 불변)."""
    meta = [
        {"chunk_id": 1, "title": "쉬운 반복", "content": "반복 개념", "concept_key": "loop",
         "coding_type": "blockly", "difficulty": "easy", "domain": "blockly", "source": "base"},
        {"chunk_id": 2, "title": "어려운 반복", "content": "반복 심화", "concept_key": "x",
         "coding_type": "react", "difficulty": "hard", "domain": "react", "source": "base"},
    ]
    monkeypatch.setattr(S, "_combined", lambda: (None, meta))
    monkeypatch.setattr(S, "_embed_query", lambda t: None)
    easy = S.search("반복", top=5, difficulty="easy")
    assert easy["results"] and all(x["difficulty"] == "easy" for x in easy["results"])
    react = S.search("반복", top=5, domain="react")
    assert react["results"] and all(x["domain"] == "react" for x in react["results"])
