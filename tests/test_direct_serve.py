"""#84 직접서브(direct-serve) 티어 검증.

- check_satisfaction: haiku 판정 JSON → accept/reject + 델타 이중 가드 + 실패 폴백.
- _maybe_direct_serve: reuse 고신뢰 + accept → 저장물 그대로 세션 코드로 서브(생성 LLM 0).
  review/register·수정 턴·후보 없음은 서브 안 함. 킬스위치(DIRECT_SERVE) OFF 는 즉시 스킵.
- _emit_turn_scores: 3-tier(direct_serve/near/cold) 코호트 + direct_served/만족도 스코어.
"""
import json

import agent.direct_serve as DS
import agent.orchestrator_stream as OS
from agent.models import Phase


# --- 가짜 LLM/Langfuse 인프라 -------------------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = None
        self.cost_usd = None


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kw):
        return _Resp(self._text)


class _FakeAnthropic:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


class _FakeGen:
    def update(self, **kw):
        pass


class _FakeObs:
    def __enter__(self):
        return _FakeGen()

    def __exit__(self, *a):
        return False


class _FakeLF:
    def start_as_current_observation(self, **kw):
        return _FakeObs()

    def create_event(self, **kw):
        pass


def _patch_lf(monkeypatch):
    monkeypatch.setattr(DS, "get_client", lambda: _FakeLF())


_CAND = {"title": "빨간 하트 좋아요 버튼",
         "payload": {"kind": "code", "files": {"App.tsx": "export default function App(){return null}"}}}


# --- check_satisfaction -------------------------------------------------------

def test_satisfaction_accept_no_delta(monkeypatch):
    _patch_lf(monkeypatch)
    client = _FakeAnthropic(json.dumps({"score": 98, "delta": False, "reason": "동일"}))
    v = DS.check_satisfaction(client, "haiku", "좋아요 버튼 만들어줘", _CAND)
    assert v["ok"] and v["accept"] and v["score"] == 98 and v["delta"] is False


def test_satisfaction_reject_on_delta(monkeypatch):
    _patch_lf(monkeypatch)
    # 점수가 높아도 델타가 있으면(색 지정) 이중 가드로 accept 하지 않는다.
    client = _FakeAnthropic(json.dumps({"score": 95, "delta": True, "reason": "파란 색 델타"}))
    v = DS.check_satisfaction(client, "haiku", "파란 하트 좋아요 버튼", _CAND)
    assert v["ok"] and v["accept"] is False and v["delta"] is True


def test_satisfaction_reject_low_score(monkeypatch):
    _patch_lf(monkeypatch)
    client = _FakeAnthropic(json.dumps({"score": 20, "delta": False, "reason": "부족"}))
    v = DS.check_satisfaction(client, "haiku", "커지는 애니메이션 추가", _CAND)
    assert v["accept"] is False and v["score"] == 20


def test_satisfaction_parse_failure_is_safe(monkeypatch):
    _patch_lf(monkeypatch)
    client = _FakeAnthropic("설명만 하고 JSON 없음")  # 파싱 실패
    v = DS.check_satisfaction(client, "haiku", "무엇이든", _CAND)
    assert v["ok"] is False and v["accept"] is False  # 실패 → 생성 경로로 폴백


# --- _maybe_direct_serve ------------------------------------------------------

