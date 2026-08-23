import os
import re
import json
import hmac
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import sys
import socket
from urllib.parse import quote
import queue
import threading
from fastapi import FastAPI, Header, Query, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (StreamingResponse, FileResponse, JSONResponse,
                               HTMLResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# RAG 하이브리드 검색/등록 모듈은 scripts/ 에 있음 — 경로 추가 후 import.
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from langfuse import get_client
from agent.orchestrator_stream import StreamOrchestrator
from agent.errors import CATALOG, ErrorCode, error_event, error_response
from agent.quota import make_quota_store, quota_subject, TokenUsage
from agent.concurrency import make_session_lock
from agent.session_store import InMemorySessionStore, should_reload
from agent.cache import TTLCache
from agent.create import CreateOrchestratorAdapter
from curriculum import get_grade_band, get_lesson, list_grade_bands

load_dotenv()

# Sentry 관측 초기화 — 에러/성능/부하. FastAPI 자동 계측이 요청을 잡으려면 app 생성 전에 호출.
# SENTRY_DSN 이 없으면 no-op. 배포 환경(dev/onprem/release)은 SENTRY_ENVIRONMENT 로 구분한다.
from agent.observability import (  # noqa: E402  (init_sentry 는 load_dotenv 뒤/app 생성 전에 와야 함)
    init_sentry,
    set_session_context,
    capture_load_constraint,
    capture_chat_exception,
    stream_scope,
)

init_sentry("server")

# Langfuse 키가 있을 때만 flush 호출 (없으면 get_client는 no-op이지만 불필요 호출 방지)
_langfuse_enabled = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))

# PII 마스킹 훅 등록 — get_client()가 쓰는 싱글톤을 첫 사용 전에 구성한다.
# (redact-at-source 입력 경계의 2차 방어선: 시스템 프롬프트·도구 입력 등까지 가림.)
if _langfuse_enabled:
    from langfuse import Langfuse
    from agent.guardrails import langfuse_mask
    Langfuse(mask=langfuse_mask)

app = FastAPI(title="교육용 바이브코딩 에이전트")

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.join(_APP_ROOT, "web")
_WEB_INDEX = os.path.join(_WEB_DIR, "index.html")
_WEB_LMS = os.path.join(_WEB_DIR, "lms.html")

