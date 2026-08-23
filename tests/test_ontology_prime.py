"""제안형 온톨로지 프라임 검증(#27) — "이전 대화와 비슷한 요청 → 맞는 학습노트·코드 제안".

이슈 #27의 핵심 검증: 사용자가 이전에 만들었던 대화와 유사한 걸 요청하면
  (A) 그에 맞는 **학습노트·코드**가 검색되어 제안되는가
  (B) **온톨로지 개념·선수학습 경로**가 함께 제안되는가
  (C) **매번 동일한 것을 그대로 주지 않는가** — '복사'가 아니라 '각색' 프레이밍 +
      이미 제안한 항목은 다음 턴에 회피(seen)

torch 없는 환경에서도 동작하도록 lexical(부분일치) 폴백으로 검증한다(개념 식별은
alias 기반이라 애초에 오프라인). 실 base 코퍼스는 읽기전용, 등록 스토어만 tmp 로 격리.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

from agent import reuse as RU  # noqa: E402

# 이전 대화(등록될 결과물). 질문 aliases("벽을 보고","멈추게")로 distance_sensing 매칭 +
# 제목/목표 토큰이 질문과 겹쳐 lexical 재사용 티어(reuse)에 들도록 구성.
PRIOR_GOAL = "벽을 보고 스스로 멈추게 하는 자동차"
QUERY = "벽을 보고 스스로 멈추게 하는 자동차 만들어줘"
UID = "test-uid-27"
SESS = "sess-prev-27"


@pytest.fixture()
def prior_conversation(tmp_path, monkeypatch):
    """등록 스토어를 tmp 로 격리하고 '이전 대화'(학습노트 + 코드)를 적재."""
    import registry_lib as R
    import search_lib

    monkeypatch.setattr(R, "REG_JSONL", str(tmp_path / "registered.jsonl"))
    monkeypatch.setattr(R, "REG_EMB", str(tmp_path / "registered_emb.npy"))
    # 캐시 리셋(실 스토어 흔적 제거) + search 통합 캐시 무효화
    R._meta = R._emb = R._seen = None
    R.VERSION += 1
    search_lib._combo_meta = None
    search_lib._combo_ver = -1

    R.register_learning_notes(
        SESS, UID, "blockly",
        [{"title": "벽을 보고 스스로 멈추게 하는 자동차의 눈",
          "what": "센서로 앞의 벽까지 거리를 재서 가까우면 자동차를 멈추게 한다.",
          "why": "충돌을 막으려면 거리를 먼저 알아야 하기 때문", "where": "자율주행 로봇"}],
        modi_keys=["tof", "motor"],
    )
    R.register_result(
        SESS, UID, "blockly",
        code_map={"main.py": "# 거리 감지 후 정지\nwhile True:\n    if dist()<10: stop()"},
        goal=PRIOR_GOAL, modi_keys=["tof", "motor"],
    )
    return R


def _by_kind(artifacts, kind):
    return [a for a in artifacts if (a.get("payload") or {}).get("kind") == kind]


def test_prior_conversation_suggests_notes_and_code(prior_conversation):
    """(A) 이전 대화의 학습노트+코드가 검색·제안되고 (B) 온톨로지 개념·선수학습이 함께 나온다."""
    sug = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=50)
    assert sug["ok"] is True

    # (B) 온톨로지: 핵심 개념 = 거리 감지, 선수학습 경로 존재
    assert sug["primary"] and sug["primary"]["key"] == "distance_sensing"
    assert sug["prerequisites"], "선수학습 경로가 비어 있으면 안 됨"
    assert "학습 경로" in sug["block"]
    assert "거리 감지" in sug["block"]

    # (A) 이전 대화 결과물: 학습노트 + 코드가 제안 목록에 존재
    notes = _by_kind(sug["artifacts"], "learning_note")
    codes = _by_kind(sug["artifacts"], "code")
    assert any("자동차" in (a.get("title") or "") for a in notes), "학습노트 제안 누락"
    assert codes, "코드 결과물 제안 누락"
    # 재사용 티어(reuse/review)만 — 콜드셀(register)은 제안하지 않음
    assert all(a.get("decision") in ("reuse", "review") for a in sug["artifacts"])


def test_frames_as_adapt_not_verbatim(prior_conversation):
    """(C-1) '그대로 복사'가 아니라 '이번 요청에 맞게 각색' 으로 제시한다(매번 동일 방지)."""
    sug = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=50)
    block = sug["block"]
    assert "각색" in block
    assert "복사하지" in block          # 그대로 복사 금지 명시
    assert "[온톨로지 제안" in block


def test_not_same_suggestion_every_time(prior_conversation):
    """(C-2) 이미 제안한 항목은 다음 턴에 회피되고, 다른 관련 자료를 계속 제안한다."""
    first = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=50)
    codes = _by_kind(first["artifacts"], "code")
    notes = _by_kind(first["artifacts"], "learning_note")
    assert codes and notes
    code_key = RU._art_key(codes[0])
    note_key = RU._art_key(notes[0])

    # 직전 턴에 코드를 이미 제안했다고 표시 → 다음 턴엔 그 코드를 밀어냄
    second = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID,
                                 seen={code_key}, top=50)
    keys2 = set(second["seen_keys"])
    assert code_key not in keys2, "이미 제안한 코드가 매번 다시 나오면 안 됨"
    assert note_key in keys2, "다른 관련 자료(학습노트)는 계속 제안돼야 함"
    assert second["artifacts"], "회피 후에도 제안할 자료가 남아야 함"


def test_proxy_mode_routes_to_upstream(monkeypatch):
    """배포(온프렘) 정합: RAG_UPSTREAM 설정 시 결과물 검색이 rag-search /api/search(MySQL/시맨틱)로 간다.

    지금 배포 서버는 프록시→MySQL(1233청크). 프라임도 인프로세스 sqlite 가 아니라 그 백엔드를 봐야 한다.
    """
    import httpx

    seen_url = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "results": [
                {"title": "코드: 업스트림 결과물", "decision": "reuse",
                 "chunk_id": 999, "payload": {"kind": "code", "files": {"a.py": "x"}}}]}

    def _fake_get(url, params=None, timeout=None):
        seen_url["url"] = url
        seen_url["params"] = params
        return _Resp()

    monkeypatch.setenv("RAG_UPSTREAM", "http://host.docker.internal:8100")
    monkeypatch.setattr(httpx, "get", _fake_get)

    sug = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=5)
    assert seen_url["url"].endswith("/api/search"), "프록시 모드인데 upstream /api/search 를 안 탐"
    assert any(a.get("title") == "코드: 업스트림 결과물" for a in sug["artifacts"]), \
        "업스트림(MySQL) 결과물이 제안에 반영돼야 함"


def test_graph_proxied_to_upstream(monkeypatch):
    """#27 원인1 해결: 프록시 모드면 그래프(선수학습·MODI·카드)도 rag-search /api/query 로 채운다.

    메인앱(:18080)은 그래프 DB 접근이 없어 로컬 graph_conn 이 비므로, artifacts 처럼 그래프도
    프록시로 가져와야 프로덕션에서 MODI·선수학습이 프라임에 실린다.
    """
    import httpx

    seen = {}

    class _Resp:
        status_code = 200

        def __init__(self, path):
            self._path = path

        def json(self):
            if "/api/query" in self._path:
                return {"ok": True, "primary": {"key": "distance_sensing", "label": "거리 감지 (ToF)"},
                        "prerequisites": [{"key": "variable", "label": "변수"}],
                        "related": [{"key": "loop", "label": "반복"}],
                        "modi_modules": ["초음파", "모터"],
                        "cards": [{"title": "벽 감지 카드"}]}
            return {"ok": True, "results": []}  # /api/search

    def _fake_get(url, params=None, timeout=None):
        seen[url.split("/api/")[-1]] = True
        return _Resp(url)

    monkeypatch.setenv("RAG_UPSTREAM", "http://host.docker.internal:8100")
    monkeypatch.setattr(httpx, "get", _fake_get)

    sug = RU.ontology_suggest("자동차가 벽 보고 멈추게", coding_type="blockly", user_id=UID, top=5)
    assert "query" in seen, "그래프가 upstream /api/query 를 안 탐"
    assert sug["ok"] is True
    assert sug["modi_modules"] == ["초음파", "모터"], "프록시 MODI 가 프라임에 반영돼야 함"
    assert sug["prerequisites"] and sug["cards"]
    assert "사용 하드웨어(MODI)" in sug["block"] and "초음파" in sug["block"]


def test_chat_build_path_injects_ontology_prime():
    """/chat 배선 검증: IMPLEMENT phase 빌드 직전 _reuse_block 이 온톨로지 프라임을 반환한다.

    orchestrator_stream 은 이 블록을 system_prompt 에 붙여(line 824~826) LLM 에 전달한다.
    → "/chat 하면 온톨로지가 반영되는가"의 결정적(LLM 無) 증거. 등록물 없어도 개념·선수학습은 주입.
    """
    from agent.models import Phase
    from agent.orchestrator_stream import StreamOrchestrator

    orch = StreamOrchestrator(api_key="", session_id="t")
    orch.state.coding_type = "blockly"
    orch.state.project.phase = Phase.IMPLEMENT  # 빌드 턴(재사용 게이트 발동 조건)

    block, msg = orch._reuse_block(QUERY, "blockly")
    assert block, "빌드 경로에서 프라임 블록이 주입돼야 함"
    assert "[온톨로지 제안" in block          # 온톨로지 프라임이 실제로 붙음
    assert "학습 경로" in block               # 선수학습 경로 주입 확인
    assert msg                                 # 상태 메시지도 생성(프론트 표시)
    # 세션 누적(seen)이 세팅돼 다음 턴 반복 제안 회피가 동작
    assert getattr(orch, "_suggested_keys", None) is not None


def test_unrelated_query_no_injection(prior_conversation):
    """개념도 유사 결과물도 없는 요청엔 프라임을 주입하지 않는다(정상 신규 생성)."""
    sug = RU.ontology_suggest("안녕하세요 반갑습니다 기분이 좋아요", coding_type="blockly",
                              user_id=UID, top=50)
    assert sug.get("ok") is False


def test_prime_includes_modi_and_cards():
    """#27 요구 #2: 프라임에 MODI 하드웨어 모듈(uses)·학습노트 카드(realized_by)가 포함된다."""
    sug = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=5)
    assert sug["ok"] is True
    assert sug["primary"]["key"] == "distance_sensing"
    # 반환 dict + 블록 양쪽에 노출(온톨로지 데이터에 uses/realized_by 엣지가 있는 개념)
    assert "modi_modules" in sug and "cards" in sug
    assert sug["modi_modules"], "MODI 모듈 관계가 비어 있으면 안 됨"
    assert "사용 하드웨어(MODI)" in sug["block"]
    if sug["cards"]:
        assert "참고 학습노트" in sug["block"]