def _drive(gen):
    """제너레이터를 소진하고 (yield된 이벤트들, 반환값) 을 돌려준다."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value


def _orch(monkeypatch, decision, cand=_CAND):
    monkeypatch.setattr(DS, "ENABLED", True)
    orch = OS.StreamOrchestrator(api_key="")
    orch.state.project.phase = Phase.IMPLEMENT
    orch._reuse_flag = {"decision": decision} if decision else None
    orch._reuse_candidate = cand
    return orch


def test_direct_serve_accept_serves_stored_files(monkeypatch):
    _patch_lf(monkeypatch)
    orch = _orch(monkeypatch, "reuse")
    monkeypatch.setattr(DS, "check_satisfaction",
                        lambda *a, **k: {"score": 97, "delta": False, "accept": True, "ok": True})
    events, served = _drive(orch._maybe_direct_serve("좋아요 버튼 만들어줘"))
    assert served is True
    assert orch.state.generated_code_map.get("App.tsx") == _CAND["payload"]["files"]["App.tsx"]
    assert orch._direct_served["accept"] is True and orch._direct_served["score"] == 97
    assert any(e.get("type") == "status" for e in events)


def test_direct_serve_reject_does_not_serve(monkeypatch):
    _patch_lf(monkeypatch)
    orch = _orch(monkeypatch, "reuse")
    monkeypatch.setattr(DS, "check_satisfaction",
                        lambda *a, **k: {"score": 20, "delta": True, "accept": False, "ok": True})
    events, served = _drive(orch._maybe_direct_serve("파란 하트 좋아요 버튼"))
    assert served is False
    assert not orch.state.generated_code_map  # 서브 안 함 → 코드 없음(생성 경로로)
    assert orch._direct_served["accept"] is False


def test_direct_serve_skips_review_tier(monkeypatch):
    _patch_lf(monkeypatch)
    orch = _orch(monkeypatch, "review")  # 유사도 中 = review 는 만족도 검증조차 안 한다
    called = []
    monkeypatch.setattr(DS, "check_satisfaction", lambda *a, **k: called.append(1) or {})
    events, served = _drive(orch._maybe_direct_serve("비슷한거"))
    assert served is False and called == [] and orch._direct_served is None


def test_direct_serve_skips_when_modify_turn(monkeypatch):
    _patch_lf(monkeypatch)
    orch = _orch(monkeypatch, "reuse")
    orch.state.generated_code_map["App.tsx"] = "기존 코드"  # 수정 턴
    called = []
    monkeypatch.setattr(DS, "check_satisfaction", lambda *a, **k: called.append(1) or {})
    events, served = _drive(orch._maybe_direct_serve("바꿔줘"))
    assert served is False and called == []


def test_direct_serve_killswitch_off(monkeypatch):
    _patch_lf(monkeypatch)
    orch = _orch(monkeypatch, "reuse")
    monkeypatch.setattr(DS, "ENABLED", False)
    called = []
    monkeypatch.setattr(DS, "check_satisfaction", lambda *a, **k: called.append(1) or {})
    events, served = _drive(orch._maybe_direct_serve("좋아요 버튼"))
    assert served is False and called == []


# --- EDU-27 직접서브 문서복원(writeback 동봉 + 세션 조인 폴백) ----------------

_NOTE = {"title": "T1", "what": "W1", "why": "Y1", "where": "Z1"}
_DESIGN_DOC = {"project_name": "하트 버튼", "description": "설명", "users": [],
              "features": [{"name": "f1", "description": "d", "priority": "mvp"}],
              "pages": [], "data_models": [], "user_flows": [], "strengths": [], "weaknesses": []}

_CAND_WITH_DOCS = {
    "title": "빨간 하트 좋아요 버튼", "session_id": "sess-1",
    "payload": {"kind": "code", "files": {"App.tsx": "export default function App(){return null}"},
               "docs": {"learning_notes": [_NOTE], "design_doc": _DESIGN_DOC}},
}

_CAND_SESSION_NO_DOCS = {
    "title": "회원가입 폼", "session_id": "sess-2",
    "payload": {"kind": "code", "files": {"App.tsx": "code"}},
}


def _accept_check(monkeypatch):
    monkeypatch.setattr(DS, "check_satisfaction",
                        lambda *a, **k: {"score": 97, "delta": False, "accept": True, "ok": True})


def test_direct_serve_restores_docs_from_payload(monkeypatch):
    """1순위: writeback 이 동봉한 payload.docs 로 재조회 없이 즉시 복원."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)
    orch = _orch(monkeypatch, "reuse", cand=_CAND_WITH_DOCS)
    events, served = _drive(orch._maybe_direct_serve("좋아요 버튼 만들어줘"))
    assert served is True
    assert orch.state.learning_notes == [_NOTE]
    assert orch.state.project.design_doc.project_name == "하트 버튼"
    assert orch.state.project.design_doc.features[0].name == "f1"
    assert orch._direct_served["docs_restored"] == 2  # 노트 1 + 설계문서 1


def test_direct_serve_session_join_fallback(monkeypatch):
    """2순위: payload.docs 없는 기존 자산은 candidate.session_id 로 세션 조인 폴백."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)
    seen_sid = []

    def fake_fetch(session_id):
        seen_sid.append(session_id)
        return {"learning_notes": [_NOTE], "design_doc": None} if session_id == "sess-2" else None

    monkeypatch.setattr(DS, "fetch_session_docs", fake_fetch)
    orch = _orch(monkeypatch, "reuse", cand=_CAND_SESSION_NO_DOCS)
    events, served = _drive(orch._maybe_direct_serve("회원가입 폼 만들어줘"))
    assert served is True
    assert seen_sid == ["sess-2"]
    assert orch.state.learning_notes == [_NOTE]
    assert orch._direct_served["docs_restored"] == 1


def test_direct_serve_no_docs_no_session_regression(monkeypatch):
    """docs 도 session_id 도 없는 기존 자산 — 예외 없이 코드만 서브(현행 동작)."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)
    orch = _orch(monkeypatch, "reuse")  # 모듈 상단 _CAND: session_id/docs 둘 다 없음
    events, served = _drive(orch._maybe_direct_serve("좋아요 버튼 만들어줘"))
    assert served is True
    assert orch.state.generated_code_map.get("App.tsx") == _CAND["payload"]["files"]["App.tsx"]
    assert orch.state.learning_notes == []
    assert orch._direct_served["docs_restored"] == 0