# P1 제품 셸. 웹 빌드 단계 없이 web/의 파일을 그대로 제공한다. check_dir=False라
# 백엔드만 배포한 환경도 import 시 실패하지 않고, / 요청에서 명확한 404를 반환한다.
app.mount("/static", StaticFiles(directory=_WEB_DIR, check_dir=False), name="product-static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# #132: 전역 예외 핸들러 — HTTP 경로에서 미처리 예외가 그대로(스택트레이스/경로 등)
# 클라이언트에 노출되던 걸 통일 스키마로 막는다. FastAPI 는 HTTPException(정상 4xx)은
# 이 핸들러로 보내지 않는다 — 여긴 진짜 unhandled(버그/장애) 만 잡는다.
# SSE(/chat 스트림) 는 응답 반환 후 생성되는 별도 경로라 여기서 안 잡힌다(#128 에서 이미 처리).
@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    capture_chat_exception(exc, path=str(request.url.path))
    return error_response(ErrorCode.INTERNAL)


# 세션 저장소 — 인메모리 + TTL/LRU eviction(S5, OOM 방지). 내부 서버용.
# 클라우드 전환 시 같은 인터페이스의 Redis 구현으로 교체(server 코드는 그대로).
# TTL/최대치는 환경변수로 조정 가능.
SESSION_TTL = float(os.getenv("SESSION_TTL_SECONDS", "86400"))   # 기본 24h
SESSION_MAX = int(os.getenv("SESSION_MAX", "500"))
sessions: InMemorySessionStore[StreamOrchestrator] = InMemorySessionStore(
    ttl_seconds=SESSION_TTL, max_size=SESSION_MAX
)

# Create 제품 세션은 기존 ProjectSession/오케스트레이터 상태를 복제하지 않는다. 여기에는
# v3 경로가 legacy /chat 계약으로 번역할 때 필요한 mode/coding_type 메타데이터만 둔다.
create_sessions: InMemorySessionStore[CreateOrchestratorAdapter] = InMemorySessionStore(
    ttl_seconds=SESSION_TTL, max_size=SESSION_MAX
)

# 같은 session_id 동시 턴 차단(S1). 멀티워커면 REDIS_URL 로 분산 락, 아니면 인메모리.
# 락 TTL 은 한 턴 최대 시간(CLI ~600s)보다 넉넉히(기본 900s) — 워커 사망 시 자동 해제.
session_locks = make_session_lock(
    os.getenv("REDIS_URL", ""),
    ttl_ms=int(os.getenv("SESSION_LOCK_TTL_MS", "900000")),
)

# ── 사용자 토큰 쿼터 게이트(#131) ────────────────────────────────────────
# 킬스위치 기본 off — 배포 후 관측하고 켠다(docs/design/token-quota-and-error-structure.md).
QUOTA_ENABLED = os.getenv("QUOTA_ENABLED", "false").strip().lower() in ("true", "1", "yes")
QUOTA_SCOPE = os.getenv("QUOTA_SCOPE", "user").strip().lower()  # "user" | "session"
QUOTA_DAILY_WEIGHTED_TOKENS = int(os.getenv("QUOTA_DAILY_WEIGHTED_TOKENS", "2000000"))
# 일일 턴(대화 요청) 상한 — "한 사용자당 하루 N번". 0=off(턴 차원 미적용).
# 토큰 한도와 병행: 둘 중 하나라도 소진되면 차단. 권장 20~30(설계 프로젝트 한 개가
# 질문→생성→수정 반복으로 금방 5턴을 넘기므로 5는 정상 사용자 차단 위험). QUOTA_ENABLED 종속.
QUOTA_DAILY_MAX_TURNS = int(os.getenv("QUOTA_DAILY_MAX_TURNS", "0"))
QUOTA_WEIGHT_OUTPUT = float(os.getenv("QUOTA_WEIGHT_OUTPUT", "5.0"))
QUOTA_WEIGHT_CACHE_READ = float(os.getenv("QUOTA_WEIGHT_CACHE_READ", "0.1"))
QUOTA_WEIGHT_CACHE_CREATION = float(os.getenv("QUOTA_WEIGHT_CACHE_CREATION", "1.25"))
# IP 보조 상한(0=off) — uuid 교체로 쿼터를 우회하는 걸 완화. NAT 공유 IP 오차단을 피하려
# user 한도의 10배 권장(운영자 판단으로 env에서 조정).
QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP = int(os.getenv("QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP", "0"))
# true면(NPM 등 신뢰 프록시 뒤에서만) X-Forwarded-For 첫 IP를 사용 — 신뢰 프록시 없이 켜면
# 헤더 위조로 IP 상한이 무력화된다.
QUOTA_TRUST_PROXY = os.getenv("QUOTA_TRUST_PROXY", "false").strip().lower() in ("true", "1", "yes")
# 운영자 차단 목록 — 콤마 구분, "u:abcd" / "s:abcd" / "ip:1.2.3.4" 형식(quota_subject/IP subject와 동일 포맷).
QUOTA_DENY_SUBJECTS = {s.strip() for s in os.getenv("QUOTA_DENY_SUBJECTS", "").split(",") if s.strip()}
# #147: 차단 스트림이 안내 문구를 렌더 가능한 type:"token" 으로도 병행 전송할지(기본 on).
# type:"error"(구조화 메타)를 렌더하지 않는 외부 프론트에서 화면이 비지 않게 하는 폴백.
# 미래 프론트가 error 를 네이티브 렌더하면 false 로 꺼서 중복 렌더를 막는다.
SSE_ERROR_AS_TOKEN = os.getenv("SSE_ERROR_AS_TOKEN", "true").strip().lower() in ("true", "1", "yes")

quota_store = make_quota_store(os.getenv("REDIS_URL", ""))

# #133: usage_turns 기록(분석 원천)의 ts 는 서버 TZ 와 무관하게 KST 로 고정(quota.py:_KST 동형).
#
# ⚠ 다만 **저장되는 값은 UTC 다.** 여기서 만든 tz-aware ISO(+09:00)를 MySQL 이 받아
#   세션 타임존(UTC)으로 변환해 넣기 때문이다. 2026-08-21 실측: KST 20:11:51 턴이
#   11:11:51 로 적재됐다. 저장을 UTC 로 두는 것 자체는 의도한 규약이고(서버 로케일이
#   바뀌어도 값이 안 흔들린다), 대신 **조회하는 쪽이 KST 로 되돌릴 책임**을 진다
#   (store_mysql 의 CONVERT_TZ). 여기를 naive KST 로 바꾸면 이미 쌓인 UTC 행과 섞여
#   구분이 불가능해지므로 절대 바꾸지 않는다.
_KST = ZoneInfo("Asia/Seoul")

# 어느 레플리카가 처리했는가. compose 가 container_name 을 edu-agent-{1,2,3} 으로 고정하고
# 그게 곧 컨테이너 호스트명이라, 추가 설정 없이 이 한 줄로 식별된다.
# 부하 중 "특정 대만 느리다/죽는다"를 가르려면 턴마다 이 값이 있어야 한다.
_REPLICA = (os.getenv("REPLICA_NAME") or socket.gethostname() or "")[:24]


def _mem_mb() -> int:
    """이 컨테이너가 지금 쓰는 메모리(MB). 못 읽으면 0.

    왜 턴마다 남기나: 레플리카에 1g 상한을 걸어 뒀다(docker-compose). 상한에 닿으면
    그 컨테이너만 재시작되고 나머지가 서빙하지만, **닿기 전에 보여야** 대회 중에
    손을 쓸 수 있다. 재시작된 뒤에 아는 건 늦다.

    cgroup 파일 한 줄을 읽는다 — 마이크로초 단위라 핫 경로에 둬도 무해하고,
    docker 소켓 같은 특권도 필요 없다(v2 우선, v1 폴백).
    """
    for path in ("/sys/fs/cgroup/memory.current",                    # cgroup v2
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes"):     # cgroup v1
        try:
            with open(path, "r") as f:
                return int(f.read().strip()) // (1024 * 1024)
        except (OSError, ValueError):
            continue
    return 0


def _utc_stamp() -> str:
    """원장에 넣을 시각 — **오프셋 없는 UTC 문자열**.

    왜 tz-aware ISO(+09:00)를 안 쓰는가(2026-08-21 실측):
        같은 형식으로 보낸 두 컬럼이 **서로 다르게 저장됐다.**
            ts         → 2026-08-21 11:50:32  (UTC 로 변환됨)
            started_at → 2026-08-21 20:50:30  (KST 그대로)
        그 결과 시작이 종료보다 9시간 뒤가 되어 동접 계산이 통째로 0 이 됐다.
        MySQL 의 암묵적 오프셋 해석에 기대는 한 이런 어긋남을 막을 수가 없다.

    그래서 변환 여지를 없앤다 — 앱이 UTC 로 확정해 보내고 DB 는 받은 값을 그대로 넣는다.
    값 자체는 기존과 동일하다(기존 행도 UTC 였다). 규약만 명시적으로 바뀐다.
    표시 시점의 KST 변환은 조회 쪽(store_mysql 의 CONVERT_TZ)이 계속 담당한다.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _quota_client_ip(request: Request) -> str:
    """요청 IP 추출. QUOTA_TRUST_PROXY=true(신뢰 프록시 뒤)일 때만 XFF 첫 IP를 신뢰한다."""
    if QUOTA_TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


_QUOTA_IP_SAFE_RE = re.compile(r"[^0-9a-fA-F:.]")


def _quota_ip_subject(ip: str) -> str:
    """IP 보조 상한/차단 목록용 subject("ip:1.2.3.4" 형식 — QUOTA_DENY_SUBJECTS 예시와 동일 포맷).

    _safe_id([^A-Za-z0-9_-] 제거)를 그대로 쓰면 IPv4 의 '.'이 지워져 서로 다른 IP가
    같은 subject로 뭉개진다(예: "1.2.3.4"→"1234", "12.3.4"→"1234" 충돌) — 그래서
    IPv4/IPv6 문자(숫자·hex·':'·'.')만 남기는 전용 정규식으로 정규화한다(45자 절단
    — IPv6 최대 길이. 스푸핑된 XFF 헤더의 임의 문자열 방지 목적은 동일).
    """
    sid = _QUOTA_IP_SAFE_RE.sub("", ip or "")[:45]
    return f"ip:{sid}" if sid else ""


def _quota_seconds_until(reset_at: datetime) -> int:
    """다음 리셋(KST 자정)까지 남은 초 — retry_after 값."""
    now = datetime.now(reset_at.tzinfo) if reset_at.tzinfo else datetime.now()
    return max(0, int((reset_at - now).total_seconds()))

# SSE 응답 공통 헤더.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _sse_chunk(event: dict) -> str:
    """이벤트 1건을 SSE data 라인으로 직렬화(서로게이트 안전)."""
    chunk = json.dumps(event, ensure_ascii=False)
    chunk = chunk.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    return f"data: {chunk}\n\n"


def _quarantine_corrupt_file(filepath: str):
    """저장 중 크래시 등으로 깨진 세션 파일을 .corrupt로 옮긴다(보존하되 로딩은 막지 않음)."""
    try:
        os.replace(filepath, filepath + ".corrupt")
    except OSError:
        pass


def _disk_mtime(filepath: str) -> float:
    return os.path.getmtime(filepath) if os.path.exists(filepath) else 0.0


def _hydrate_from_upstream(session_id: str, user_id: str, filepath: str) -> bool:
    """파일이 없을 때 MySQL 원천(rag-search /api/session/get)에서 세션 전문을 받아 파일로 내려받는다.

    멀티박스/파일 유실 상황에서도 /chat 이 프로젝트를 MySQL 통해 이어갈 수 있게 한다(#27 P3).
    materialize 후엔 기존 파일 복원 경로(_restore_state_from_file)·캐시 정합이 그대로 성립. 성공 시 True.
    """
    import httpx
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{_RAG_UPSTREAM}/api/session/get",
                      params={"session_id": session_id, "user_id": user_id or ""})
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict) or "session_id" not in data:
            return False  # 404 {error:...} 등 — 원천에도 없음
    except Exception as e:
        print(f"[restore] 업스트림 하이드레이트 실패({e})", flush=True)
        return False
    _write_session_json(data, filepath)  # 파일로 내려받아 이후 복원/캐시와 정합
    return True


def get_orchestrator(session_id: str, user_id: str = "") -> StreamOrchestrator:
    filepath = _session_path(user_id, session_id)

    def _build() -> StreamOrchestrator:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        # 디스크에 저장된 세션이 있으면 자동 복원 (해당 유저 폴더 안에서만 탐색).
        # 파일이 없고 프록시 모드면 MySQL 원천에서 먼저 내려받아(hydrate) 파일화한다(#27 P3).
        if not os.path.exists(filepath) and _RAG_UPSTREAM:
            _hydrate_from_upstream(session_id, user_id, filepath)
        orch = StreamOrchestrator(api_key=api_key, session_id=session_id)
        if os.path.exists(filepath):
            try:
                _restore_state_from_file(orch, filepath)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                # 저장 중 크래시로 파일이 깨졌을 때 500 대신 빈 세션으로 시작.
                # 깨진 파일은 격리해 두면 다음 저장이 깨끗한 내용으로 덮어쓴다.
                print(f"[restore] 세션 {session_id} 복원 실패({e}); 빈 세션으로 시작", flush=True)
                _quarantine_corrupt_file(filepath)
                orch = StreamOrchestrator(api_key=api_key, session_id=session_id)
        # 새 세션이면 요청자를 소유자로 — auto_save가 올바른 유저 폴더에 쓰도록.
        if not orch._user_id:
            orch._user_id = user_id
        # 멀티워커 stale 방지용으로 디스크 적재 시점 기록.
        orch._loaded_mtime = _disk_mtime(filepath)
        return orch

    # 멀티워커(공유 볼륨): 다른 워커가 더 최신 상태를 저장했으면 캐시를 버리고 재로딩한다.
    cached = sessions.get(session_id)
    if cached is not None and not should_reload(getattr(cached, "_loaded_mtime", None), _disk_mtime(filepath)):
        return cached
    orch = _build()
    sessions.set(session_id, orch)
    return orch


def get_user_id(user_id: str = Query(""), x_user_id: str = Header("")) -> str:
    """익명 디바이스 uuid를 쿼리(user_id) 또는 헤더(X-User-Id) 어느 쪽으로 와도 받는다.

    프론트는 CORS preflight를 피하려 쿼리로 보내지만(커스텀 헤더는 preflight 유발),
    헤더로 오는 호출도 그대로 수용해 전송 방식에 안 묶이게 한다.
    """
    return user_id or x_user_id


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    mode: str = "design"          # "design" | "quick"
    coding_type: str = "react"    # "react" | "blockly" | "hybrid"
    # 미리보기(Sandpack)에서 발생한 런타임 에러 — 프론트가 실어 보내면 수정 턴의
    # 시스템 프롬프트에 주입되어 모델이 '실제 에러'를 근거로 고친다(블라인드 디버깅 방지).
    # 빌드 검증은 컴파일만 잡고 런타임 크래시는 프론트에서만 보이기 때문에 이 채널이 필요.
    runtime_error: str = ""


class CreateSessionRequest(BaseModel):
    coding_type: str = "react"


class CreateChatRequest(BaseModel):
    message: str
    runtime_error: str = ""


@app.get("/", include_in_schema=False)
async def product_index():
    """Serve the dependency-free MODI Planet product shell when it is present."""
    if not os.path.isfile(_WEB_INDEX):
        raise HTTPException(status_code=404, detail="web/index.html is not installed")
    return FileResponse(_WEB_INDEX)


@app.get("/lms", include_in_schema=False)
async def product_lms():
    """Serve the curriculum player supplied by the product frontend."""
    if not os.path.isfile(_WEB_LMS):
        raise HTTPException(status_code=404, detail="web/lms.html is not installed")
    return FileResponse(_WEB_LMS)


@app.get("/api/v3/home")
async def v3_home():
    return JSONResponse({
        "product": {"name": "MODI Planet", "version": "3.0"},
        "modes": [
            {
                "id": "learn",
                "title": "교육과정으로 배우기",
                "description": "학교 수업에 맞춰 AI와 프로젝트를 만들어요.",
            },
            {
                "id": "create",
                "title": "자유롭게 만들기",
                "description": "AI와 이야기하며 나만의 Web/MODI 프로젝트를 만들어요.",
            },
        ],
        "recent_projects_endpoint": "/projects",
    })


@app.get("/api/v3/curriculum")
async def v3_curriculum():
    return JSONResponse({"grade_bands": list_grade_bands()})


@app.get("/api/v3/curriculum/{grade_band}")
async def v3_curriculum_grade_band(grade_band: str):
    band = get_grade_band(grade_band)
    if band is None:
        raise HTTPException(status_code=404, detail="unknown grade_band")
    return JSONResponse(band)


@app.get("/api/v3/curriculum/{grade_band}/{lesson_no}")
async def v3_curriculum_lesson(grade_band: str, lesson_no: int):
    lesson = get_lesson(grade_band, lesson_no)
    if lesson is None:
        raise HTTPException(status_code=404, detail="unknown lesson")
    return JSONResponse(lesson)


class SimulateRequest(BaseModel):
    """/api/simulate 입력 — /chat 의 온톨로지 RAG 분기·프라임을 LLM 없이 재현(옵션 A)."""
    message: str
    mode: str = "quick"            # "design" | "quick"
    coding_type: str = "react"     # "react" | "blockly"
    phase: str = "implement"       # "design" | "implement" | "verify"
    has_code: bool = False          # 이미 산출물(코드)이 있는 세션인가 = 수정 턴 여부
    is_clarification_answer: bool = False


@app.post("/api/simulate")
async def simulate_prime(req: SimulateRequest, user_id: str = Depends(get_user_id)):
    """LLM 없이 /chat 의 온톨로지 RAG 분기·프라임을 그대로 재현(옵션 A).

    /chat 과 **동일한** prime_service(resolve_intent + assemble_prime)를 태워, 이 입력에서
    온톨로지 RAG가 발동하는지(code_action)와 실제 주입되는 프라임(prime_block)을 반환한다.
    비용 0(LLM 미호출). 8091 뷰어의 '모드 시뮬레이션' 패널이 이 엔드포인트를 호출한다.
    """
    from agent import prime_service
    from agent.models import Phase
    phase_map = {"design": Phase.DESIGN, "implement": Phase.IMPLEMENT, "verify": Phase.VERIFY,
                 "설계": Phase.DESIGN, "구현": Phase.IMPLEMENT, "검증": Phase.VERIFY}
    phase = phase_map.get((req.phase or "implement").lower(), Phase.IMPLEMENT)
    bundle = prime_service.build_prime(
        req.message, req.coding_type, phase=phase, has_code=req.has_code,
        mode=req.mode, user_id=user_id or None,
        is_clarification_answer=req.is_clarification_answer,
    )
    og = bundle.ontology or {}
    gate = bundle.reuse_gate
    return JSONResponse({
        "ok": True,
        "message": req.message,
        "persona": {"mode": req.mode, "coding_type": req.coding_type,
                    "phase": req.phase, "has_code": req.has_code},
        "intent": bundle.intent,
        "code_action": bundle.code_action,   # 온톨로지 RAG 발동 여부(핵심 분기)
        "injected": bundle.injected,          # 실제 프라임 주입 여부
        "ontology": {
            "primary": og.get("primary"),
            "prerequisites": og.get("prerequisites") or [],
            "related": og.get("related") or [],
            "modi_modules": og.get("modi_modules") or [],
            "cards": og.get("cards") or [],
            "artifacts": og.get("artifacts") or [],
        },
        "reuse_gate": ({"decision": gate.get("decision"), "top1": gate.get("top1"),
                        "source_title": (gate.get("candidate") or {}).get("title")}
                       if gate else None),
        "prime_block": bundle.prime_block,   # /chat 이 실제 프롬프트에 붙이는 문자열 그대로
        "status_msg": bundle.status_msg,
    })


@app.get("/simulate")
def simulate_page():
    """모드 시뮬레이터 테스트 페이지(정적) — /api/simulate 를 호출해 분기·프라임을 표시.

    8091 뷰어 대신(또는 함께) 이 페이지를 :18080/simulate 로 바로 열 수 있다.
    """
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "simulate.html"))


@app.post("/chat")
async def chat(req: ChatRequest, request: Request, user_id: str = Depends(get_user_id)):
    # 부하 관측 기준점 — **엔드포인트 진입 시각**으로 잡는다(스트림 시작이 아니라).
    # 부하가 걸리면 StreamingResponse 를 돌려준 뒤 제너레이터가 threadpool 에서 실제로
    # 소비되기까지 대기가 생긴다. 그 대기야말로 학생이 체감하는 지연이므로 포함해야 한다.
    _t_entry = time.monotonic()
    _started_at = _utc_stamp()
    orch = get_orchestrator(req.session_id, user_id)
    # 턴 시작 시점의 산출물 크기 — 끝나고 비교해 "이 턴이 무엇을 만들었나"를 판정한다.
    _fp_before = _artifact_fingerprint(orch)
    # 익명 디바이스 uuid(쿼리 user_id 또는 X-User-Id 헤더). 이 턴이 만든/이어가는
    # 프로젝트의 소유자로 기록되고(auto_save가 projects/<uid>/에 저장), Langfuse에도 태깅.
    if user_id:
        orch._user_id = user_id

    # ── #131: 쿼터 게이트(락 acquire 전, 킬스위치 QUOTA_ENABLED 기본 off — 완전 no-op) ──
    quota_subj = ""
    if QUOTA_ENABLED:
        try:
            quota_subj = quota_subject(user_id, req.session_id, scope=QUOTA_SCOPE)
            ip_subj = ""
            if QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP > 0 or QUOTA_DENY_SUBJECTS:
                ip_subj = _quota_ip_subject(_quota_client_ip(request))

            if quota_subj in QUOTA_DENY_SUBJECTS or (ip_subj and ip_subj in QUOTA_DENY_SUBJECTS):
                capture_load_constraint(
                    "blocked", session_id=req.session_id, mode=req.mode, subject=quota_subj)
                record_ops_event("blocked", user_id=user_id, session_id=req.session_id,
                                 detail=f"subject={quota_subj}")

                def blocked_stream():
                    if SSE_ERROR_AS_TOKEN:
                        yield _sse_chunk({"type": "token",
                                          "text": CATALOG[ErrorCode.BLOCKED].user_message})
                    yield _sse_chunk(error_event(ErrorCode.BLOCKED))
                    yield _sse_chunk({"type": "done"})
                return StreamingResponse(blocked_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

            # quota_subj 스냅샷은 한 번만 읽는다 — 같은 요청 안에서 두 번 HGETALL 하면
            # 락 acquire 전 지연이 늘어 동시 요청 간 session_busy 경합 창이 넓어지고
            # (breadcrumb: 같은 키 HGETALL ×2 → SET 실패), 두 스냅샷 사이 turns 가
            # 바뀔 수 있는 TOCTOU 도 생긴다.
            usage_snapshot = quota_store.used(quota_subj)
            store_used = usage_snapshot.weighted(
                QUOTA_WEIGHT_OUTPUT, QUOTA_WEIGHT_CACHE_READ, QUOTA_WEIGHT_CACHE_CREATION)
            over_limit = store_used >= QUOTA_DAILY_WEIGHTED_TOKENS
            if not over_limit and QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP > 0 and ip_subj:
                ip_used = quota_store.used(ip_subj).weighted(
                    QUOTA_WEIGHT_OUTPUT, QUOTA_WEIGHT_CACHE_READ, QUOTA_WEIGHT_CACHE_CREATION)
                over_limit = ip_used >= QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP
            # 턴 상한(사용자별) — "하루 N번". 토큰 한도와 병행: 둘 중 하나라도 소진되면 차단.
            if not over_limit and QUOTA_DAILY_MAX_TURNS > 0:
                over_limit = usage_snapshot.turns >= QUOTA_DAILY_MAX_TURNS

            if over_limit:
                # 잔여>0 판정 — 사용자 일 쿼터(또는 IP 보조 상한) 소진.
                capture_load_constraint(
                    "user_quota", session_id=req.session_id, mode=req.mode, subject=quota_subj)
                record_ops_event("user_quota", user_id=user_id, session_id=req.session_id,
                                 detail=f"subject={quota_subj}")
                retry_after = _quota_seconds_until(quota_store.reset_at())

                def quota_exceeded_stream():
                    if SSE_ERROR_AS_TOKEN:
                        yield _sse_chunk({"type": "token",
                                          "text": CATALOG[ErrorCode.QUOTA_EXCEEDED].user_message})
                    yield _sse_chunk(error_event(ErrorCode.QUOTA_EXCEEDED, retry_after=retry_after))
                    yield _sse_chunk({"type": "done"})
                return StreamingResponse(quota_exceeded_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
        except Exception as quota_err:
            # 쿼터 판정 자체가 실패해도 /chat 은 막지 않는다(fail-open, auto_save 가드와 동일 원칙).
            capture_chat_exception(quota_err, session_id=req.session_id, stage="quota")

    # S1 가드: 같은 세션이 이미 처리 중이면 즉시 거절(대기 큐를 만들지 않아 스레드가 안 쌓임).
    # 새 탭·더블클릭·프론트 중복 전송이 orchestrator 상태를 동시에 건드려 깨지는 걸 막는다.
    if not session_locks.acquire(req.session_id):
        # 부하 제약 신호: 같은 세션이 이미 처리 중이라 거절됨(동시성 상한 도달).
        capture_load_constraint("session_busy", session_id=req.session_id, mode=req.mode)
        # ★ 40명 동시 수업에서 "몇 명이 튕겼나"가 곧 이 숫자다. 이 경로는 락을 잡기 전에
        #   return 하므로 usage_turns 에는 한 줄도 안 남는다 — 여기서 남기지 않으면 없다.
        record_ops_event("session_busy", user_id=user_id, session_id=req.session_id,
                         detail=f"mode={req.mode}")

        def busy_stream():
            if SSE_ERROR_AS_TOKEN:
                yield _sse_chunk({"type": "token",
                                  "text": CATALOG[ErrorCode.SESSION_BUSY].user_message})
            yield _sse_chunk(error_event(ErrorCode.SESSION_BUSY))
            yield _sse_chunk({"type": "done"})
        return StreamingResponse(busy_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def event_stream():
        # 스트림 제너레이터는 엔드포인트 리턴 후(자동 트랜잭션 종료 후) threadpool 에서
        # next() 단위로 실행되므로, 이 스트림 전용 isolation scope 로 감싼다 — 그러지 않으면
        # 스트림이 오래 실행되는 동안 스레드가 재사용되면서 동시에 처리된 다른(훨씬 빈번한)
        # 요청(예: /health 폴링)의 스코프/트랜잭션이 남아 있다가 뒤섞여 아래 capture_chat_exception
        # 이벤트의 culprit/세션 태그가 엉뚱한 라우트로 찍힌다(EDU-AGENT-H).
        with stream_scope():
            # 이 스트림에서 터지는 모든 이벤트에 세션 식별/태그가 붙도록 스코프를 먼저 세팅.
            set_session_context(req.session_id, user_id, mode=req.mode, coding_type=req.coding_type)
            # 이 턴의 결과 라벨. 예외 경로에서 갱신되고 finally 가 원장에 싣는다.
            # 리스트로 두는 이유: 제너레이터 안에서 재바인딩하면 finally 가 못 보기 때문에
            # 가변 컨테이너로 공유한다(nonlocal 은 중첩 제너레이터에서 지저분해진다).
            _turn_stat = {"status": "ok", "error_code": "", "ttft_ms": 0, "done": False}
            try:
                for event in orch.chat_stream(req.message, mode=req.mode, coding_type=req.coding_type,
                                              runtime_error=req.runtime_error):
                    # 학생 체감 대기는 총 소요가 아니라 **첫 글자가 뜨기까지**가 지배한다.
                    # 90초 걸리는 턴도 3초에 글자가 시작되면 기다리지만, 20초 동안 아무것도
                    # 안 나오면 새로고침한다. 그래서 총 소요와 따로 잰다.
                    if isinstance(event, dict):
                        if event.get("type") == "token" and not _turn_stat["ttft_ms"]:
                            _turn_stat["ttft_ms"] = round((time.monotonic() - _t_entry) * 1000)
                        elif event.get("type") == "error":
                            # ★ 스트림 **안에서** 방출되는 실패(LLM_AUTH·LLM_QUOTA 등)는
                            #   예외가 아니라 이벤트로 나가고 done 없이 끝난다. 잡지 않으면
                            #   '중단(학생 이탈)'로 분류돼 원인을 정반대로 읽게 된다 —
                            #   구독 쿼터가 수업 중에 소진되면 전 학생이 실패하는데
                            #   리포트에는 "다들 기다리다 나갔다"로 보인다.
                            #   코드 이름을 하드코딩하지 않고 이벤트를 그대로 신뢰한다.
                            _turn_stat["status"] = "error"
                            _turn_stat["error_code"] = str(event.get("code") or "")[:32]
                            record_ops_event("error", code=_turn_stat["error_code"],
                                             user_id=user_id, session_id=req.session_id,
                                             detail=str(event.get("message") or "")[:200])
                        elif event.get("type") == "done":
                            # 끝까지 갔다는 표시. 이게 없이 finally 에 도달하면 학생이
                            # 기다리다 새로고침·이탈한 턴(aborted)이다 — 부하 분석에서
                            # "실패"와 "포기"는 원인이 달라 반드시 갈라야 한다.
                            _turn_stat["done"] = True
                    # Claude CLI 스트림에서 이모지 등이 chunk 경계에 걸리면
                    # 서로게이트 문자가 섞일 수 있다 → UTF-8 인코딩 실패 방지
                    yield _sse_chunk(event)
                    # 산출물이 확정되면 즉시 디스크에 박아 둔다 — 뒤따르는 후처리(학습노트·
                    # 흐름도) 도중에 죽어도 학생이 만든 결과가 남는다. 사용자에게 이벤트를
                    # 먼저 보낸 뒤 저장해 체감 지연을 늘리지 않는다.
                    if isinstance(event, dict) and event.get("type") in _CHECKPOINT_EVENTS:
                        checkpoint_save(req.session_id, orch)
            except Exception as e:
                # SSE 본문은 엔드포인트 리턴 후 생성돼 FastAPI 자동 트랜잭션이 못 잡는 경우가 있어
                # 여기서 명시적으로 Sentry 에 캡처(서버 이슈/레이트리밋 분류)하고, 프론트엔도
                # 친절한 에러를 흘려 스트림을 깔끔히 닫는다. (GeneratorExit=클라 끊김은 Exception 아님 → 통과)
                capture_chat_exception(
                    e, session_id=req.session_id, mode=req.mode, coding_type=req.coding_type
                )
                # ★ 실패한 턴을 원장에서 성공 턴과 갈라 낸다. 이 표시가 없으면 실패 턴은
                #   "토큰 0인 턴"으로 남아 재사용으로 싸게 끝난 턴과 구분이 안 된다 —
                #   실패율도 못 구하고 절감 분석까지 오염된다.
                _turn_stat["status"] = "error"
                _turn_stat["error_code"] = ErrorCode.INTERNAL.value
                record_ops_event("error", code=_turn_stat["error_code"], user_id=user_id,
                                 session_id=req.session_id, detail=str(e)[:200])
                yield _sse_chunk(error_event(ErrorCode.INTERNAL))
                yield _sse_chunk({"type": "done"})
            finally:
                # 클라이언트 끊김(GeneratorExit)에도 저장·flush·락해제 보장.
                # release 는 acquire 와 다른 스레드(스트림 워커)에서 호출되지만 Lock 이라 OK.
                # ⚠️ 핵심: auto_save(디스크 쓰기)나 flush 가 예외를 던져도 release 는 반드시 실행돼야 한다.
                #   예전엔 세 줄이 그냥 나열돼 있어 auto_save 가 OSError 등을 던지면 release 가 스킵됐고,
                #   그러면 세션 락이 영구(인메모리)/장기(redis TTL 900s) 점유돼 이후 같은 세션의 모든
                #   요청이 session_busy 로 거절됐다(이 이슈의 근본 원인 · S1 회귀). 그래서 각 정리 단계를
                #   개별 가드로 감싸고 release 를 맨 마지막에 무조건 실행한다.
                try:
                    auto_save(req.session_id, orch)
                except Exception as save_err:
                    # 저장 실패는 관측에 남기되(원인 추적용) 락 해제를 막지 않는다.
                    capture_chat_exception(save_err, session_id=req.session_id, stage="auto_save")

                # #130/#131: 턴 토큰 사용량 회수 — 쿼터 집행(QUOTA_ENABLED) 여부와 무관하게
                # 항상 회수한다. #133: usage_turns(분석 원천) 기록이 집행 킬스위치에 종속되면
                # QUOTA_ENABLED=false(현재 기본값) 배포에서 분석 데이터가 전혀 쌓이지 않아
                # 집행/분석 역할 분리(설계문서 §3.3)가 깨진다.
                turn_usage = None
                try:
                    turn_usage = orch.pop_turn_usage()
                    if QUOTA_ENABLED:
                        subj = quota_subj or quota_subject(user_id, req.session_id, scope=QUOTA_SCOPE)
                        # 사용자 subject 에만 turns=1 을 실어 "하루 N번" 카운트(토큰은 그대로 누적).
                        # IP subject 는 토큰 상한 전용이라 턴을 세지 않는다.
                        quota_store.add(subj, turn_usage + TokenUsage(turns=1))
                        if QUOTA_DAILY_WEIGHTED_TOKENS_PER_IP > 0:
                            ip_subj = _quota_ip_subject(_quota_client_ip(request))
                            if ip_subj:
                                quota_store.add(ip_subj, turn_usage)
                except Exception as quota_err:
                    # 기록 실패도 락 해제를 막지 않는다(fail-open, auto_save 가드와 동일 원칙).
                    capture_chat_exception(quota_err, session_id=req.session_id, stage="quota")

                # #133: 사용량 영속 기록 — rag-search MySQL usage_turns 로 이중쓰기(분석 원천).
                # 앱은 MySQL 직접 접근 금지 제약을 지키며 rag-search /api/usage/add 경유.
                # 실패해도 /chat 을 막지 않는다(fail-open) — 세션 이중쓰기와 동일 원칙.
                # RAG_UPSTREAM 미설정이면 POST 시도 자체를 하지 않는다(온프렘 경량 배포 등).
                if turn_usage is not None and _RAG_UPSTREAM:
                    try:
                        subj = quota_subj or quota_subject(user_id, req.session_id, scope=QUOTA_SCOPE)
                        trace_id = ""
                        try:
                            trace_id = get_client().get_current_trace_id() or ""
                        except Exception:
                            pass  # Langfuse 트레이스 컨텍스트가 finally 시점에 없을 수 있음(엣지 케이스)
                        _usage_writeback_upstream({
                            "ts": _utc_stamp(),
                            "subject": subj,
                            "user_id": user_id or "",
                            "session_id": req.session_id,
                            "mode": req.mode,
                            "coding_type": req.coding_type,
                            # ★ 설정값이 아니라 **이 턴이 실제로 탄 경로**를 남긴다.
                            #   폴백이 걸리면 API 모드여도 그 턴은 구독으로 나가 실청구가
                            #   0 이고, 운영 중 모드를 바꾼 날은 하루에 두 경로가 섞인다.
                            #   날짜 단위 라벨로는 실과금을 못 가른다.
                            "llm_mode": _turn_llm_route(orch),
                            # 재사용 라우팅 결과 — "재사용으로 얼마나 아꼈나"는 티어별
                            # 단가를 대조해야만 나온다.
                            "reuse_tier": getattr(orch, "_reuse_tier", "") or "",
                            "input_tokens": turn_usage.input,
                            "output_tokens": turn_usage.output,
                            "cache_read_tokens": turn_usage.cache_read,
                            "cache_creation_tokens": turn_usage.cache_creation,
                            # 기록 시점 가중치로 계산해 저장 — 이후 QUOTA_WEIGHT_* 가 바뀌어도
                            # 과거 분석값은 불변(엣지 케이스 결정).
                            "weighted_tokens": turn_usage.weighted(
                                QUOTA_WEIGHT_OUTPUT, QUOTA_WEIGHT_CACHE_READ, QUOTA_WEIGHT_CACHE_CREATION),
                            "trace_id": trace_id,

                            # ── 부하 관측 ──
                            # started_at 이 있어야 동시 접속을 **구간 겹침**으로 셀 수 있다.
                            # 별도 샘플링이 아니라 전수 계산이라 피크를 놓치지 않는다.
                            "started_at": _started_at,
                            "duration_ms": round((time.monotonic() - _t_entry) * 1000),
                            "ttft_ms": _turn_stat["ttft_ms"],
                            "status": ("error" if _turn_stat["status"] == "error"
                                       else ("ok" if _turn_stat["done"] else "aborted")),
                            "error_code": _turn_stat["error_code"],
                            "replica": _REPLICA,

                            # ── 질문 유형·결과 ──
                            # intent 는 이미 매 턴 계산되던 값이다(분류기 추가 비용 0).
                            "intent": getattr(orch, "_turn_intent", "") or "",
                            "phase": _turn_phase(orch),
                            "outcome": _turn_outcome(_fp_before, _artifact_fingerprint(orch),
                                                     getattr(orch, "_turn_intent", "") or "",
                                                     req.coding_type),

                            # ── 비용 절감 분석 ──
                            # top1 분포가 있어야 "임계값을 얼마로 내리면 재사용이 몇 % 늘고
                            # 얼마가 절감되는가"를 사후에 계산할 수 있다.
                            **_reuse_metrics(orch),

                            # ── 접속 환경 ──
                            # 기기·브라우저별로 실패나 지연이 갈리는지 보려면 필요하고,
                            # 부수적으로 **사람과 스크립트를 가른다** — 부하 스크립트는
                            # UA 가 curl 이라 실제 학생 수와 섞이지 않는다.
                            "user_agent": (request.headers.get("user-agent") or "")[:255],
                            "client_ip": _quota_client_ip(request)[:45],
                            # 이 턴이 끝난 시점의 컨테이너 메모리 — 1g 상한에
                            # 다가가는지 미리 보려면 턴마다 찍어야 한다.
                            "mem_mb": _mem_mb(),
                        })
                    except Exception as usage_err:
                        capture_chat_exception(usage_err, session_id=req.session_id, stage="usage_persist")

                if _langfuse_enabled:
                    try:
                        get_client().flush()
                    except Exception:
                        pass
                session_locks.release(req.session_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/v3/create/sessions", status_code=201)
async def v3_create_session(req: CreateSessionRequest):
    """Start a guided Create session without instantiating a second generation core."""
    try:
        adapter = CreateOrchestratorAdapter.start(req.coding_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    create_sessions.set(adapter.session_id, adapter)
    return JSONResponse({
        "session_id": adapter.session_id,
        "mode": adapter.mode,
        "coding_type": adapter.coding_type,
        "chat_endpoint": f"/api/v3/create/sessions/{adapter.session_id}/chat",
    }, status_code=201)


@app.post("/api/v3/create/sessions/{session_id}/chat")
async def v3_create_chat(
    session_id: str,
    req: CreateChatRequest,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Translate to and delegate through the existing /chat implementation."""
    adapter = create_sessions.get(session_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail="create session not found")
    legacy_request = ChatRequest(**adapter.legacy_chat_payload(
        req.message, runtime_error=req.runtime_error
    ))
    return await chat(req=legacy_request, request=request, user_id=user_id)


@app.get("/quota")
async def quota_info(session_id: str = Query(""), user_id: str = Depends(get_user_id)):
    """프론트가 남은 일 쿼터를 조회(#131). QUOTA_ENABLED 가 꺼져 있어도 현재 누적을 그대로 보여준다
    (enabled 플래그로 강제 여부만 구분 — 프론트가 킬스위치 켜기 전 미리보기 가능)."""
    subject = quota_subject(user_id, session_id, scope=QUOTA_SCOPE)
    snapshot = quota_store.used(subject)
    used = snapshot.weighted(
        QUOTA_WEIGHT_OUTPUT, QUOTA_WEIGHT_CACHE_READ, QUOTA_WEIGHT_CACHE_CREATION)
    remaining = max(0, QUOTA_DAILY_WEIGHTED_TOKENS - used)
    turns_used = snapshot.turns
    return JSONResponse({
        "ok": True,
        "enabled": QUOTA_ENABLED,
        "scope": QUOTA_SCOPE,
        "limit": QUOTA_DAILY_WEIGHTED_TOKENS,
        "used": used,
        "remaining": remaining,
        # 턴 차원(#turn-limit) — max_turns=0 이면 미적용이라 remaining 은 null.
        "max_turns": QUOTA_DAILY_MAX_TURNS,
        "turns_used": turns_used,
        "turns_remaining": (max(0, QUOTA_DAILY_MAX_TURNS - turns_used)
                            if QUOTA_DAILY_MAX_TURNS > 0 else None),
        "resets_at": quota_store.reset_at().isoformat(),
    })


@app.get("/health")
async def health():
    """경량 라이브니스 — LLM 토큰을 소모하지 않는다(부하 테스트 중 쿼터 보호)."""
    return {
        "status": "ok",
        "mode": "cli" if os.getenv("USE_LOCAL_CLAUDE", "true").strip().lower() in ("true", "1", "yes") else "api",
        "active_sessions": len(sessions),
        # 어느 레플리카가 응답했는지 — 프록시가 실제로 3대에 분산하는지 확인용.
        "replica": _REPLICA,
        # 관측 기록이 새고 있는지. 부하 중 이 숫자가 오르면 리포트가 **과소집계**된다.
        # 조용히 사라지는 것이 부하 분석에서 제일 위험해서 밖으로 뺀다.
        "writeback": {
            "queued": _writeback_q.qsize(),
            "dropped": _writeback_dropped,
            "failed": _writeback_failed,
            "last_error": _writeback_last_error,
            # 스레드가 죽으면 큐에 쌓이기만 하고 아무것도 안 나간다 — 살아 있는지 본다.
            "worker_alive": _writeback_thread.is_alive(),
        },
        # 이 컨테이너의 메모리(MB). compose 의 mem_limit(1g) 대비로 본다.
        "mem_mb": _mem_mb(),
    }


# LLM 핑 결과 캐시(짧은 TTL) — 헬스체크가 구독 쿼터를 갉아먹지 않도록.
_llm_health_cache: TTLCache = TTLCache(ttl_seconds=30.0)


@app.get("/health/llm")
async def health_llm(session_id: str = Query(""), ping: int = Query(0)):
    """세션별 LLM 동작 확인.

    - 기본: 토큰 소모 없이 mode + 세션 생존 여부만 반환.
    - ?ping=1: 실제 LLM 왕복 1회(짧은 프롬프트). TTL 캐시로 빈도 제한.
    """
    info = {
        "mode": "cli" if os.getenv("USE_LOCAL_CLAUDE", "true").strip().lower() in ("true", "1", "yes") else "api",
        "session_id": session_id or None,
        "session_alive": (session_id in sessions) if session_id else None,
        "session_busy": session_locks.is_busy(session_id) if session_id else None,
    }
    if not ping:
        info["ok"] = True
        return info

    cached = _llm_health_cache.get("ping")
    if cached is not None:
        return {**info, **cached, "cached": True}

    from agent.claude_client import create_client
    from agent.llm_config import HAIKU

    started = time.time()
    try:
        client = create_client(os.getenv("ANTHROPIC_API_KEY", ""))
        client.messages.create(
            model=HAIKU, max_tokens=8, system="",
            messages=[{"role": "user", "content": "ping"}],
        )
        result = {"ok": True, "latency_ms": round((time.time() - started) * 1000)}
    except Exception as e:
        result = {"ok": False, "error": str(e)[:300], "latency_ms": round((time.time() - started) * 1000)}

    _llm_health_cache.set("ping", result)
    return {**info, **result, "cached": False}


class StopRequest(BaseModel):
    session_id: str


@app.post("/chat/stop")
async def stop_chat(req: StopRequest):
    orch = sessions.get(req.session_id)
    if orch is None:
        return {"status": "no_session"}
    orch.cancel()
    return {"status": "cancelled"}


@app.get("/session/{session_id}")
async def get_session(session_id: str, user_id: str = Depends(get_user_id)):
    orch = get_orchestrator(session_id, user_id)
    return {
        "phase": orch.get_phase(),
        "diagram": orch.get_diagram(),
    }


@app.post("/session/{session_id}/reset")
async def reset_session(session_id: str):
    orch = sessions.get(session_id)
    if orch is not None:
        orch.reset()
    return {"status": "ok"}


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def _turn_llm_route(orch) -> str:
    """이번 턴이 실제로 탄 LLM 경로. 판정 실패는 빈 문자열(=미상)로 둔다.

    비용 측정의 정확도가 여기 달려 있으므로 추측하지 않는다 — 모르면 모른다고 남기고,
    리포트에서 '미상'으로 따로 보여 준다.
    """
    try:
        from agent.claude_client import call_route
        return call_route(getattr(orch, "client", None))
    except Exception:
        return ""


def _turn_phase(orch) -> str:
    """설계 단계인가 구현 단계인가. 단계에 따라 비용 구조가 완전히 다르다."""
    try:
        ph = orch.state.project.phase
        return (getattr(ph, "value", None) or str(ph)).lower()[:12]
    except Exception:
        return ""


def _reuse_metrics(orch) -> dict:
    """재사용·직접서브 지표 — 비용 절감을 사후에 계산하기 위한 원장 항목.

    reuse_tier 는 "결과"만 알려 준다(direct_serve/near/cold). 절감을 **늘리려면**
    분포가 필요하다: top1 이 0.58 에 몰려 있는데 임계가 0.60 이면 임계를 조금만 내려도
    재사용이 확 는다는 뜻이고, 0.2 에 흩어져 있으면 임계를 내려도 오탐만 는다는 뜻이다.
    그 판단은 top1 을 남겨 둔 날에만 할 수 있다.
    """
    out = {"reuse_top1": 0.0, "direct_served": 0, "docs_restored": 0}
    try:
        flag = getattr(orch, "_reuse_flag", None) or {}
        top1 = flag.get("top1")
        if top1 is not None:
            out["reuse_top1"] = round(float(top1), 4)
    except (TypeError, ValueError):
        pass
    try:
        ds = getattr(orch, "_direct_served", None) or {}
        out["direct_served"] = 1 if ds.get("accept") else 0
        out["docs_restored"] = int(ds.get("docs_restored") or 0)
    except (TypeError, ValueError):
        pass
    return out


def _artifact_fingerprint(orch) -> tuple:
    """산출물의 현재 크기 지문. 턴 전후를 비교해 **이 턴이 뭘 만들었는지** 가른다.

    현재 상태만 보면 안 된다 — 한 번 코드가 생기면 그 뒤의 잡담 턴까지 전부 "코드 턴"
    으로 집계돼 유형별 단가가 무너진다. 그래서 전/후 델타로 판정한다.
    """
    try:
        st = orch.state
        return (len(getattr(st, "generated_code_map", {}) or {}),
                len(getattr(st, "blockly_xml", "") or ""),
                len(getattr(st, "design_doc", "") or ""))
    except Exception:
        return (0, 0, 0)


def _turn_outcome(before: tuple, after: tuple, intent: str, coding_type: str) -> str:
    """이 턴의 결과물 유형: blockly | code | doc | chat | none.

    같은 "질문"이어도 코드가 나온 턴과 대화만 한 턴은 비용이 10배 넘게 갈린다.
    coding_type(react=소프트웨어 / blockly=하드웨어)과 교차하면 유형별 단가가 나온다.
    """
    d_code, d_block, d_doc = (a - b for a, b in zip(after, before))
    if d_block > 0:
        return "blockly"
    if d_code > 0:
        return "blockly" if coding_type == "blockly" else "code"
    if d_doc > 0:
        return "doc"
    if intent in ("question", "chat"):
        return "chat"
    return "none"


SAVE_DIR = "projects"


# 프로젝트를 유저(uuid)별 하위 폴더로 정리한다: projects/<user_id>/<session_id>.json.
# 폴더 자체가 소유 경계 — 목록·복원·삭제가 전부 자기 폴더 안에서만 일어나므로, 다른 uuid의
# 파일을 건드릴 수 없다(파일별 user_id 비교 불필요). 식별자가 없으면 _anon 폴더로 모은다.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_id(s: str) -> str:
    """경로 컴포넌트로 안전하게 — uuid/세션id는 [A-Za-z0-9_-]만 허용(../ 등 조작 차단)."""
    return _SAFE_ID_RE.sub("", s or "")


def _user_dir(user_id: str) -> str:
    return os.path.join(SAVE_DIR, _safe_id(user_id) or "_anon")


def _session_path(user_id: str, session_id: str) -> str:
    return os.path.join(_user_dir(user_id), f"{_safe_id(session_id)}.json")


def _build_save_data(session_id: str, orch: StreamOrchestrator) -> dict:
    state = orch.state
    doc = state.project.design_doc
    task_plan = state.project.task_plan
    return {
        "session_id": session_id,
        "user_id": getattr(orch, "_user_id", "") or "",
        "title": state.title or None,
        "description": state.description or None,
        "phase": state.project.phase.value,  # canonical(design/implement/verify)로 저장 — 표시용 한글 라벨은 get_phase()로 분리
        "diagram": orch.get_diagram(),
        "generated_code": dict(state.generated_code_map),
        "coding_type": state.coding_type or None,
        "blockly_xml": state.blockly_xml or None,
        "blockly_flowchart": state.blockly_flowchart or None,
        "blockly_code_langs": state.blockly_code_langs or None,
        "modi_modules": state.modi_modules or None,
        "files": state.get_files_summary(),
        "design_doc": doc.model_dump() if (doc.features or doc.pages) else None,
        "task_plan": {
            "tasks": [t.model_dump() for t in task_plan.tasks],
            "progress": task_plan.progress_summary(),
        } if task_plan.tasks else None,
        "learning_notes": state.learning_notes,
        "code_annotations": state.code_annotations,
        "app_type": state.app_type,
        "conversation": state.get_text_history(),
        "messages": _serialize_messages(state._messages),
    }


def _serialize_messages(messages: list) -> list:
    """채팅 메시지를 JSON 직렬화 가능한 형태로 변환.

    tool_use 포함 중간 턴은 건너뛰고, 유저에게 보여진 최종 응답만 저장.
    agent_steps 정보가 있으면 함께 저장.

    단, tool_use로만 끝난 턴(quick 모드 등 최종 텍스트 응답이 없는 경우)에 태깅된
    agent_steps는 그냥 버리면 에이전트 로그가 사라지므로, 보류했다가 다음 텍스트
    응답에 붙이거나(없으면) 빈 AI 항목으로 보존한다.
    """
    from agent.context import _extract_text

    result = []
    pending_steps = None  # 텍스트 없는 turn에서 떠밀린 agent_steps

    def flush_pending():
        nonlocal pending_steps
        if pending_steps is not None:
            result.append({"role": "ai", "content": "", "agent_steps": pending_steps})
            pending_steps = None

    for msg in messages:
        # 오케스트레이터 내부 메시지(재시도 nudge, 무효 처리된 실패 응답)는 LLM 컨텍스트용일 뿐
        # 유저에게 보여준 적 없는 대화 — 저장하면 복원 채팅에 유저가 안 친 말이 나타난다.
        if msg.get("_internal"):
            continue
        if msg["role"] == "user":
            if isinstance(msg["content"], str):
                flush_pending()  # 턴 경계 — 미부착 스텝을 빈 응답으로 보존
                result.append({"role": "user", "content": msg["content"]})
        elif msg["role"] == "assistant":
            steps = msg.get("_agent_steps")
            # 에이전트가 '한 말'(프로즈)은 도구를 같이 호출했든 아니든 모두 'ai' 메시지로 저장한다
            # (스트리밍에서 채팅 버블로 보여준 것과 일치). '한 일'(도구 진행)은 agent_steps에 따로 있음.
            texts = [t for b in msg["content"] if (t := _extract_text(b))]
            if texts:
                entry = {"role": "ai", "content": "\n".join(texts)}
                # 이 메시지의 스텝 우선, 없으면 앞서 보류된 스텝을 붙임
                entry_steps = steps if steps is not None else pending_steps
                if entry_steps is not None:
                    entry["agent_steps"] = entry_steps
                    pending_steps = None
                result.append(entry)
            elif steps is not None:
                # 텍스트 없이 도구만 호출한 턴: 스텝만 보류해 다음 메시지에 붙임
                pending_steps = steps

    flush_pending()  # 마지막 턴 잔여 스텝 보존
    return result


# 깨진 유니코드(lone surrogate) 매칭용. 스트리밍 중 이모지/멀티바이트 문자가
# 토큰 경계에서 잘리면 짝 없는 surrogate가 생겨 utf-8 인코딩이 실패한다.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _write_session_json(data: dict, filepath: str):
    # json.dumps는 lone surrogate를 통과시키지만 utf-8 파일로 쓸 때 UnicodeEncodeError가
    # 나므로, 직렬화 후 깨진 surrogate를 제거해 항상 유효한 UTF-8로 저장한다.
    text = _SURROGATE_RE.sub("", json.dumps(data, ensure_ascii=False, indent=2))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    # 원자적 저장: temp에 다 쓴 뒤 교체. 쓰다 중단/실패해도 기존 파일이 잘리지 않는다
    # (예전엔 open(...,"w")가 먼저 비운 뒤 쓰다 크래시→파일이 손상됨).
    tmp = f"{filepath}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, filepath)


def _rag_writeback_upstream(bundle: dict) -> None:
    """프록시 모드: 세션 결과 묶음을 rag-search(/api/writeback)로 보내 되먹임 등록.

    메인앱(:18080)은 torch 가 없어 인프로세스 임베딩이 0벡터가 되고 검색 백엔드에도
    안 닿는다(#58). 그래서 등록을 torch·백엔드를 가진 rag-search 에 위임한다.
    """
    import httpx
    with httpx.Client(timeout=15.0) as c:
        c.post(f"{_RAG_UPSTREAM}/api/writeback", json=bundle)


def _score_register_result(ok: bool, reason: str | None = None) -> None:
    """등록 되먹임 성공/실패를 Langfuse 현재 트레이스 스코어로 가시화(#104).

    성공: register_ok=1(BOOLEAN). 실패: register_ok=0 + register_skip_reason(CATEGORICAL,
    예외 타입명). "저장이 안 되고 있다"를 코호트로 즉시 발견하려는 목적 — print 무음 실패 보완.
    Langfuse 미설정/트레이스 부재/호출 실패는 조용히 무시한다(관측이 저장 경로를 깨면 안 됨 —
    orchestrator_stream._emit_turn_scores 의 try/except pass 패턴과 동형).
    """
    try:
        lf = get_client()
        lf.score_current_trace(name="등록 성공 (register_ok)",
                               value=1 if ok else 0, data_type="BOOLEAN")
        if not ok and reason:
            lf.score_current_trace(name="등록 스킵사유 (register_skip_reason)",
                                   value=reason, data_type="CATEGORICAL")
    except Exception:
        pass


def _rag_feedback(session_id: str, user_id: str | None, state) -> None:
    """빌드 결과(learning_notes·design_doc·code)를 검색 인덱스에 되먹임 등록.

    - 프록시 모드(RAG_UPSTREAM): 묶음을 rag-search /api/writeback 으로 POST(#58).
    - 로컬 모드: registry_lib 를 인프로세스로 호출(기존 동작, torch 있는 배포/개발).
    #44: 세션 MODI 모듈 key 를 함께 전달 → 등록물에 하드웨어 연계 채움(base 청크와 동형).
    실패해도 상위 저장 경로는 깨지지 않게 예외를 삼킨다.
    #104: 성공/실패를 Langfuse score(register_ok)로 남겨 무음 실패를 코호트로 가시화한다.
    """
    try:
        import chunk_fields
        modi_keys = chunk_fields.modi_module_keys(getattr(state, "modi_modules", None))
        # #44: 학습노트뿐 아니라 설계문서·생성 코드도 되먹임 → 검색 클릭 시 동일 렌더.
        doc = state.project.design_doc
        design_doc = doc.model_dump() if (doc.features or doc.pages) else None
        code_map = dict(state.generated_code_map) if state.generated_code_map else None
        # goal: 재사용 검색이 "무엇을 만들었나"(요청 목표)로 잡히도록.
        goal = ((design_doc or {}).get("description")
                or (design_doc or {}).get("project_name")
                or state.title or "")
        if _RAG_UPSTREAM:
            _rag_writeback_upstream({
                "session_id": session_id, "user_id": user_id,
                "coding_type": state.coding_type,
                "learning_notes": state.learning_notes or [],
                "design_doc": design_doc, "code_map": code_map,
                "modi_keys": modi_keys, "goal": goal,
            })
        else:
            import registry_lib
            registry_lib.register_learning_notes(
                session_id, user_id, state.coding_type, state.learning_notes or [],
                modi_keys=modi_keys,
            )
            if design_doc or code_map:
                registry_lib.register_result(
                    session_id, user_id, state.coding_type,
                    design_doc=design_doc, code_map=code_map, modi_keys=modi_keys, goal=goal,
                    learning_notes=state.learning_notes or [],
                )
        _score_register_result(True)  # 프록시 POST / 로컬 등록 모두 성공 시 동일 경로
    except Exception as e:  # 임베딩/스토어/네트워크 문제로 저장 경로가 깨지면 안 됨
        _score_register_result(False, type(e).__name__)
        print(f"[rag] auto-register 건너뜀: {e}", flush=True)


def _session_writeback_upstream(data: dict, user_id: str | None) -> None:
    """프록시 모드: 세션 전문을 rag-search /api/session/save(MySQL 원천)로 이중쓰기(#27 P3).

    메인앱은 MySQL 접속이 없어 세션 원천 저장을 torch·DB 를 가진 rag-search 에 위임한다.
    파일 저장과 병행(이중쓰기) — 실패해도 파일이 있으니 리스트/복원은 파일 폴백으로 동작.
    """
    import httpx
    body = {
        "session_id": data.get("session_id"), "user_id": user_id or "",
        "title": data.get("title"), "description": data.get("description"),
        "coding_type": data.get("coding_type"), "app_type": data.get("app_type"),
        "phase": data.get("phase"), "raw": data,
    }
    with httpx.Client(timeout=15.0) as c:
        c.post(f"{_RAG_UPSTREAM}/api/session/save", json=body)


# ─────────────────────────────────────────────────────────────────────────────
# 관측 기록 전송기 — 요청 스레드를 막지 않는 단일 백그라운드 워커
#
# 왜 비동기인가(2026-08-21 부하 대비 결정):
#   기존 구조는 /chat 의 finally 에서 **동기 HTTP(타임아웃 2s)** 로 보냈다. 평상시엔
#   문제가 없지만 부하가 걸리면 정확히 반대로 작동한다 — MySQL 이 느려질수록 finally 가
#   최대 2초 더 걸리고, 그동안 **세션 락이 계속 잡혀 있어** 다음 요청이 session_busy 로
#   튕긴다. 관측을 위한 코드가 장애를 키우는 구조였다.
#
#   게다가 rag-search 쪽 /api/usage/add 는 호출마다 MySQL 연결을 새로 연다. 40명이
#   동시에 몰리면 연결 경합이 겹친다. 워커를 하나로 모으면 연결도 하나로 모인다.
#
# 유실을 숨기지 않는다:
#   큐가 가득 차면 버리되 **버린 개수를 센다.** "조용히 사라져서 리포트가 과소집계되는"
#   것이 부하 분석에서 제일 위험하다. /health 로 노출해 수업 중에도 확인할 수 있게 한다.
_WRITEBACK_QUEUE_MAX = int(os.getenv("WRITEBACK_QUEUE_MAX", "5000"))
_writeback_q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=_WRITEBACK_QUEUE_MAX)
_writeback_dropped = 0          # 큐 포화로 버린 건수(누적)
_writeback_failed = 0           # 전송 실패 건수(누적)
# 마지막 실패 사유. 건수만 노출하면 "몇 건 실패"까지는 알아도 **왜**를 몰라 고칠 수가
# 없다(2026-08-21: 운영 사건이 안 쌓이는데 원인을 볼 방법이 없었다).
_writeback_last_error = ""


def _writeback_worker() -> None:
    """큐를 비우는 단일 데몬 스레드. httpx.Client 를 재사용해 연결을 아낀다."""
    global _writeback_failed, _writeback_last_error
    import httpx

    # 타임아웃을 넉넉히 잡는 이유(2026-08-21 동시 15건 실측):
    #   5초로 뒀더니 15건 중 12건이 "timed out" 으로 기록됐는데, **DB 에는 15건이
    #   전부 들어가 있었다.** rag-search 가 같은 순간 임베딩 작업을 처리하느라 응답이
    #   늦었을 뿐이고 삽입 자체는 끝났던 것이다.
    #
    #   두 가지가 나빴다: ① 멀쩡한데 실패로 세어 /health 가 "기록이 새고 있다"고
    #   잘못 알린다 ② 여기서 재시도를 넣었다면 **중복 행**이 쌓였을 것이다.
    #
    #   이 워커는 요청 스레드 밖에서 돌므로 오래 기다려도 /chat 에 영향이 없다.
    #   짧은 타임아웃으로 얻을 게 없다.
    client = httpx.Client(timeout=float(os.getenv("WRITEBACK_TIMEOUT_SECONDS", "30")))
    while True:
        try:
            path, body = _writeback_q.get()
            try:
                r = client.post(f"{_RAG_UPSTREAM}{path}", json=body)
                r.raise_for_status()
            except Exception as e:
                _writeback_failed += 1
                _writeback_last_error = f"{path}: {str(e)[:160]}"
                # 표준출력에도 남긴다 — /health 는 마지막 하나만 보여 주는데,
                # 부하 중에는 어떤 종류가 계속 실패하는지 흐름으로 봐야 한다.
                print(f"[writeback] 실패 {path}: {str(e)[:200]}", flush=True)
            finally:
                _writeback_q.task_done()
        except Exception:
            # 워커가 죽으면 이후 기록이 전부 사라진다 — 무슨 일이 있어도 루프를 유지한다.
            continue


_writeback_thread = threading.Thread(target=_writeback_worker, daemon=True,
                                     name="writeback")
_writeback_thread.start()


def _ensure_writeback_worker() -> None:
    """워커가 죽어 있으면 되살린다.

    스레드를 임포트 시점에 한 번만 띄우면 두 경우에 조용히 사라진다:
      ① uvicorn 이 워커 프로세스를 **fork** 로 띄우면 자식에는 스레드가 안 따라간다
      ② 예기치 못한 이유로 스레드가 종료된 경우
    둘 다 큐에는 계속 쌓이는데 아무것도 안 나가는 상태가 되고, 그 사이 기록이 전부
    유실된다. 큐잉할 때마다 싸게 확인한다(is_alive 는 논블로킹).
    """
    global _writeback_thread
    if _writeback_thread.is_alive():
        return
    _writeback_thread = threading.Thread(target=_writeback_worker, daemon=True,
                                         name="writeback")
    _writeback_thread.start()


def _enqueue_writeback(path: str, body: dict) -> None:
    """요청 스레드를 절대 막지 않는다 — 큐가 차 있으면 세지고 버린다."""
    global _writeback_dropped
    if not _RAG_UPSTREAM:
        return
    try:
        _ensure_writeback_worker()
        _writeback_q.put_nowait((path, body))
    except queue.Full:
        _writeback_dropped += 1
    except Exception:
        _writeback_dropped += 1


def _usage_writeback_upstream(body: dict) -> None:
    """#133: 턴 사용량 1건을 rag-search `/api/usage/add`(MySQL usage_turns)로 큐잉."""
    _enqueue_writeback("/api/usage/add", body)


def record_ops_event(kind: str, *, code: str = "", user_id: str = "",
                     session_id: str = "", detail: str = "") -> None:
    """턴이 만들어지지 않는 사건을 원장에 남긴다(session_busy·쿼터·차단·에러 등).

    ★ 이게 없으면 40명 동시 수업에서 가장 중요한 숫자를 못 얻는다. session_busy 와
      쿼터 거절은 /chat 이 세션 락을 잡기 **전에** return 하므로 usage_turns 의
      finally 에 도달하지 못한다 — 즉 "몇 명이 튕겼나"가 원장에 아예 안 남는다.
      지금까지는 Sentry 에만 있었는데, Sentry 는 보존기간이 있고 리포트와 같은
      시간축에 겹쳐 볼 수가 없다.

    거절 경로는 부하가 가장 심할 때 도는 코드라 **절대 블로킹하지 않는다**(큐잉만).
    """
    _enqueue_writeback("/api/ops/event", {
        "ts": _utc_stamp(),
        "kind": kind, "code": code,
        "user_id": user_id or "", "session_id": session_id or "",
        "replica": _REPLICA, "detail": (detail or "")[:255],
    })


# 턴 '중간'에 산출물을 디스크에 확정하는 체크포인트 대상 이벤트.
# 이 신호가 나온 시점엔 이미 비싼 생성(LLM 수 회 · 수십 초)이 끝났고, 뒤이어 학습노트·
# 흐름도 같은 후처리가 더 붙는다. 후처리 도중 프로세스가 죽으면(OOM/SIGKILL) finally 가
# 안 돌아 **그 턴 산출물이 통째로 유실**된다(실측: 복원 시 코드 0개).
# 실제 생성 턴이 94~160초인데 그 대부분이 산출물 확정 이후 구간이라 노출 창이 크다.
_CHECKPOINT_EVENTS = frozenset({"blockly_ready", "code_validated"})


def checkpoint_save(session_id: str, orch: StreamOrchestrator) -> None:
    """턴 중간 체크포인트 — 실패해도 스트림을 절대 막지 않는다.

    auto_save 와 달리 RAG 되먹임·MySQL 이중쓰기는 하지 않는다(턴이 아직 안 끝났고,
    핫 경로에 네트워크 왕복을 넣으면 스트림이 느려진다). 파일만 확정해 두고, 턴이
    정상 종료하면 finally 의 auto_save 가 되먹임까지 마무리한다.
    """
    try:
        state = orch.state
        if not state.generated_code_map and not state.blockly_xml:
            return
        filepath = _session_path(getattr(orch, "_user_id", "") or "", session_id)
        _write_session_json(_build_save_data(session_id, orch), filepath)
        orch._loaded_mtime = _disk_mtime(filepath)
    except Exception as e:
        # 체크포인트 실패가 사용자 응답을 끊으면 본말전도다. 관측만 남긴다.
        print(f"[checkpoint] 세션 {session_id} 중간 저장 실패(무시): {e}", flush=True)


def auto_save(session_id: str, orch: StreamOrchestrator):
    state = orch.state
    if not state.generated_code_map and not state.get_text_history():
        return
    data = _build_save_data(session_id, orch)
    # 소유자 폴더(projects/<uid>/)에 저장. orch._user_id는 /chat에서 주입됨.
    filepath = _session_path(orch._user_id, session_id)
    _write_session_json(data, filepath)
    # 방금 내가 저장했으니 적재 시점을 최신으로 — 다음 get_orchestrator 가 불필요 재로딩 안 하도록.
    orch._loaded_mtime = _disk_mtime(filepath)
    # RAG 되먹임: 빌드 결과(노트/설계/코드)를 검색 인덱스에 자동 등록. 실패해도 저장은 성공.
    _rag_feedback(session_id, orch._user_id, state)
    # #27 P3: 세션 전문을 MySQL 원천에도 이중쓰기(프록시 모드). 실패해도 파일 저장은 유효.
    if _RAG_UPSTREAM:
        try:
            _session_writeback_upstream(data, orch._user_id)
        except Exception as e:
            print(f"[session] MySQL 이중쓰기 건너뜀: {e}", flush=True)


@app.post("/session/{session_id}/save")
async def save_session(session_id: str, user_id: str = Depends(get_user_id)):
    orch = get_orchestrator(session_id, user_id)
    if user_id:
        orch._user_id = user_id
    data = _build_save_data(session_id, orch)

    filename = f"{session_id}.json"
    filepath = _session_path(user_id, session_id)
    _write_session_json(data, filepath)

    return {"status": "ok", "file": filename}


@app.get("/projects")
async def list_projects(user_id: str = Depends(get_user_id)):
    # 파일 목록(항상 베이스라인) — 폴더가 곧 소유 경계라 별도 필터가 필요 없다.
    # #27 P3: 프록시 모드면 MySQL 원천 목록을 session_id 기준으로 union(MySQL 우선)한다.
    #   union 이므로 세션 백필 전이라도 과거 파일 대화가 목록에서 사라지지 않는다(무손실 전환).
    by_id: dict[str, dict] = {}
    user_dir = _user_dir(user_id)
    os.makedirs(user_dir, exist_ok=True)
    for filename in os.listdir(user_dir):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(user_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        sid = data.get("session_id", filename.replace(".json", ""))
        by_id[sid] = {
            "filename": filename,
            "session_id": sid,
            "title": data.get("title") or "제목 없음",
            "description": data.get("description") or "",
            "phase": data.get("phase", ""),
            "app_type": data.get("app_type", ""),
            "coding_type": data.get("coding_type", "react"),
            "has_code": bool(data.get("generated_code")),
            "updated_at": os.path.getmtime(filepath),
        }
    if _RAG_UPSTREAM:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{_RAG_UPSTREAM}/api/session/list",
                                params={"user_id": user_id or ""})
            if r.status_code == 200:
                for p in (r.json().get("projects") or []):
                    sid = p.get("session_id")
                    if sid:
                        by_id[sid] = p  # MySQL 원천 우선(최신)
        except Exception as e:
            print(f"[session] list 프록시 실패, 파일만 반환: {e}", flush=True)
    result = sorted(by_id.values(), key=lambda x: x.get("updated_at") or 0, reverse=True)
    return {"projects": result}


@app.get("/projects/{filename}")
async def load_project(filename: str, user_id: str = Depends(get_user_id)):
    # #27 P3: 프록시 모드면 세션 전문을 MySQL 원천에서 로드(없거나 실패 시 파일 폴백).
    if _RAG_UPSTREAM:
        session_id = os.path.basename(filename).replace(".json", "")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{_RAG_UPSTREAM}/api/session/get",
                                params={"session_id": session_id, "user_id": user_id or ""})
            if r.status_code == 200:
                return JSONResponse(r.json())
        except Exception as e:
            print(f"[session] get 프록시 실패, 파일 폴백: {e}", flush=True)
    filepath = os.path.join(_user_dir(user_id), os.path.basename(filename))
    if not os.path.exists(filepath):
        return error_response(ErrorCode.NOT_FOUND, message="파일을 찾을 수 없습니다")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[project] 세션 파일 손상: {filepath}: {e}", flush=True)
        return error_response(ErrorCode.INTERNAL, message="세션 파일이 손상되었습니다")


@app.delete("/projects/{filename}")
async def delete_project(filename: str, user_id: str = Depends(get_user_id)):
    # 자기 유저 폴더 안에서만 삭제 — 폴더 격리라 남의 파일엔 애초에 닿지 않는다.
    safe_name = os.path.basename(filename)
    filepath = os.path.join(_user_dir(user_id), safe_name)
    session_id = safe_name.replace(".json", "")
    if os.path.exists(filepath):
        os.remove(filepath)
        # 메모리에서도 세션 제거
        sessions.pop(session_id)
        session_locks.discard(session_id)  # 락 dict 무한 증가 방지
    # #27 P3: 프록시 모드면 MySQL 원천에서도 삭제(파일만 지우면 리스트에 다시 뜸).
    if _RAG_UPSTREAM:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as c:
                await c.request("DELETE", f"{_RAG_UPSTREAM}/api/session/delete",
                                params={"session_id": session_id, "user_id": user_id or ""})
        except Exception as e:
            print(f"[session] delete 프록시 건너뜀: {e}", flush=True)
    return {"status": "ok"}


# ── reference(추천 템플릿) ──
REFERENCE_DIR = "reference"

# 추천 템플릿 목록은 자주 안 바뀌므로 짧은 TTL 로 캐싱(디스크 I/O 절감, 모든 유저 공통).
_reference_cache: TTLCache = TTLCache(ttl_seconds=float(os.getenv("REFERENCE_CACHE_TTL", "60")))


def _new_session_id() -> str:
    """프론트(NaturalLanguageCodingManager)와 같은 형식의 새 세션 id 생성."""
    import time
    import random
    import string

    ms = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"session_{ms}_{suffix}"


def _summarize_project(filename: str, data: dict, mtime: float) -> dict:
    """프론트 ProjectSummary 형태로 요약."""
    return {
        "filename": filename,
        "session_id": data.get("session_id", filename.replace(".json", "")),
        "title": data.get("title") or "제목 없음",
        "description": data.get("description") or "",
        "phase": data.get("phase", ""),
        "app_type": data.get("app_type", ""),
        "coding_type": data.get("coding_type", "react"),
        "has_code": bool(data.get("generated_code")),
        "updated_at": mtime,
    }


def _read_reference_list() -> list:
    os.makedirs(REFERENCE_DIR, exist_ok=True)
    result = []
    for filename in sorted(os.listdir(REFERENCE_DIR)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(REFERENCE_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        item = _summarize_project(filename, data, os.path.getmtime(filepath))
        item["name"] = filename[:-5]  # 확장자 뺀 reference 이름
        result.append(item)
    return result


@app.get("/reference")
async def list_reference():
    """추천 템플릿 목록. 짧은 TTL 캐시(디스크 I/O 절감)."""
    return {"reference": _reference_cache.get_or_compute("list", _read_reference_list)}


@app.post("/reference/{name}/instantiate")
async def instantiate_reference(name: str, user_id: str = Depends(get_user_id)):
    """추천 템플릿을 새 세션으로 복제해 projects/에 저장하고 요약을 돌려준다.

    클릭할 때마다 새 session_id로 복사되므로, 학습자는 원본을 건드리지 않고
    자신만의 편집 가능한 사본을 받는다.
    """
    safe = os.path.basename(name)  # 경로 조작 방지
    ref_path = os.path.join(REFERENCE_DIR, f"{safe}.json")
    if not os.path.exists(ref_path):
        return error_response(ErrorCode.NOT_FOUND, message="추천 템플릿을 찾을 수 없습니다")
    try:
        with open(ref_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[reference] 템플릿 손상: {ref_path}: {e}", flush=True)
        return error_response(ErrorCode.INTERNAL, message="추천 템플릿이 손상되었습니다")

    new_id = _new_session_id()
    data["session_id"] = new_id
    # 복제본의 소유자 = 클릭한 디바이스(uuid). projects/<uid>/ 에 저장돼 내 히스토리에 뜬다.
    data["user_id"] = user_id
    filename = f"{new_id}.json"
    filepath = _session_path(user_id, new_id)
    _write_session_json(data, filepath)

    return _summarize_project(filename, data, os.path.getmtime(filepath))


def _restore_state_from_file(orch: StreamOrchestrator, filepath: str):
    """디스크의 JSON 파일에서 orchestrator 상태를 복원"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    state = orch.state
    from agent.models import Phase, Feature, Page, DataModel, Task

    # 소유자(uuid) 복원 — 이후 auto_save가 같은 소유자를 유지하도록.
    orch._user_id = data.get("user_id") or ""

    # phase — 저장은 canonical enum 값(design/implement/verify). 단 예전 파일은 get_phase()의
    # 한글 라벨("설계"/"구현"/"검증")로 저장돼 있어 둘 다 받는다(레거시 호환).
    # (한글을 못 받으면 "구현"이 DESIGN으로 폴백 → 복원 세션이 코드 수정 대신 설계 대화만 하던 버그)
    phase_str = data.get("phase", "design")
    phase_map = {
        "design": Phase.DESIGN, "implement": Phase.IMPLEMENT, "verify": Phase.VERIFY,
        "설계": Phase.DESIGN, "구현": Phase.IMPLEMENT, "검증": Phase.VERIFY,
    }
    state.project.phase = phase_map.get(phase_str, Phase.DESIGN)

    # 코드
    state.generated_code_map = data.get("generated_code") or {}

    # 설계 문서
    doc_data = data.get("design_doc")
    if doc_data:
        state.project.design_doc.project_name = doc_data.get("project_name", "")
        state.project.design_doc.description = doc_data.get("description", "")
        state.project.design_doc.users = doc_data.get("users", [])
        state.project.design_doc.features = [Feature(**f) for f in doc_data.get("features", [])]
        state.project.design_doc.pages = [Page(**p) for p in doc_data.get("pages", [])]
        state.project.design_doc.data_models = [DataModel(**d) for d in doc_data.get("data_models", [])]
        state.project.design_doc.user_flows = doc_data.get("user_flows", [])
        state.project.design_doc.strengths = doc_data.get("strengths", [])
        state.project.design_doc.weaknesses = doc_data.get("weaknesses", [])

    # 태스크 플랜 — 정식 포맷은 {"tasks": [...]} dict 지만, 백필/업스트림 유래 파일엔
    # tasks 리스트가 바로 담긴 레거시 형태가 있다(EDU-AGENT-B: list.get AttributeError).
    task_data = data.get("task_plan")
    if isinstance(task_data, list):
        task_data = {"tasks": task_data}
    if isinstance(task_data, dict) and task_data.get("tasks"):
        state.project.task_plan.tasks = [
            Task(**t) for t in task_data["tasks"] if isinstance(t, dict)
        ]

    # 다이어그램
    diagram_str = data.get("diagram", "")
    if diagram_str:
        state.diagram_manager._mermaid = diagram_str

    # 기본 정보
    state.coding_type = data.get("coding_type") or ""
    state.title = data.get("title") or ""
    state.description = data.get("description") or ""

    # blockly
    state.blockly_xml = data.get("blockly_xml") or ""
    state.blockly_flowchart = data.get("blockly_flowchart") or []
    state.blockly_code_langs = data.get("blockly_code_langs") or {}
    state.modi_modules = data.get("modi_modules") or {}

    # 학습 노트 / 코드 주석 / 앱 타입
    state.learning_notes = data.get("learning_notes") or []
    state.code_annotations = data.get("code_annotations") or []
    state.app_type = data.get("app_type", "")

    # 대화 내역 복원 — 원본 메시지가 있으면 사용, 없으면 요약으로
    saved_messages = data.get("messages", [])
    conversation = data.get("conversation", "")
    if saved_messages:
        for msg in saved_messages:
            if msg["role"] == "user":
                state._messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "ai":
                restored = {"role": "assistant", "content": [
                    {"type": "text", "text": msg["content"]}
                ]}
                # 에이전트 스텝 정보 보존
                if "agent_steps" in msg:
                    restored["_agent_steps"] = msg["agent_steps"]
                state._messages.append(restored)
    elif conversation:
        state._messages = [
            {"role": "user", "content": f"[이전 대화 복원]\n{conversation}"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "네, 이전 대화 내용을 확인했습니다. 이어서 진행할게요."}
            ]}
        ]


@app.post("/session/{session_id}/restore")
async def restore_session(session_id: str, user_id: str = Depends(get_user_id)):
    """저장된 프로젝트를 불러와서 세션에 복원 (자기 유저 폴더 안에서만 탐색)"""
    filepath = _session_path(user_id, session_id)
    if not os.path.exists(filepath):
        return error_response(ErrorCode.NOT_FOUND, message="저장된 세션을 찾을 수 없습니다")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    orch = StreamOrchestrator(api_key=api_key, session_id=session_id)
    try:
        _restore_state_from_file(orch, filepath)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # 파일이 깨졌으면 격리하고 명확한 에러 반환
        _quarantine_corrupt_file(filepath)
        print(f"[session] 복원 실패, 격리됨: {filepath}: {e}", flush=True)
        return error_response(ErrorCode.INTERNAL, message="세션 파일이 손상되어 복원할 수 없습니다")
    # 소유자 없는 레거시 파일이면 복원한 디바이스를 소유자로 보정(이후 저장에 반영).
    if not orch._user_id and user_id:
        orch._user_id = user_id
    orch._loaded_mtime = _disk_mtime(filepath)
    sessions[session_id] = orch

    state = orch.state
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        "status": "ok",
        "phase": data.get("phase", "design"),
        "generated_code": state.generated_code_map,
        "coding_type": state.coding_type or None,
        "blockly_xml": state.blockly_xml or None,
        "blockly_flowchart": state.blockly_flowchart or None,
        "blockly_code_langs": state.blockly_code_langs or None,
        "modi_modules": state.modi_modules or None,
        "design_doc": data.get("design_doc"),
        "task_plan": data.get("task_plan"),
        "diagram": data.get("diagram", ""),
        "learning_notes": state.learning_notes,
        "code_annotations": state.code_annotations,
        "app_type": state.app_type,
        "messages": data.get("messages", []),
    }


# ─────────────────────────────────────────────────────────────
# RAG 하이브리드 검색/등록 (통합) — user_id(uuid) 인증 공유.
# 검색: base(전역 재사용) + 등록 스토어 통합, 개념 centroid blend로 매칭 품질↑.
# 등록: 결과물 → 즉시 임베딩 → 다음 검색 히트(콘텐츠 팩토리 되먹임).
# ─────────────────────────────────────────────────────────────
# RAG 연동 모드:
#   - RAG_UPSTREAM 설정 시: rag-search(:8100, 벡터 MySQL+Redis)로 프록시 → 메인 이미지 경량 유지.
#   - 미설정 시: 로컬 search_lib(있으면) 사용. 둘 다 없으면 RAG 라우트 미등록(코어는 정상 부팅).
_RAG_UPSTREAM = os.getenv("RAG_UPSTREAM", "").rstrip("/")
try:
    import registry_lib  # noqa: E402
    from search_lib import search as _rag_search  # noqa: E402
    from search_lib import coverage as _rag_coverage  # noqa: E402
    from search_lib import vector_enabled as _rag_vector_enabled  # noqa: E402

    _RAG_LOCAL = True
except Exception as _rag_exc:  # scripts/ 미복사, numpy 미설치 등
    print(f"[rag] 로컬 모듈 비활성(모듈/의존 없음): {_rag_exc}", flush=True)
    _RAG_LOCAL = False

_RAG_ENABLED = _RAG_LOCAL or bool(_RAG_UPSTREAM)
if _RAG_UPSTREAM:
    print(f"[rag] 프록시 모드 → {_RAG_UPSTREAM}", flush=True)


if _RAG_ENABLED:
    import httpx  # anthropic 의존으로 이미 설치됨

    _RAG_DEFAULT_QUERIES = [
        "자동차가 벽을 보고 스스로 멈추게 하고 싶어요",
        "다이얼을 돌려서 색을 고르고 싶어요",
        "좋아요 버튼을 누르면 하트 색이 바뀌게",
        "느린 폰에서도 게임 속도가 똑같게 하려면",
        "카드 100개를 똑같이 만들고 싶어요",
        "스크롤 내려도 위 메뉴는 그대로 붙어있게",
        "깜깜해지면 불이 켜지는 장치",
        "숫자를 천 단위 콤마로 예쁘게",
    ]

    def _rag_up_reply(r):
        """업스트림 응답을 JSON 으로 되돌린다. 비-JSON(500 텍스트 등)이면 502 로 감싼다.

        #132: 상태코드 결정 로직은 그대로 두고(Non-goal), 에러 본문 스키마만
        error_response() 와 동일한 {"ok","error":{"code","message","detail"}} 로 통일.
        업스트림이 이미 JSON 을 준 경우(성공/에러 불문)는 그대로 통과 — 이중 래핑 금지.
        """
        try:
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception:
            status = 502 if r.status_code < 400 else r.status_code
            spec = CATALOG[ErrorCode.UPSTREAM_ERROR]
            return JSONResponse(
                {"ok": False, "error": {"code": ErrorCode.UPSTREAM_ERROR.value,
                                        "message": spec.user_message,
                                        "detail": (r.text or "")[:300]}},
                status_code=status)

    async def _rag_up_get(path: str, params: dict):
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                return _rag_up_reply(await c.get(f"{_RAG_UPSTREAM}{path}", params=params))
        except httpx.HTTPError as e:
            return error_response(ErrorCode.UPSTREAM_ERROR, detail=str(e)[:200])

    async def _rag_up_post(path: str, json_body: dict):
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                return _rag_up_reply(await c.post(f"{_RAG_UPSTREAM}{path}", json=json_body))
        except httpx.HTTPError as e:
            return error_response(ErrorCode.UPSTREAM_ERROR, detail=str(e)[:200])

    class RagRegisterRequest(BaseModel):
        question: str = ""
        title: str = ""
        content: str = ""
        coding_type: str | None = None
        concept_key: str | None = None
        session_id: str | None = None
        user_id: str | None = None  # 미지정 시 인증(user_id)에서 채움
        # #44: base 청크와 동형으로 등록 → 검색 클릭 시 chat 카드 동일 렌더 + 페르소나 필터.
        intent: str | None = None
        domain: str | None = None
        difficulty: str | None = None
        modi_keys: list | None = None
        payload: dict | None = None
        outcome: str | None = None
        reusability_score: float = 1.0

    @app.get("/api/search")
    async def rag_search(q: str, coding_type: str = "any", top: int = 8,
                         difficulty: str | None = None, domain: str | None = None,
                         user_id: str = Depends(get_user_id)):
        """하이브리드 검색. user_id(uuid) 지정 시 base(전역)+내 등록물로 한정.

        difficulty(easy|medium|hard)/domain 지정 시 페르소나 필터(#44) — 순위 불변, 노출만 좁힘.
        RAG_UPSTREAM 설정 시 rag-search 로 프록시(벡터 백엔드), 아니면 로컬.
        """
        if _RAG_UPSTREAM:
            return await _rag_up_get("/api/search", {
                "q": q, "coding_type": coding_type, "top": top, "user_id": user_id or "",
                "difficulty": difficulty or "", "domain": domain or ""})
        return JSONResponse(_rag_search(q, coding_type=coding_type, top=top,
                                        user_id=user_id or None,
                                        difficulty=difficulty, domain=domain))

    @app.post("/api/register")
    async def rag_register(req: RagRegisterRequest, user_id: str = Depends(get_user_id)):
        """결과물 등록 → 즉시 임베딩·색인 → 검색 반영(RAG 되먹임). 소유자=인증 user_id 우선."""
        if _RAG_UPSTREAM:
            body = req.model_dump()
            body["user_id"] = user_id or req.user_id  # 소유자 주입
            return await _rag_up_post("/api/register", body)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        res = registry_lib.register(
            question=req.question, title=req.title, content=req.content,
            coding_type=req.coding_type, concept_key=req.concept_key,
            user_id=(user_id or req.user_id), session_id=req.session_id, ts=ts,
            intent=req.intent, domain=req.domain, difficulty=req.difficulty,
            modi_keys=req.modi_keys, payload=req.payload, outcome=req.outcome,
            reusability_score=req.reusability_score,
        )
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/api/coverage")
    async def rag_coverage(coding_type: str = "any"):
        """질문 묶음의 재사용/근접/등록 비율(%)."""
        if _RAG_UPSTREAM:
            return await _rag_up_get("/api/coverage", {"coding_type": coding_type})
        return JSONResponse(_rag_coverage(_RAG_DEFAULT_QUERIES, coding_type=coding_type))

    @app.get("/api/query")
    async def rag_query(question: str, grade: int = 4, coding_type: str = "any"):
        """페르소나 도출: 질문+페르소나 → 개념·선수학습·연관·MODI·재사용 학습노트."""
        if _RAG_UPSTREAM:
            return await _rag_up_get("/api/query", {
                "question": question, "grade": grade, "coding_type": coding_type})
        from rag_demo_app import derive as _rag_derive

        return JSONResponse(_rag_derive(grade, coding_type, question))

    @app.get("/rag")
    async def rag_ui():
        """검색/도출/커버리지 UI. 로컬 파일 서빙(있으면), 없으면 업스트림 루트."""
        p = os.path.join(_SCRIPTS_DIR, "rag_demo.html")
        if os.path.exists(p):
            return FileResponse(p)
        if _RAG_UPSTREAM:
            return await _rag_up_get("/", {})
        return error_response(ErrorCode.NOT_FOUND, message="UI 파일 없음")

    @app.get("/rag/health")
    async def rag_health():
        if _RAG_UPSTREAM:
            return await _rag_up_get("/health", {})
        return {"status": "ok", "vector_enabled": _rag_vector_enabled(),
                "registered": registry_lib.count()}

    def _rag_registry_stats_upstream() -> dict | None:
        """프록시 모드: rag-search 의 등록 스토어 stats 조회(실패 시 None)."""
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(f"{_RAG_UPSTREAM}/api/registry/stats")
                if r.status_code < 400:
                    return r.json()
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────────────────────────────
    # 사용량·비용 리포트 (웹)
    # ──────────────────────────────────────────────────────────────────────
    #
    # 데이터는 rag-search(:8100)에 있지만 그건 컴포즈 내부 전용이라 링크로 열 수 없다.
    # 외부에 노출된 건 이 앱뿐이므로 여기서 프록시한다.
    #
    # ⚠ 접근 통제가 필수다. 학생이 쓰는 것과 **같은 도메인**이고, 리포트에는 사용자별
    #   사용량·비용이 담긴다. 그래서 기본은 '꺼짐'이다 — REPORT_TOKEN 이 없으면 404.
    #   토큰이 틀려도 403 이 아니라 404 를 준다(403 은 "여기 뭔가 있다"를 알려 준다).
    #   토큰은 URL 에 실려 접속 로그에 남으므로 운영 비밀이 아니라 '링크를 아는 사람만'
    #   수준의 장치로 취급한다.
    #
    # 시간 규약: 저장은 UTC(usage_reports.*_at_utc), 표시는 KST. day 는 타임스탬프가
    #   아니라 KST 영업일 라벨이므로 변환 대상이 아니다.

    _PAGE = 25   # 한 표에 뿌리는 행 수 — 수백 줄을 쏟으면 읽히지 않는다

    def _report_token() -> str:
        return os.getenv("REPORT_TOKEN", "").strip()

    def _report_authorized(token: str, header_token: str = "") -> bool:
        """상수시간 비교 — 길이/내용 차이로 토큰을 캐내지 못하게."""
        want = _report_token()
        if not want:
            return False
        return hmac.compare_digest((token or header_token or "").strip(), want)

    def _deny():
        raise HTTPException(status_code=404, detail="Not Found")

    async def _up_get(path: str, params: dict, timeout: float = 30.0) -> dict:
        if not _RAG_UPSTREAM:
            return {"ok": False, "error":
                    "RAG_UPSTREAM 이 설정돼 있지 않아 사용량 원장을 조회할 수 없습니다"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{_RAG_UPSTREAM}{path}", params=params)
                if r.headers.get("content-type", "").startswith("application/json"):
                    return r.json()
                return {"ok": False, "error": f"업스트림이 JSON 이 아님(HTTP {r.status_code})"}
        except Exception as e:
            return {"ok": False, "error": f"업스트림 조회 실패: {str(e)[:200]}"}

    async def _up_post(path: str, params: dict, timeout: float = 180.0,
                       body: dict | None = None) -> dict:
        """업스트림 POST. params 는 쿼리스트링, body 는 JSON 본문.

        본문을 따로 받는 이유: 분석 텍스트처럼 긴 값을 쿼리로 보내면 URL 길이 상한과
        인코딩에 걸린다. 짧은 제어값(day·store)은 쿼리, 내용은 본문으로 나눈다.
        """
        if not _RAG_UPSTREAM:
            return {"ok": False, "error": "RAG_UPSTREAM 미설정"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{_RAG_UPSTREAM}{path}", params=params, json=body)
                if r.headers.get("content-type", "").startswith("application/json"):
                    return r.json()
                return {"ok": False, "error": f"업스트림이 JSON 이 아님(HTTP {r.status_code})"}
        except Exception as e:
            return {"ok": False, "error": f"업스트림 호출 실패: {str(e)[:200]}"}

    def _llm_mode() -> str:
        from agent.claude_client import _use_local_cli
        return "cli" if _use_local_cli() else "api"

    def _qnum(raw, default, cast=float):
        """빈 문자열·쓰레기 값을 기본값으로 흡수한다.

        HTML 폼은 비어 있는 입력도 `budget_usd=` 로 **보낸다**. FastAPI 는 그걸
        float 로 파싱하려다 422 를 내고, 사용자는 리포트 대신 JSON 에러를 본다
        (2026-08-21 실제 발생). 조회 화면의 선택 입력은 못 읽으면 무시하는 게 맞다 —
        숫자 하나 때문에 화면 전체를 못 보는 건 과한 실패다.
        """
        try:
            v = cast(str(raw).strip())
        except (TypeError, ValueError):
            return default
        return v if v >= 0 else default

    def _preset_range(preset: str) -> tuple[str, str]:
        """빠른 기간 버튼 → (start, end). KST 기준 영업일 라벨."""
        today = datetime.now(_KST).date()
        if preset == "today":
            return today.isoformat(), today.isoformat()
        if preset == "7d":
            return (today - timedelta(days=6)).isoformat(), today.isoformat()
        if preset == "30d":
            return (today - timedelta(days=29)).isoformat(), today.isoformat()
        if preset == "month":
            return today.replace(day=1).isoformat(), today.isoformat()
        return "", ""

    def _render_or_500(fn, *a, **kw):
        try:
            import report_html
        except Exception as e:
            import html as _h
            return HTMLResponse(f"<h1>렌더러 로드 실패</h1><p>{_h.escape(str(e))}</p>",
                                status_code=500)
        return HTMLResponse(getattr(report_html, fn)(*a, **kw))

    # ── 상대 링크 기준점 ────────────────────────────────────────────────────
    # 앱은 자신이 /agent 아래 붙어 있는지 모른다(프록시가 붙였다 뗀다). 그래서 링크를
    # 절대경로로 못 쓰고 상대경로를 쓰는데, **상대경로는 현재 URL 의 디렉터리 기준**이라
    # 페이지 깊이마다 달라진다:
    #     /reports            → 디렉터리 /            → "reports"        (그대로)
    #     /report/live        → 디렉터리 /report/     → "../reports"
    #     /report/session/xxx → 디렉터리 /report/xxx/ → "../../reports"
    #
    # 이걸 안 맞추면 뒤로 가기가 /report/session/reports 로 풀려 "세션을 찾을 수
    # 없습니다"가 뜬다(2026-08-21 실제로 그랬다). 깊이는 라우트가 아는 값이므로
    # 라우트가 렌더러에 넘긴다.
    _ROOT_1 = "../"      # /report/{live}
    _ROOT_2 = "../../"   # /report/{archive,session}/{...}

    @app.get("/api/usage/report")
    async def usage_report_json(token: str = "", start: str = "", end: str = "",
                                user_id: str = "", limit_users: str = "30",
                                x_report_token: str = Header("")):
        """리포트 원본 JSON — 대시보드·자동화 연동용."""
        if not _report_authorized(token, x_report_token):
            _deny()
        return JSONResponse(await _up_get("/api/usage/report", {
            "start": start, "end": end, "user_id": user_id,
            "limit_users": _qnum(limit_users, 30, int)}))

    @app.get("/reports", response_class=HTMLResponse)
    async def usage_report_index(token: str = "", start: str = "", end: str = "",
                                 preset: str = "", budget_usd: str = "",
                                 user_id: str = "", limit_users: str = "",
                                 poff: str = "", pq: str = "", uoff: str = "",
                                 x_report_token: str = Header("")):
        """기간 화면 — 위에 전체 요약, 아래에 하루 한 줄로 쌓인다."""
        if not _report_authorized(token, x_report_token):
            _deny()
        budget = _qnum(budget_usd, 0.0)
        limit_users = _qnum(limit_users, _PAGE, int)
        if preset:
            start, end = _preset_range(preset)
        if not start and not end:
            # 기본은 최근 30일 — 이 화면의 목적이 '추세'라 하루만 보면 의미가 없다.
            start, end = _preset_range("30d")

        rep = await _up_get("/api/usage/report", {
            "start": start, "end": end, "user_id": user_id,
            "limit_users": limit_users,
            "project_offset": _qnum(poff, 0, int), "project_q": pq,
            "user_offset": _qnum(uoff, 0, int)})
        snaps = await _up_get("/api/usage/snapshots", {"start": start, "end": end})
        confirmed = {str(r.get("day")): r for r in (snaps.get("items") or [])}

        # 기간 끝날의 확정 분석이 있으면 그대로 보여준다(재생성 비용 0).
        insight = None
        last = confirmed.get(end)
        if last and last.get("has_insight"):
            one = await _up_get("/api/usage/snapshot", {"day": end})
            if one.get("ok"):
                insight = {"text": one.get("insight", ""),
                           "model": one.get("insight_model", ""),
                           "generated_at_utc": one.get("insight_at_utc", "")}

        return _render_or_500(
            "render_index", rep, budget_usd=budget, mode=_llm_mode(),
            form_action="", hidden_fields={"token": token} if token else {},
            detail_href="report", confirmed=confirmed, insight=insight,
            can_generate=True, insight_endpoint="report/insight", token=token,
            page_qs={"token": token, "start": start, "end": end,
                     "budget_usd": budget_usd, "user_id": user_id})

    @app.get("/report", response_class=HTMLResponse)
    async def usage_report_page(token: str = "", start: str = "", end: str = "",
                                user_id: str = "", limit_users: str = "",
                                budget_usd: str = "",
                                poff: str = "", pq: str = "", uoff: str = "",
                                x_report_token: str = Header("")):
        """하루(또는 지정 기간) 상세. 열 때마다 그 자리에서 집계한다."""
        if not _report_authorized(token, x_report_token):
            _deny()
        budget = _qnum(budget_usd, 0.0)
        limit_users = _qnum(limit_users, _PAGE, int)
        rep = await _up_get("/api/usage/report", {
            "start": start, "end": end, "user_id": user_id,
            "limit_users": limit_users,
            "project_offset": _qnum(poff, 0, int), "project_q": pq,
            "user_offset": _qnum(uoff, 0, int)})

        insight, day = None, (start or "")[:10]
        if day:
            one = await _up_get("/api/usage/snapshot", {"day": day})
            if one.get("ok") and one.get("insight"):
                insight = {"text": one["insight"], "model": one.get("insight_model", ""),
                           "generated_at_utc": one.get("insight_at_utc", "")}

        return _render_or_500(
            "render", rep, budget_usd=budget, mode=_llm_mode(),
            form_action="", hidden_fields={"token": token} if token else {},
            insight=insight, can_generate=bool(day),
            insight_endpoint="report/insight", token=token,
            page_qs={"token": token, "start": start, "end": end,
                     "budget_usd": budget_usd, "user_id": user_id})

    @app.get("/report/live", response_class=HTMLResponse)
    async def usage_report_live(token: str = "", minutes: str = "", refresh: str = "",
                                x_report_token: str = Header("")):
        """수업 중에 띄워 두는 실시간 화면 — 최근 N분, 자동 새로고침.

        사후 리포트만으로는 **수업 중에 대응할 수 없다.** 지금 붐비는지·지금 튕기고
        있는지를 봐야 쉬는 시간을 넣거나 진행 순서를 바꾼다.

        창을 짧게(기본 15분) 잡는 이유: 하루 전체를 매번 집계하면 새로고침마다
        무거워지고, 수업 중에 알고 싶은 건 '지금'이지 '오늘 평균'이 아니다.
        """
        if not _report_authorized(token, x_report_token):
            _deny()
        win = int(_qnum(minutes, 15, int)) or 15
        win = max(1, min(win, 240))          # 실시간 화면이 하루치를 긁지 않도록 상한
        every = int(_qnum(refresh, 10, int)) or 10
        every = max(5, min(every, 120))

        now = datetime.now(_KST)
        rep = await _up_get("/api/usage/report", {
            "start": (now - timedelta(minutes=win)).strftime("%Y-%m-%d %H:%M:%S"),
            "end": now.strftime("%Y-%m-%d %H:%M:%S"),
            # 실시간 화면은 표를 안 그리므로 목록은 최소로 — 새로고침 비용을 줄인다.
            "limit_users": 1,
        })
        qs = f"?token={quote(token)}" if token else ""
        try:
            import report_load_html as RL
            return HTMLResponse(RL.render_live(
                rep.get("load") or {}, window_min=win, refresh=every,
                root=_ROOT_1,
                stamp=now.strftime("%H:%M:%S"),
                health={"writeback": {"queued": _writeback_q.qsize(),
                                      "dropped": _writeback_dropped,
                                      "failed": _writeback_failed}},
                query=qs))
        except Exception as e:
            # 실시간 화면이 깨져도 수업은 계속된다 — 500 대신 사유를 보여 준다.
            return _render_or_500("render_error", "실시간 화면을 그리지 못했습니다",
                                  detail=str(e)[:200])

    @app.get("/report/session/{sid}", response_class=HTMLResponse)
    async def usage_report_session(sid: str, token: str = "",
                                   x_report_token: str = Header("")):
        """프로젝트 하나를 리포트 안에서 읽는다 — 대화와 산출물.

        왜 원본 JSON 링크가 아니었나: 관리자가 알고 싶은 건 "이 학생이 무엇을 묻고
        무엇을 만들었나"인데, JSON 을 그대로 열면 대화가 \n 이 박힌 한 줄로 뭉개져
        사실상 못 읽는다.

        왜 프론트(SPA)로 보내지 않나: 그쪽 URL 규약은 이 저장소가 모른다. 추측해서
        깨진 링크를 만드는 대신 리포트가 직접 보여 준다.

        서비스 영향: 읽기 전용이고 REPORT_TOKEN 뒤에 있다(학생 경로와 무관).
        조회는 세션 1건이라 리포트 페이지보다 가볍다.
        ⚠ 다만 **학생 대화 원문이 보인다.** 토큰이 곧 열람 권한이므로 추측하기 쉬운
          값을 쓰면 안 된다(입력 PII 는 redact_pii 로 이미 마스킹되지만 내용 자체는 남는다).
        """
        if not _report_authorized(token, x_report_token):
            _deny()
        # 경로 조작 차단 — 세션 id 문자셋만 허용한다.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            _deny()

        sess = await _up_get("/api/session/get", {"session_id": sid})
        # 업스트림이 {ok, session} 또는 세션 본문을 직접 줄 수 있어 둘 다 받는다.
        if isinstance(sess, dict) and "session" in sess:
            sess = sess.get("session") or {}
        if isinstance(sess, dict) and sess.get("raw"):
            sess = sess["raw"] if isinstance(sess["raw"], dict) else sess

        # 이 세션의 사용량 — "이 작품에 얼마 들었나"를 같은 화면에서 답한다.
        # 프로젝트 검색(project_q)이 session_id 로도 매칭되고 matched_cost 를 함께
        # 돌려주므로, 세션 전용 집계 엔드포인트를 새로 만들 필요가 없다.
        usage = None
        try:
            rep = await _up_get("/api/usage/report", {
                # 세션이 언제 만들어졌는지 모르므로 기간을 넓게 잡는다. 집계 대상이
                # session_id 하나로 좁혀지므로 넓은 기간이어도 비싸지 않다.
                "start": "2000-01-01", "end": "2099-12-31",
                "limit_users": 1, "project_q": sid, "project_limit": 1})
            mc = ((rep.get("projects") or {}).get("matched_cost") or {})
            if mc.get("turns"):
                usage = mc
        except Exception:
            usage = None   # 사용량을 못 구해도 대화·산출물은 보여 준다

        qs = f"?token={quote(token)}" if token else ""
        try:
            import report_load_html as RL
            return HTMLResponse(RL.render_session(sess if isinstance(sess, dict) else {},
                                                  usage=usage, back_qs=qs,
                                                  root=_ROOT_2))
        except Exception as e:
            return _render_or_500("render_error", "프로젝트를 열지 못했습니다",
                                  detail=str(e)[:200])

    @app.get("/report/archive/{day}", response_class=HTMLResponse)
    async def usage_report_archive(day: str, token: str = "", budget_usd: str = "",
                                   x_report_token: str = Header("")):
        """DB 에 굳혀 둔 그날 확정본. 라이브 집계와 달리 이후 변하지 않는다."""
        if not _report_authorized(token, x_report_token):
            _deny()
        # 경로 조작 차단 — 날짜 형식만 허용한다.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            _deny()
        one = await _up_get("/api/usage/snapshot", {"day": day})
        if not one.get("ok"):
            raise HTTPException(status_code=404, detail="확정본이 없습니다")

        insight = None
        if one.get("insight"):
            insight = {"text": one["insight"], "model": one.get("insight_model", ""),
                       "generated_at_utc": one.get("insight_at_utc", "")}
        return _render_or_500(
            "render", one.get("payload") or {}, title="일별 리포트 (확정본)",
            budget_usd=_qnum(budget_usd, 0.0), mode=one.get("llm_mode") or _llm_mode(),
            show_form=False, insight=insight, can_generate=True,
            insight_endpoint="../insight", token=token, root=_ROOT_2,
            confirmed_at_utc=one.get("generated_at_utc", ""))

    @app.post("/report/insight")
    async def usage_report_insight(token: str = "", day: str = "",
                                   x_report_token: str = Header("")):
        """AI 분석 (재)생성 — 화면의 버튼이 부르는 경로.

        LLM 호출이라 과금된다. 페이지 로드가 아니라 **명시적인 버튼**에만 붙인 이유다.
        """
        if not _report_authorized(token, x_report_token):
            _deny()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            return JSONResponse({"ok": False, "error": "날짜 형식이 올바르지 않습니다"},
                                status_code=400)
        # ★ 생성은 **여기(앱)** 에서 한다. rag-search 컨테이너에는 agent/ 도, anthropic
        #   SDK 도, Claude 인증도 없다(scripts/ 만 COPY) — 거기서 부르면
        #   "No module named 'agent'" 로 죽는다(2026-08-21 실제 증상).
        #   그리고 CLI(구독) 경로를 쓴다: 수업의 병목이 **분당 출력 토큰**이라
        #   분석이 같은 API 예산을 나눠 쓰면 학생 응답이 그만큼 밀린다.
        rep = await _up_get("/api/usage/snapshot", {"day": day})
        report = rep.get("payload") if rep.get("ok") else None
        if not report:
            report = await _up_get("/api/usage/report",
                                   {"start": day, "end": day, "limit_users": 50})
        try:
            import report_insight
            out = report_insight.generate(report or {}, mode=_llm_mode(), prefer_cli=True)
        except Exception as e:
            out = {"ok": False, "error": str(e)[:300]}

        # 만들었으면 rag-search 에 굳힌다(저장은 원천을 가진 쪽 역할).
        if out.get("ok"):
            try:
                await _up_post("/api/usage/insight", {"day": day},
                               body={"day": day, "text": out.get("text", ""),
                                     "model": out.get("model", "")})
            except Exception as e:
                out["store_error"] = str(e)[:160]
        if out.get("ok"):
            try:
                import report_html
                out["html"] = (report_html._md_lite(out.get("text", ""))
                               + f'<div class="ai-meta">{report_html._e(out.get("model", ""))}'
                                 ' · 방금 생성</div>')
            except Exception:
                pass
        return JSONResponse(out)

    @app.get("/api/registry/stats")
    async def rag_registry_stats():
        """등록 스토어 상태(#103) — 재배포에도 데이터가 보존되는지 코드 밖에서 즉시 검증.

        프록시 모드(RAG_UPSTREAM)에서 업스트림 조회가 실패해도 500 대신 로컬 stats로 대체.
        """
        if _RAG_UPSTREAM:
            up = _rag_registry_stats_upstream()
            if up is not None:
                return JSONResponse(up)
            s = registry_lib.stats()
            return JSONResponse({"ok": True, **s, "upstream": True})
        s = registry_lib.stats()
        return JSONResponse({"ok": True, **s, "upstream": False})
