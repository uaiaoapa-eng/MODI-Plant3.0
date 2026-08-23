"""페르소나 온톨로지 도출 테스트 서버 (FastAPI).

질문 + 페르소나(학년/coding_type) → 개념 매칭 → 그래프 확장(선수학습·연관·MODI)
→ 학년 레벨 게이팅 → 재사용 학습노트(포괄 카드) 도출. Aurora/Redis 불필요(SQLite).

로컬:   uvicorn rag_demo_app:app --app-dir scripts --port 8100
브라우저: http://localhost:8100
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import registry_lib

from ontology_lib import (
    GRADE_LEVEL_CAP,
    chunks_for,
    concept_levels,
    graph_conn,
    load_seed,
    match_concepts,
    prerequisites,
    related,
    uses,
)
from search_lib import coverage as coverage_fn
from search_lib import search as search_fn
from search_lib import vector_enabled

# 커버리지 기본 질문 묶음(예시). 실제 로그로 교체 가능.
DEFAULT_COVERAGE_QUERIES = [
    "자동차가 벽을 보고 스스로 멈추게 하고 싶어요",
    "다이얼을 돌려서 색을 고르고 싶어요",
    "좋아요 버튼을 누르면 하트 색이 바뀌게",
    "느린 폰에서도 게임 속도가 똑같게 하려면",
    "카드 100개를 똑같이 만들고 싶어요",
    "두더지가 랜덤한 곳에서 튀어나오게",
    "스크롤 내려도 위 메뉴는 그대로 붙어있게",
    "깜깜해지면 불이 켜지는 장치",
    "숫자를 천 단위 콤마로 예쁘게",
    "화면 여러 개를 복사해서 똑같이 쓰고 싶어요",
]

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="edu-agent 온톨로지 도출 테스트")
_concepts = load_seed()
_levels = concept_levels(_concepts)
_label = {c["key"]: c["label"] for c in _concepts}


def derive(grade: int, coding_type: str, question: str) -> dict:
    matched = match_concepts(question, _concepts, top=3)
    if not matched:
        return {"ok": False, "reason": "질문에서 개념을 찾지 못했어요. 다른 표현으로 물어보세요.",
                "question": question}
    cap = GRADE_LEVEL_CAP.get(grade, 99)
    conn = graph_conn()
    try:
        primary, score = matched[0]
        plevel = _levels[primary]
        within = plevel <= cap

        # 학년 심화 차단 시: 선수학습(기초) 중심으로 도출 전환
        prereq = prerequisites(conn, primary)
        rel = related(conn, primary)
        cards = chunks_for(conn, primary, coding_type)
        # coding_type 필터로 0건이면 any로 폴백
        fallback = False
        if not cards:
            cards = chunks_for(conn, primary, "any")
            fallback = bool(cards)
        modi = uses(conn, primary)

        note = None
        if not within:
            foundational = [p for p in prereq if p["level"] <= cap]
            note = (f"'{_label[primary]}'는 {grade}학년엔 심화(L{plevel} > 상한 L{cap})예요. "
                    f"먼저 {', '.join(p['label'] for p in foundational[:3]) or '기초 개념'}부터 권장.")
        return {
            "ok": True,
            "question": question,
            "persona": {"grade": grade, "coding_type": coding_type, "level_cap": cap},
            "matched": [{"key": k, "label": _label[k], "level": _levels[k],
                         "score": round(s, 2), "within_grade": _levels[k] <= cap}
                        for k, s in matched],
            "primary": {"key": primary, "label": _label[primary], "level": plevel,
                        "within_grade": within},
            "prerequisites": prereq,
            "related": rel,
            "modi_modules": modi,
            "cards": cards,
            "coding_type_fallback": fallback,
            "note": note,
        }
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "rag_demo.html"))


@app.get("/api/query")
def query(question: str, grade: int = 4, coding_type: str = "any"):
    return JSONResponse(derive(grade, coding_type, question))


@app.get("/api/search")
def api_search(q: str, coding_type: str = "any", top: int = 8, user_id: str = "",
               intent: str = ""):
    """하이브리드 검색: 질문 → 매칭된 학습노트 청크(결정 태그 포함) 직접 노출.

    user_id(uuid) 지정 시 base(전역) + 내가 등록한 것으로 한정.
    intent 지정 시 의도 필터 — 재사용 게이트가 code 자산(implement_request)만
    검색할 때 사용(EDU-27 실전 미발동 수정).
    """
    return JSONResponse(search_fn(q, coding_type=coding_type, top=top,
                                  user_id=user_id or None, intent=intent or None))


@app.get("/api/coverage")
def api_coverage(coding_type: str = "any", intent: str = ""):
    """질문 묶음의 재사용/근접/등록 비율(%) — 일부 등록·일부 가져오기 게이팅 확인.

    intent=implement_request 지정 시 게이트 동형(code 자산 한정) 커버리지 —
    점수 기준 지표가 실전 발동률과 달리 낙관 보고되던 착시(EDU-27 진단)를 분리 측정.
    """
    return JSONResponse(coverage_fn(DEFAULT_COVERAGE_QUERIES, coding_type=coding_type,
                                    intent=intent or None))


class RegisterBody(BaseModel):
    question: str = ""
    title: str = ""
    content: str = ""
    coding_type: str | None = None
    concept_key: str | None = None
    user_id: str | None = None
    session_id: str | None = None


@app.post("/api/register")
def api_register(body: RegisterBody):
    """결과물(학습노트) 등록 → 저장 + 즉시 임베딩 → 바로 검색 가능(RAG 되먹임).

    검색이 '등록 후보'로 판정한 질문의 결과물을 여기로 보내면 다음 검색부터 히트.
    """
    ts = datetime.now(timezone.utc).isoformat()
    res = registry_lib.register(
        question=body.question, title=body.title, content=body.content,
        coding_type=body.coding_type, concept_key=body.concept_key,
        user_id=body.user_id, session_id=body.session_id, ts=ts,
    )
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


class WritebackBody(BaseModel):
    """chat 빌드 결과 묶음(#58). 프록시 모드 메인앱의 auto_save 가 한 번에 보낸다."""
    session_id: str = ""
    user_id: str | None = None
    coding_type: str | None = None
    learning_notes: list = []
    design_doc: dict | None = None
    code_map: dict | None = None
    modi_keys: list | None = None
    goal: str | None = None


@app.post("/api/writeback")
def api_writeback(body: WritebackBody):
    """chat 빌드 결과(노트+설계+코드)를 한 번에 되먹임 등록(#58).

    프록시 모드의 메인앱(:18080)은 torch 가 없어 auto_save 를 인프로세스로 돌리면
    0벡터가 되고, 검색 백엔드(rag-search)에도 안 닿아 재사용이 안 됐다. 세션 결과
    묶음을 torch 를 가진 이 서비스로 보내 register_learning_notes/register_result 를
    그대로 실행한다(파생·임베딩·저장은 두 헬퍼가 담당). 반환: 종류별 추가 건수.
    """
    notes_added = registry_lib.register_learning_notes(
        body.session_id, body.user_id, body.coding_type,
        body.learning_notes or [], modi_keys=body.modi_keys,
    )
    result_added = 0
    if body.design_doc or body.code_map:
        result_added = registry_lib.register_result(
            body.session_id, body.user_id, body.coding_type,
            design_doc=body.design_doc, code_map=body.code_map,
            modi_keys=body.modi_keys, goal=body.goal,
            learning_notes=body.learning_notes,
        )
    return JSONResponse({"ok": True, "notes_added": notes_added,
                         "result_added": result_added})


@app.get("/api/chunks")
def api_chunks(session_id: str, kind: str = "learning_note,design_doc"):
    """세션 조인 폴백(EDU-27 직접서브 문서복원) — 같은 세션의 학습노트/설계문서 청크 조회.

    직접서브(#84)가 payload.docs 없는(동봉 이전 등록분) 후보를 서브할 때, 후보의
    session_id 로 이 엔드포인트를 호출해 문서를 복원한다. MySQL(mysql_redis 백엔드)
    원천을 우선 조회하고, 없거나 실패하면 registry_lib 인프로세스 스토어로 폴백한다.
    """
    kinds = [k.strip() for k in kind.split(",") if k.strip()] or None
    chunks: list = []
    try:
        import store_mysql as M

        with M.connect() as conn:
            chunks = M.get_chunks_by_session(conn, session_id, kinds)
    except Exception:
        chunks = []
    if not chunks:
        try:
            chunks = registry_lib.get_by_session(session_id, kinds)
        except Exception:
            chunks = []
    return JSONResponse({"ok": True, "chunks": chunks})


class SessionSaveBody(BaseModel):
    """chat 세션 전문(#27 P3). 메인앱 auto_save 가 파일과 동시에 이 원천으로 이중쓰기."""
    session_id: str
    user_id: str = ""
    title: str | None = None
    description: str | None = None
    coding_type: str | None = None
    app_type: str | None = None
    phase: str | None = None
    raw: dict = {}


@app.post("/api/session/save")
def api_session_save(body: SessionSaveBody):
    """세션 전문을 MySQL sessions(raw JSON, 무손실)에 upsert — 대화/프로젝트 원천 저장."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            M.upsert_session(conn, body.model_dump())
        return JSONResponse({"ok": True})
    except Exception as e:  # MySQL 문제로 저장 경로가 깨지면 안 됨(호출측이 파일 폴백)
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


class UsageAddBody(BaseModel):
    """턴 사용량 1건(#133). 메인앱 /chat finally 가 pop_turn_usage() 회수 직후 이중쓰기."""
    ts: str
    subject: str
    user_id: str = ""
    session_id: str = ""
    mode: str = ""
    coding_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    weighted_tokens: int = 0
    trace_id: str = ""
    # 실제로 탄 LLM 경로: cli | api | api_fallback_cli (빈 값=미상, 구버전 호환)
    # ⚠ 설정값이 아니라 그 턴의 실제 경로다 — 폴백·모드 전환이 있으면 둘이 어긋난다.
    llm_mode: str = ""
    # 재사용 라우팅 결과: direct_serve | near | cold (빈 값=미상)
    reuse_tier: str = ""

    # ── 부하 관측(2026-08-22 40명 동시 수업) ──
    # 전부 기본값이 있다 — 구버전 앱이 안 보내도 400 으로 죽으면 안 되기 때문이다.
    # 관측 필드 때문에 사용량 기록 자체가 유실되면 본말전도다.
    started_at: str = ""      # 턴 시작(동접을 구간 겹침으로 세기 위해)
    duration_ms: int = 0
    ttft_ms: int = 0          # 첫 토큰까지 — 학생 체감 대기
    status: str = "ok"        # ok | error | aborted
    error_code: str = ""
    replica: str = ""         # edu-agent-1|2|3

    # ── 질문 유형·결과 ──
    intent: str = ""          # question|chat|modify_request|implement_request|...
    phase: str = ""           # design | implement
    outcome: str = ""         # code|blockly|doc|chat|none

    # ── 비용 절감 분석 ──
    reuse_top1: float = 0.0   # 재사용 후보 최고 점수(임계값 튜닝 근거)
    direct_served: int = 0
    docs_restored: int = 0

    # ── 접속 환경 ──
    user_agent: str = ""      # 원문(잘라서 보관) — 파싱 규칙이 바뀌어도 재해석 가능
    client_ip: str = ""       # 학교/집/모바일 구분용
    mem_mb: int = 0           # 턴 종료 시점 컨테이너 메모리(1g 상한 대비 감시)


class OpsEventBody(BaseModel):
    """턴이 만들어지지 않는 운영 사건 1건(session_busy·쿼터·차단·에러·재시작).

    ★ 이 경로가 없으면 40명 동시 수업에서 "몇 명이 튕겼나"를 알 수 없다. 거절은
      /chat 이 세션 락을 잡기 전에 return 해서 usage_turns 에 흔적이 안 남는다.
    """
    ts: str
    kind: str
    code: str = ""
    user_id: str = ""
    session_id: str = ""
    replica: str = ""
    detail: str = ""


@app.post("/api/usage/add")
def api_usage_add(body: UsageAddBody):
    """턴 사용량 1행을 MySQL usage_turns(분석 원천)에 적재 — 쿼터 카운터(Redis, 집행)와 분리.

    앱은 MySQL 직접 접근 금지 제약을 지키며 이 엔드포인트를 경유한다. 실패해도 호출측
    (/chat)을 막으면 안 되므로 예외를 삼키고 500 을 반환한다(호출측이 fail-open 으로 감싼다).
    """
    import store_mysql as M

    try:
        with M.connect() as conn:
            M.insert_usage_turn(conn, body.model_dump())
        return JSONResponse({"ok": True})
    except Exception as e:  # MySQL 문제로 /chat 이 막히면 안 됨(호출측 fail-open)
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/ops/event")
def api_ops_event(body: OpsEventBody):
    """운영 사건 1행을 ops_events 에 적재. 호출측(/chat 거절 경로)을 절대 막지 않는다."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            M.insert_ops_event(conn, body.model_dump())
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.get("/api/usage/report")
def api_usage_report(start: str = "", end: str = "", user_id: str = "",
                     limit_users: int = 25, project_offset: int = 0,
                     project_q: str = "", user_offset: int = 0,
                     project_limit: int = 25):
    """기간별 사용량·비용 리포트 — usage_turns(분석 원천) 집계.

    수업 하루가 끝난 뒤 "얼마 나왔나"를 답하기 위한 읽기 경로. 쓰기(/api/usage/add)만
    있고 조회가 없어 쌓인 데이터를 볼 방법이 없었다.

    비용 환산: weighted_tokens 는 기록 시점 가중치(출력×5, 캐시읽기×0.1, 캐시쓰기×1.25)로
    이미 계산돼 있고, 이 가중치는 Haiku 4.5 단가 비율과 일치한다 →
    **weighted_tokens / 1_000_000 = USD**. (입력 $1/MTok 기준으로 정규화된 값)

    - start/end: 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS' (KST). 비우면 오늘 하루.
    - user_id: 특정 사용자만. 비우면 전체.
    - limit_users: 상위 사용자 표에 포함할 인원수.
    """
    import store_mysql as M

    try:
        with M.connect() as conn:
            return JSONResponse(M.usage_report(
                conn, start=start, end=end, user_id=user_id, limit_users=limit_users,
                project_offset=project_offset, project_q=project_q,
                project_limit=max(1, min(project_limit, 200)),
                user_offset=user_offset))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


# ──────────────────────────────────────────────────────────────────────────────
# 일별 리포트 확정본 — 파일이 아니라 DB(usage_reports)가 원천
# ──────────────────────────────────────────────────────────────────────────────
#
# 라이브 조회(/api/usage/report)는 열 때마다 usage_turns 를 재집계한다. "지금 얼마"에는
# 맞지만 청구 근거로는 약하다 — 원본이 정리되거나 집계 로직이 바뀌면 과거 수치가
# 조용히 달라진다. 그래서 그날 값을 굳혀 두고 이후에는 이걸 읽는다.


@app.post("/api/usage/snapshot")
def api_usage_snapshot(day: str = "", llm_mode: str = "", with_insight: bool = False):
    """하루치를 집계해 usage_reports 에 굳힌다(멱등 — 다시 돌리면 덮어쓴다).

    with_insight=True 면 LLM 분석까지 만들어 함께 저장한다. 분석 생성이 실패해도
    **리포트 저장은 성공으로 둔다** — 부가 정보 때문에 청구 근거를 잃으면 안 된다.
    """
    import store_mysql as M

    if not day:
        return JSONResponse({"ok": False, "error": "day 가 필요합니다(YYYY-MM-DD, KST)"},
                            status_code=400)
    try:
        with M.connect() as conn:
            report = M.usage_report(conn, start=day, end=day, limit_users=50)
            if not report.get("ok"):
                return JSONResponse(report, status_code=500)

            insight = None
            insight_error = ""
            if with_insight:
                try:
                    import report_insight
                    insight = report_insight.generate(report, mode=llm_mode)
                    if not insight.get("ok"):
                        insight_error = insight.get("error", "")
                except Exception as e:
                    insight_error = str(e)[:200]
                    insight = None

            res = M.save_report(conn, day, report, llm_mode=llm_mode, insight=insight)
            conn.commit()
        return JSONResponse({**res, "insight_stored": bool(insight and insight.get("ok")),
                             "insight_error": insight_error})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


class InsightBody(BaseModel):
    """앱이 만들어 보낸 분석 본문 — 여기서는 **저장만** 한다.

    왜 여기서 생성하지 않나: 이 컨테이너에는 `agent/` 패키지도, anthropic SDK 도,
    Claude 인증도 없다(Dockerfile.rag-search 는 scripts/ 만 COPY). 넣으려면 이미지가
    커지고 API 키까지 들여야 한다. 생성은 그 셋을 이미 가진 앱이 하고, 여기는
    저장이라는 자기 역할만 한다.
    """
    day: str
    text: str = ""
    model: str = ""


@app.post("/api/usage/insight")
def api_usage_insight(body: InsightBody | None = None, day: str = "",
                      llm_mode: str = "", store: bool = True):
    """AI 분석 저장 — 본문(text)이 오면 그대로 굳힌다.

    text 없이 부르면 이 프로세스에서 생성을 시도하는데, 위 주석의 이유로 여기서는
    실패한다. 그 경우 **왜 안 되는지**를 그대로 돌려준다(조용한 실패 금지).
    """
    import store_mysql as M

    day = (body.day if body and body.day else day) or ""
    if not day:
        return JSONResponse({"ok": False, "error": "day 가 필요합니다"}, status_code=400)
    try:
        # ① 앱이 만들어 보낸 본문 → 저장만
        if body and body.text.strip():
            out = {"ok": True, "text": body.text, "model": body.model}
            if store:
                with M.connect() as conn:
                    M.save_report_insight(conn, day, out)
                    conn.commit()
            return JSONResponse({**out, "stored": bool(store)})

        # ② 본문 없이 온 호출(구버전 클라이언트 등) — 여기서 만들 수 없다.
        import report_insight
        with M.connect() as conn:
            stored = M.get_report(conn, day)
            report = (stored or {}).get("payload") or M.usage_report(
                conn, start=day, end=day, limit_users=50)
            out = report_insight.generate(report, mode=llm_mode)
            if out.get("ok") and store and stored:
                M.save_report_insight(conn, day, out)
                conn.commit()
        return JSONResponse(out, status_code=200 if out.get("ok") else 502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@app.get("/api/usage/snapshot")
def api_usage_snapshot_get(day: str = ""):
    """굳혀 둔 그날 확정본(payload + AI 분석)."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            row = M.get_report(conn, day)
        if not row:
            return JSONResponse({"ok": False, "error": "확정본이 없습니다"}, status_code=404)
        return JSONResponse({"ok": True, **row})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@app.get("/api/usage/snapshots")
def api_usage_snapshots(start: str = "", end: str = "", limit: int = 400):
    """확정본 목록(스칼라만). 목록 화면이 '어느 날이 굳어 있는지' 알기 위해 쓴다."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            rows = M.list_reports(conn, start=start, end=end, limit=limit)
        return JSONResponse({"ok": True, "count": len(rows), "items": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@app.get("/api/session/list")
def api_session_list(user_id: str = ""):
    """유저의 대화(세션) 리스트 — 파일 목록과 동일한 요약 형태로 MySQL 원천에서 제공."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            rows = M.list_sessions(conn, user_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
    projects = []
    for r in rows:
        raw = r.get("raw")
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        sid = r["session_id"]
        projects.append({
            "filename": f"{sid}.json", "session_id": sid,
            "title": r.get("title") or "제목 없음",
            "description": r.get("description") or "",
            "phase": r.get("phase") or "", "app_type": r.get("app_type") or "",
            "coding_type": r.get("coding_type") or "react",
            "has_code": bool(d.get("generated_code")),
            "updated_at": r.get("updated_at"),
        })
    return JSONResponse({"projects": projects})


@app.get("/api/session/get")
def api_session_get(session_id: str, user_id: str = ""):
    """세션 전문(raw) 반환 — 파일 로드와 동일한 페이로드."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            data = M.get_session(conn, session_id, user_id or None)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if data is None:
        return JSONResponse({"error": "파일을 찾을 수 없습니다"}, status_code=404)
    return JSONResponse(data)


@app.delete("/api/session/delete")
def api_session_delete(session_id: str, user_id: str = ""):
    """소유자 세션 삭제(MySQL 원천)."""
    import store_mysql as M

    try:
        with M.connect() as conn:
            n = M.delete_session(conn, session_id, user_id)
        return JSONResponse({"ok": True, "deleted": n})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.get("/health")
def health():
    return {"status": "ok", "vector_enabled": vector_enabled(),
            "registered": registry_lib.count()}


@app.get("/api/registry/stats")
def api_registry_stats():
    """등록 스토어 상태(#115) — 메인 앱(:18080) /api/registry/stats 위임 대상.

    메인 앱은 이 응답을 그대로 전달하므로 backend 를 정확히 보고해야 운영자가
    프록시(mysql_redis)를 local 로 오인하지 않는다(#115 증상). count 는
    backend=mysql_redis 일 때 실제 검색 인덱스(Redis 등록 문서 kchunk:reg:*)에서
    센다 — registry_lib.stats() 의 count 는 파일 스토어(registered.jsonl) 기준이라
    프록시 운영에서 실제 색인 수와 어긋나기 때문. Redis 조회 실패 시 500 대신 파일
    stats 로 폴백한다(가용성 우선, 앱 위임과 동형).
    """
    s = registry_lib.stats()  # {count, last_registered_at, backend}
    if s.get("backend") == "mysql_redis":
        try:
            import vector_redis as V

            reg = sum(1 for _ in V.client().scan_iter(match=f"{V.PREFIX}reg:*"))
            s = {**s, "count": reg}
        except Exception as exc:  # Redis 문제로 stats 가 깨지면 안 됨
            print(f"[registry] Redis 등록 수 집계 실패, 파일 stats 폴백: {exc}", flush=True)
    return JSONResponse({"ok": True, **s})