def test_modi_cards_killswitch(monkeypatch):
    """킬스위치 off 면 MODI/카드는 제외되고 개념·선수학습은 그대로(무회귀 롤백 경로)."""
    monkeypatch.setattr(RU, "_PRIME_INCLUDE_MODI", False)
    monkeypatch.setattr(RU, "_PRIME_INCLUDE_CARDS", False)
    sug = RU.ontology_suggest(QUERY, coding_type="blockly", user_id=UID, top=5)
    assert sug["ok"] is True
    assert not sug["modi_modules"] and not sug["cards"]
    assert "사용 하드웨어(MODI)" not in sug["block"]
    assert "핵심 개념" in sug["block"]  # 기존 프라임은 유지


def test_reuse_block_equals_prime_service(monkeypatch):
    """계약(동일성): orchestrator._reuse_block 이 주입하는 블록 == prime_service.assemble_prime.

    이것이 "시뮬레이터(/api/simulate) == /chat" 을 보장하는 핵심 회귀 방어선이다.
    #EDU-27 재사용 시드편집은 세션이 필요해 stateless 시뮬레이터가 재현 못 하는 유일한 발산이므로,
    이 순수 프라임 동일성 검증에서는 킬스위치를 꺼(REUSE_SEED_EDIT=False) 시드 발산을 배제한다.
    """
    from agent.models import Phase
    from agent.orchestrator_stream import StreamOrchestrator
    from agent import prime_service
    monkeypatch.setattr(prime_service, "REUSE_SEED_EDIT", False)

    orch = StreamOrchestrator(api_key="", session_id="t")
    orch.state.coding_type = "blockly"
    orch.state.project.phase = Phase.IMPLEMENT
    block, msg = orch._reuse_block(QUERY, "blockly")

    res = prime_service.assemble_prime(QUERY, "blockly", is_modify=False, user_id="")
    assert res.block == block
    assert res.status_msg == msg

    # build_prime(엔드포인트 경로)도 동일 블록을 재현(코드 턴 분기)
    bundle = prime_service.build_prime(QUERY, "blockly", phase=Phase.IMPLEMENT,
                                       has_code=False, mode="quick", user_id="")
    assert bundle.code_action is True
    assert bundle.prime_block == block
