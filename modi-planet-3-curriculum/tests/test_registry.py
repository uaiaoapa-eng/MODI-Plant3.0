"""registry_lib 등록 스토어 + RAG 되먹임 + user_id 필터 테스트.

torch 없이 결정적으로 돌도록 임베딩은 몽키패치(부분일치 경로)로 대체.
등록 스토어 파일은 tmp 로 격리해 리포 data/ 를 오염시키지 않는다.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

try:
    import registry_lib as R
    import search_lib as S
except Exception as e:
    pytest.skip(f"import 불가: {e}", allow_module_level=True)

if not os.path.exists(S.META_PATH):
    pytest.skip("chunk_meta.json 자산 없음", allow_module_level=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """등록 스토어를 tmp 로 격리 + 임베딩은 0벡터(부분일치 경로) + 캐시 리셋."""
    monkeypatch.setattr(R, "REG_JSONL", str(tmp_path / "registered.jsonl"))
    monkeypatch.setattr(R, "REG_EMB", str(tmp_path / "registered_emb.npy"))
    monkeypatch.setattr(R, "_embed", lambda t: np.zeros(R.DIM, dtype=np.float32))
    monkeypatch.setattr(R, "_meta", None)
    monkeypatch.setattr(R, "_emb", None)
    # 검색은 부분일치(벡터 off) 경로로 → 결정적
    monkeypatch.setattr(S, "_embed_query", lambda t: None)
    monkeypatch.setattr(S, "_combo_meta", None)
    monkeypatch.setattr(S, "_combo_ver", -1)
    return R


def test_register_and_count(store):
    assert store.count() == 0
    res = store.register("q1", "지브리풍 파스텔 배경 만들기", "그라디언트로 부드러운 하늘", coding_type="react")
    assert res["ok"] is True and res["id"] == 0 and res["count"] == 1
    assert store.count() == 1


def test_register_requires_content(store):
    res = store.register("q", "", "")
    assert res["ok"] is False


def test_writeback_makes_query_searchable(store):
    """콜드 질문 → 등록 → 같은 질문 검색에서 등록물이 잡혀야(source=registered)."""
    q = "지브리풍 파스텔 노을 배경을 그리고 싶어요"
    before = S.search(q, top=3)
    assert all(r["source"] == "base" for r in before["results"])  # 아직 등록 전
    store.register(q, "지브리풍 파스텔 노을 배경", "파스텔 노을 그라디언트 배경 표현", coding_type="react")
    after = S.search(q, top=5)
    reg_hits = [r for r in after["results"] if r["source"] == "registered"]
    assert reg_hits, "등록물이 검색에 잡혀야 한다"
    assert "파스텔" in reg_hits[0]["title"]


def test_user_id_filter_scopes_registered(store):
    q = "네온 사이버펑크 도시 배경 애니메이션"
    store.register(q, "네온 사이버펑크 도시 배경", "네온 간판 도시 배경", coding_type="react", user_id="userA")
    # 전체(필터 없음) → 등록물 보임
    assert any(r["source"] == "registered" for r in S.search(q, top=5)["results"])
    # 다른 사용자 → 내 등록물 제외
    other = S.search(q, top=5, user_id="userB")
    assert all(r["source"] == "base" for r in other["results"])
    # 본인 → 등록물 다시 보임
    mine = S.search(q, top=5, user_id="userA")
    assert any(r["source"] == "registered" for r in mine["results"])


def test_visible_rules():
    base = {"source": "base"}
    regA = {"source": "registered", "user_id": "A"}
    regNone = {"source": "registered", "user_id": None}
    assert S._visible(base, None) and S._visible(regA, None)      # 필터 없음 → 전부
    assert S._visible(base, "B")                                  # base 는 전역
    assert not S._visible(regA, "B")                              # 남의 등록물 제외
    assert S._visible(regA, "A")                                  # 내 등록물
    assert S._visible(regNone, "B")                               # 무주인 등록물 허용


def test_reset_clears(store):
    store.register("q", "제목", "내용")
    assert store.count() == 1
    store.reset()
    assert store.count() == 0


def test_register_learning_notes_dedup(store):
    """세션 저장 훅: 같은 노트를 재저장해도 중복 등록 안 됨."""
    notes = [
        {"title": "앞 장애물 감지 후 정지", "what": "센서로 거리 측정", "why": "충돌 방지", "where": "로봇"},
        {"title": "속도 부드럽게 감속", "what": "PWM 점감", "why": "급정거 방지", "where": "모터"},
    ]
    added1 = store.register_learning_notes("sess1", "uA", "hybrid", notes)
    assert added1 == 2 and store.count() == 2
    added2 = store.register_learning_notes("sess1", "uA", "hybrid", notes)  # 동일 재저장
    assert added2 == 0 and store.count() == 2  # 중복 제거
    # 새 노트 하나 추가되면 그것만 등록
    notes.append({"title": "새 개념", "what": "x", "why": "y", "where": "z"})
    assert store.register_learning_notes("sess1", "uA", "hybrid", notes) == 1
    assert store.count() == 3


def test_register_learning_notes_searchable(store):
    store.register_learning_notes("sess2", "uB", "react",
        [{"title": "무지개 그라디언트 배경", "what": "색상환 보간", "why": "부드러운 전환", "where": "캔버스"}])
    r = S.search("무지개 그라디언트 배경 그리기", top=5)
    assert any(x["source"] == "registered" and "무지개" in x["title"] for x in r["results"])


def test_register_learning_notes_empty(store):
    assert store.register_learning_notes("s", "u", "react", []) == 0


# ── EDU-27 직접서브 문서복원: writeback 동봉 + 세션 조인 폴백 ──

def test_register_result_embeds_docs_in_code_payload(store):
    """code write-back 시 같은 세션의 학습노트·설계문서가 payload.docs 로 동봉된다."""
    notes = [{"title": "노트1", "what": "무엇", "why": "왜", "where": "어디"}]
    dd = {"project_name": "회원가입 폼", "description": "가입 폼",
          "features": [{"name": "제출", "description": "d", "priority": "mvp"}],
          "pages": [], "data_models": [], "user_flows": [], "strengths": [], "weaknesses": [],
          "users": []}
    code = {"App.tsx": "export default function App(){return null}"}
    n = store.register_result("sess-docs", "uD", "react", design_doc=dd, code_map=code,
                              learning_notes=notes)
    assert n == 2  # design_doc 청크 1 + code 청크 1
    r = S.search("회원가입 폼 제출", top=8)
    cc = next((x for x in r["results"] if (x.get("payload") or {}).get("kind") == "code"), None)
    assert cc is not None
    assert cc["payload"]["docs"]["learning_notes"][0]["title"] == "노트1"
    assert cc["payload"]["docs"]["design_doc"]["project_name"] == "회원가입 폼"
    assert cc["session_id"] == "sess-docs"


def test_register_result_without_notes_omits_docs_key(store):
    """learning_notes 도 design_doc 도 없으면 payload 에 docs 키 자체가 없다(회귀 없음)."""
    code = {"App.tsx": "code"}
    store.register_result("sess-nodocs", "uE", "react", code_map=code, goal="단순 카운터")
    r = S.search("단순 카운터", top=8)
    cc = next((x for x in r["results"] if (x.get("payload") or {}).get("kind") == "code"), None)
    assert cc is not None
    assert "docs" not in cc["payload"]


def test_get_by_session_filters_by_session_and_kind(store):
    store.register_learning_notes("sessJ", "uJ", "react",
        [{"title": "노트A", "what": "w", "why": "y", "where": "z"}])
    store.register_result("sessJ", "uJ", "react",
        design_doc={"project_name": "P", "features": [{"name": "f", "description": "", "priority": "mvp"}]},
        code_map={"a.tsx": "c"})
    store.register_learning_notes("otherSess", "uK", "react",
        [{"title": "다른세션노트", "what": "w", "why": "y", "where": "z"}])

    chunks = store.get_by_session("sessJ")
    types = {c["chunk_type"] for c in chunks}
    assert types == {"learning_note", "design_doc", "code"}

    only_notes = store.get_by_session("sessJ", ("learning_note",))
    assert len(only_notes) == 1 and only_notes[0]["title"] == "노트A"

    assert store.get_by_session("nope") == []
    assert store.get_by_session("") == []