def test_direct_serve_session_join_miss_no_exception(monkeypatch):
    """session_id 는 있지만 조인 조회가 실패/빈손이어도 코드 서브는 그대로 유지."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)
    monkeypatch.setattr(DS, "fetch_session_docs", lambda sid: None)
    orch = _orch(monkeypatch, "reuse", cand=_CAND_SESSION_NO_DOCS)
    events, served = _drive(orch._maybe_direct_serve("회원가입 폼"))
    assert served is True
    assert orch.state.generated_code_map.get("App.tsx") == "code"
    assert orch.state.learning_notes == []
    assert orch._direct_served["docs_restored"] == 0


def test_direct_serve_docs_restore_exception_does_not_break_code_serve(monkeypatch):
    """문서 복원 중 예외가 나도(조회 인프라 장애 등) 코드 직접서브는 절대 깨지지 않는다."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)

    def boom(sid):
        raise RuntimeError("조회 인프라 장애")

    monkeypatch.setattr(DS, "fetch_session_docs", boom)
    orch = _orch(monkeypatch, "reuse", cand=_CAND_SESSION_NO_DOCS)
    events, served = _drive(orch._maybe_direct_serve("회원가입 폼"))
    assert served is True
    assert orch.state.generated_code_map.get("App.tsx") == "code"
    assert orch._direct_served["docs_restored"] == 0


def test_direct_serve_docs_killswitch_off(monkeypatch):
    """DIRECT_SERVE_DOCS=0 이면 payload.docs 가 있어도 복원하지 않는다(현행 동작 유지)."""
    _patch_lf(monkeypatch)
    _accept_check(monkeypatch)
    monkeypatch.setattr(DS, "DOCS_ENABLED", False)
    orch = _orch(monkeypatch, "reuse", cand=_CAND_WITH_DOCS)
    events, served = _drive(orch._maybe_direct_serve("좋아요 버튼"))
    assert served is True
    assert orch.state.learning_notes == []
    assert orch.state.project.design_doc.project_name == ""  # 기본값 그대로
    assert orch._direct_served["docs_restored"] == 0


# --- direct_serve.restore_docs / fetch_session_docs 단위 테스트 ---------------

def test_restore_docs_prefers_payload_over_session_join(monkeypatch):
    """payload.docs 가 있으면 fetch_session_docs(조회)는 아예 호출하지 않는다."""
    called = []
    monkeypatch.setattr(DS, "fetch_session_docs", lambda sid: called.append(sid) or None)

    class _S:
        def __init__(self):
            self.learning_notes = []

            class _P:
                design_doc = None
            self.project = _P()

    state = _S()
    n = DS.restore_docs(state, _CAND_WITH_DOCS)
    assert n == 2
    assert called == []


def test_fetch_session_docs_local_uses_registry_lib(monkeypatch):
    """RAG_UPSTREAM 미설정(로컬)이면 registry_lib.get_by_session 을 직접 호출한다."""
    import sys
    import types

    fake_registry = types.SimpleNamespace(
        get_by_session=lambda sid, kinds: [
            {"chunk_type": "learning_note", "title": "T", "content": "",
             "payload": {"title": "T", "what": "w", "why": "y", "where": "z"}},
        ] if sid == "sess-x" else [],
    )
    monkeypatch.setitem(sys.modules, "registry_lib", fake_registry)
    monkeypatch.delenv("RAG_UPSTREAM", raising=False)
    docs = DS.fetch_session_docs("sess-x")
    assert docs["learning_notes"] == [{"title": "T", "what": "w", "why": "y", "where": "z"}]
    assert docs["design_doc"] is None


def test_fetch_session_docs_no_session_id_returns_none():
    assert DS.fetch_session_docs("") is None
    assert DS.fetch_session_docs(None) is None


# --- 계측(3-tier 코호트) ------------------------------------------------------

class _FakeScoreClient:
    def __init__(self):
        self.scores = []

    def score_current_trace(self, name, value, data_type=None, **kw):
        self.scores.append({"name": name, "value": value, "data_type": data_type})


def _emit(monkeypatch, reuse_flag, direct_served):
    fake = _FakeScoreClient()
    monkeypatch.setattr(OS, "get_client", lambda: fake)
    orch = OS.StreamOrchestrator(api_key="")
    orch._reuse_flag = reuse_flag
    orch._direct_served = direct_served
    orch._emit_turn_scores("quick")
    return {s["name"]: s for s in fake.scores}


def test_tier_direct_serve(monkeypatch):
    scores = _emit(monkeypatch, {"decision": "reuse"}, {"accept": True, "score": 96})
    assert scores["재사용 티어 (reuse_tier)"]["value"] == "direct_serve"
    assert scores["직접서브 (direct_served)"]["value"] == 1
    assert scores["직접서브 만족도 (direct_serve_score)"]["value"] == 96


def test_tier_near_when_reject(monkeypatch):
    scores = _emit(monkeypatch, {"decision": "reuse"}, {"accept": False, "score": 20})
    assert scores["재사용 티어 (reuse_tier)"]["value"] == "near"
    assert scores["직접서브 (direct_served)"]["value"] == 0


def test_tier_cold(monkeypatch):
    scores = _emit(monkeypatch, None, None)
    assert scores["재사용 티어 (reuse_tier)"]["value"] == "cold"
    # 만족도 검증이 안 돈 턴은 direct_served 스코어를 찍지 않는다.
    assert "직접서브 (direct_served)" not in scores
