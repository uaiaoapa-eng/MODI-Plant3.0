"""Sentry 통합 — 에러·성능·부하 관측 (self-hosted: sentry.luxrobo.net / project: edu-agent).

이 모듈이 커버하는 것(이슈 요구사항):
- 서버 이슈        : 처리되지 않은 예외 + SSE 스트리밍 중 예외를 자동·수동 캡처.
- 퍼포먼스 문제     : 트랜잭션/프로파일링 + LLM 호출 커스텀 span(지연·토큰·비용 measurement).
- 부하(로드) 제약  : 세션 동시성 거절·LLM 레이트리밋(429/529) 소진을 전용 태그로 캡처.
- 기타 이슈        : 취소·가드레일 분류 오류·세션 파일 손상 등을 breadcrumb/event 로 남김.

설계 원칙:
- SENTRY_DSN 이 없으면 **전체 no-op** → 로컬/테스트/CI 에서 자동 비활성(부작용 0).
- sentry-sdk 가 설치돼 있지 않아도 import 가 깨지지 않게, sentry_sdk import 는 모두 지연(lazy).
- PII 보호: guardrails.redact_pii 를 before_send/before_send_transaction 에 연결해
  Sentry 로 나가는 메시지·예외값·breadcrumb·요청 데이터까지 한 번 더 가린다(redact-at-source 연장).
- 내부 TLS 인터셉션(사설 CA) 환경 대비: SENTRY_CA_CERTS 로 ca 번들 경로를 주입할 수 있다.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

# 모듈 전역 초기화 플래그. 헬퍼들은 이 값으로 빠르게 no-op 분기한다.
_initialized = False


# ──────────────────────────────────────────────
# 환경변수 파서
# ──────────────────────────────────────────────

def _use_local_cli() -> bool:
    return os.getenv("USE_LOCAL_CLAUDE", "true").strip().lower() in ("true", "1", "yes")


def _sample_rate(name: str, default: float) -> float:
    """0.0~1.0 샘플레이트 환경변수 파싱. 잘못된 값이면 default 로 폴백."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return min(1.0, max(0.0, val))


def _release() -> str:
    """릴리스 식별자. SENTRY_RELEASE > git short sha > 패키지 버전 순.

    릴리스가 지정돼야 Sentry 에서 '어느 배포에서 회귀가 생겼는지' 추적이 된다.
    """
    explicit = os.getenv("SENTRY_RELEASE", "").strip()
    if explicit:
        return explicit
    sha = _git_sha()
    if sha:
        return f"edu-agent@{sha}"
    return "edu-agent@0.1.0"


def _git_sha() -> str:
    """배포 디렉터리의 git short sha. 실패하면 빈 문자열(릴리스는 버전으로 폴백)."""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ──────────────────────────────────────────────
# PII 스크러빙 (before_send / before_send_transaction)
# ──────────────────────────────────────────────

def _scrub(obj: Any, depth: int = 0) -> Any:
    """이벤트 안의 모든 문자열에 redact_pii 적용(재귀). 깊이 상한으로 폭주 방지.

    주민/전화/카드/이메일 패턴만 가리므로 스택트레이스·span 이름 등 일반 텍스트엔 영향 거의 없다.
    """
    if depth > 8:
        return obj
    # guardrails 의 입력경계 마스킹을 그대로 재사용 → 마스킹 규칙 단일 소스 유지.
    from agent.guardrails import redact_pii

    if isinstance(obj, str):
        return redact_pii(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub(v, depth + 1) for v in obj)
    return obj


def _before_send(event: dict, hint: dict) -> dict | None:
    """에러 이벤트 전송 직전 훅 — 노이즈 로거 이벤트 드롭 + 전체 이벤트 PII 스크럽."""
    try:
        logger_name = event.get("logger") or ""
        # sentry_sdk.ignore_logger() 는 record.name 과 '완전 일치'만 걸러낸다(logging.py의
        # `name not in ignored_loggers` 집합 조회). 그런데 langfuse/OpenTelemetry/httpx 등은
        # 서브모듈 단위 로거명(예: "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        # "httpcore.http11")으로 로깅해 ignore_logger("opentelemetry") 가 매치되지 않는다.
        # (EDU-AGENT-P: "Failed to export span batch..." 가 바로 이 사각지대로 새어나간 사례)
        # prefix 매치로 실제로 걸러지게 한다 — _ignore_noisy_loggers() 의 exact-match 등록은
        # 그대로 두되(무해), 여기서 이중 방어한다.
        if logger_name.startswith(_IGNORED_LOGGERS):
            return None
        return _scrub(event)
    except Exception:
        # 스크럽이 깨져도 관측은 살린다(원본 그대로 전송).
        return event


def _before_send_transaction(event: dict, hint: dict) -> dict | None:
    """트랜잭션(성능) 전송 직전 훅 — 요청 데이터만 스크럽(전체 재귀는 트랜잭션 양이 많아 회피)."""
    try:
        if isinstance(event.get("request"), dict):
            event["request"] = _scrub(event["request"])
        return event
    except Exception:
        return event


# ──────────────────────────────────────────────
# 초기화
# ──────────────────────────────────────────────

def is_enabled() -> bool:
    return _initialized


# LoggingIntegration(event_level="ERROR") 은 표준 logging 의 ERROR 로그를 Sentry 이벤트로
# 승격한다. 문제는 백그라운드 텔레메트리/HTTP 클라이언트(Langfuse 플러시·OpenTelemetry
# 익스포터·httpx/urllib3)가 self-hosted 서버(langfuse.luxrobo.net 등)로의 일시적 전송 실패
# 때 트레이스백을 통째로 logging.error 로 남긴다는 것. 이러면 in-app 프레임이 없는(=culprit
# 빈) "Traceback (most recent call last):" 제목의 phantom ERROR 이슈가 Sentry 에 쌓인다.
# 이런 백그라운드 로거는 이벤트 승격에서 제외한다 — 앱의 실제 오류는 FastAPI/Starlette/Asyncio
# 통합과 명시적 capture_chat_exception / note_llm_failure 로 여전히 잡힌다.
#
# "uvicorn.error"도 같은 부류다: 서버 기동 시 포트 바인딩 실패(OSError, 예: 배포 교체/재시작
# 창에서 이전 컨테이너가 포트를 아직 붙들고 있을 때) 를 uvicorn 이 logger.error(exc) 로 남기는데,
# 이건 in-app 프레임이 없는(culprit 빈) "[Errno N] error while attempting to bind on address ..."
# phantom ERROR 이슈로 쌓인다(EDU-AGENT-M). 운영 재시도/헬스게이트(deploy.yml --wait)가 이미
# 이 상황을 다루므로 앱 버그가 아니다 — 요청 처리 중 실제 예외는 FastAPI/Starlette 통합이 별도로 잡는다.
_IGNORED_LOGGERS = (
    "langfuse",
    "opentelemetry",
    "backoff",
    "httpx",
    "httpcore",
    "urllib3",
    "uvicorn.error",
)


def _ignore_noisy_loggers() -> None:
    """백그라운드 텔레메트리/HTTP 로거의 ERROR 로그가 Sentry 이벤트로 승격되지 않게 한다."""
    try:
        from sentry_sdk.integrations.logging import ignore_logger
    except Exception:
        return
    for name in _IGNORED_LOGGERS:
        try:
            ignore_logger(name)
        except Exception:
            pass


def init_sentry(component: str = "server") -> bool:
    """Sentry SDK 초기화. component = "server" | "cli".

    DSN 이 없거나 sentry-sdk 미설치면 False 를 반환하고 아무것도 하지 않는다(no-op).
    FastAPI 자동 계측이 요청을 잡으려면 app 생성 전에 호출해야 한다.
    """
    global _initialized
    if _initialized:
        return True

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        print("[sentry] sentry-sdk 미설치 — 관측 비활성 (pip install 'sentry-sdk[fastapi]')", flush=True)
        return False

    integrations = [
        FastApiIntegration(),
        StarletteIntegration(),
        AsyncioIntegration(),
        # logging.error → Sentry 이벤트, logging.info → breadcrumb. (이 앱은 print 위주라
        #  실효는 작지만, 표준 logging 을 쓰는 의존 라이브러리 에러는 자동으로 잡힌다.)
        LoggingIntegration(level=None, event_level="ERROR"),
    ]
    # redis 연결이 있을 때만 RedisIntegration 추가(미설치 환경에서 import 실패 무시).
    try:
        from sentry_sdk.integrations.redis import RedisIntegration

        integrations.append(RedisIntegration())
    except Exception:
        pass
    # API 모드(anthropic SDK) 전환 시 LLM 호출 자동 계측. CLI 모드에선 무해(미사용).
    try:
        from sentry_sdk.integrations.anthropic import AnthropicIntegration

        integrations.append(AnthropicIntegration())
    except Exception:
        pass

    init_kwargs: dict[str, Any] = dict(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=_release(),
        # 성능: 트랜잭션 + 프로파일. 트래픽이 적은 사내 서비스라 기본 1.0(전수).
        # 트래픽이 늘면 환경변수로 낮춘다(예: 0.2).
        traces_sample_rate=_sample_rate("SENTRY_TRACES_SAMPLE_RATE", 1.0),
        profiles_sample_rate=_sample_rate("SENTRY_PROFILES_SAMPLE_RATE", 1.0),
        # PII: 기본 비활성 + before_send 스크럽으로 이중 방어. (학생 입력 보호)
        send_default_pii=False,
        max_breadcrumbs=50,
        attach_stacktrace=True,
        max_request_body_size="medium",
        integrations=integrations,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
    )

    # 사설 CA / TLS 인터셉션 대비 — ca 번들 경로를 명시할 수 있게.
    ca_certs = os.getenv("SENTRY_CA_CERTS", "").strip()
    if ca_certs:
        init_kwargs["ca_certs"] = ca_certs

    sentry_sdk.init(**init_kwargs)
    _ignore_noisy_loggers()
    sentry_sdk.set_tag("component", component)
    sentry_sdk.set_tag("llm_mode", "cli" if _use_local_cli() else "api")

    _initialized = True
    print(
        f"[sentry] 활성화 — env={init_kwargs['environment']} "
        f"release={init_kwargs['release']} component={component}",
        flush=True,
    )
    return True


# ──────────────────────────────────────────────
# 컨텍스트 / 헬퍼 (모두 비활성 시 안전한 no-op)
# ──────────────────────────────────────────────

def set_session_context(
    session_id: str,
    user_id: str = "",
    mode: str | None = None,
    coding_type: str | None = None,
) -> None:
    """현재 스코프에 세션 식별자·태그를 건다. user_id 는 익명 디바이스 uuid 라 PII 아님."""
    if not _initialized:
        return
    import sentry_sdk

    if user_id:
        sentry_sdk.set_user({"id": user_id})
    sentry_sdk.set_tag("session_id", session_id or "")
    if mode is not None:
        sentry_sdk.set_tag("mode", mode)
    if coding_type is not None:
        sentry_sdk.set_tag("coding_type", coding_type)
    sentry_sdk.set_context("session", {
        "session_id": session_id or None,
        "mode": mode,
        "coding_type": coding_type,
    })


@contextmanager
def stream_scope() -> Iterator[None]:
    """SSE 스트림 제너레이터 전체 수명을 감싸는 격리 스코프.

    StreamingResponse 의 본문 제너레이터는 엔드포인트가 리턴된 뒤(자동 트랜잭션 종료 후)
    threadpool 에서 next() 단위로 실행된다. set_session_context 가 "현재 스코프"에만
    태그/유저를 얹으면, 스트림이 오래 실행되는 동안 그 스레드가 재사용되면서 동시에 처리된
    다른(훨씬 빈번한) 요청(예: /health 폴링)의 스코프/트랜잭션이 남아있다가 뒤섞여 exception
    culprit 이 엉뚱한 라우트로 찍힌다. 이 스트림 전용 isolation scope 로 그 오염을 막는다.
    """
    if not _initialized:
        yield
        return
    import sentry_sdk

    with sentry_sdk.isolation_scope():
        yield


def add_breadcrumb(message: str, category: str = "app", level: str = "info", **data: Any) -> None:
    """가벼운 흐름 기록(phase 전환·취소·재시도 등). 이벤트 발생 시 직전 맥락으로 따라붙는다."""
    if not _initialized:
        return
    import sentry_sdk

    sentry_sdk.add_breadcrumb(message=message, category=category, level=level, data=data or None)


def capture_load_constraint(kind: str, message: str = "", **tags: Any) -> None:
    """부하(로드) 제약 이벤트를 전용 태그로 캡처.

    kind 예: "session_busy"(동시 턴 거절), "llm_rate_limit"(LLM 429/529 소진).
    Sentry 에서 tag:load_constraint 로 필터·알림을 걸 수 있다.
    """
    if not _initialized:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.level = "warning"
        scope.set_tag("load_constraint", kind)
        for k, v in tags.items():
            scope.set_tag(k, v)
        sentry_sdk.capture_message(message or f"load constraint: {kind}", level="warning")


_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "429", "overloaded", "529",
)


def classify_load_constraint(message: str) -> str | None:
    """부하/용량성 실패를 load_constraint 종류로 분류. 해당 없으면 None.

    - 세션/사용량 한도(구독 소진, 예: "You've hit your session limit · resets 7:20pm")
      → "llm_quota_limit". 코드 버그가 아니라 용량 신호이므로 error 이슈가 아닌
      load_constraint 로 분류해 Sentry 노이즈를 줄이고 부하 알림으로 필터되게 한다.
    - 레이트리밋/과부하(429/529) → "llm_rate_limit".

    세션 한도 메시지엔 429 가 함께 오는 경우가 많아(EDU-AGENT-5) 재시도 무의미한
    quota 를 먼저 판정한다. 마커는 agent.retry 와 공유해 분류 기준이 갈라지지 않게 한다.
    """
    low = (message or "").lower()
    try:
        from agent.retry import QUOTA_LIMIT_MARKERS
    except Exception:  # retry 모듈 부재 시에도 관측이 깨지지 않게 폴백.
        QUOTA_LIMIT_MARKERS = ("session limit", "usage limit", "hit your limit", "resets ")
    if any(m in low for m in QUOTA_LIMIT_MARKERS):
        return "llm_quota_limit"
    if any(m in low for m in _RATE_LIMIT_MARKERS):
        return "llm_rate_limit"
    return None


def capture_chat_exception(exc: BaseException, **tags: Any) -> None:
    """채팅/스트리밍 경로의 예외 캡처. 레이트리밋·사용량 한도면 load_constraint 로도 분류.

    SSE 스트리밍은 응답 본문이 엔드포인트 리턴 후 생성돼 FastAPI 자동 트랜잭션이
    예외를 못 잡는 경우가 있어, 여기서 명시적으로 캡처한다.
    """
    if not _initialized:
        return
    import sentry_sdk

    kind = classify_load_constraint(str(exc))
    with sentry_sdk.new_scope() as scope:
        for k, v in tags.items():
            scope.set_tag(k, v)
        if kind:
            scope.set_tag("load_constraint", kind)
        sentry_sdk.capture_exception(exc)


def note_llm_failure(exc: BaseException, **tags: Any) -> None:
    """LLM 호출 실패 메모. 레이트리밋·사용량 한도면 load_constraint 이벤트, 아니면 breadcrumb 만.

    (예외 자체는 상위에서 capture_chat_exception 으로 다시 잡히므로 여기선 부하 신호만 남긴다.)
    """
    if not _initialized:
        return
    kind = classify_load_constraint(str(exc))
    if kind:
        capture_load_constraint(kind, str(exc)[:200], **tags)
    else:
        add_breadcrumb(f"LLM 호출 실패: {str(exc)[:200]}", category="llm", level="warning", **tags)


@contextmanager
def llm_span(op: str, description: str, **data: Any) -> Iterator[Any]:
    """LLM 호출을 트레이싱 span 으로 감싼다(퍼포먼스 계측).

    비활성 시 None 을 yield 하는 무해한 컨텍스트가 된다. 호출 측은:
        with llm_span("ai.run.claude_cli", "model=...") as span:
            ...
            tag_llm_usage(span, usage, cost)
    """
    if not _initialized:
        yield None
        return
    import sentry_sdk

    with sentry_sdk.start_span(op=op, description=description) as span:
        for k, v in data.items():
            try:
                span.set_data(k, v)
            except Exception:
                pass
        yield span


def tag_llm_usage(span: Any, usage: Any, cost_usd: float | None) -> None:
    """span 에 토큰/비용 데이터를 붙이고, 트랜잭션엔 measurement 로 집계.

    usage 는 CLI dict({"input_tokens","output_tokens",...}) 또는 SDK Usage 객체.
    """
    if not _initialized or span is None:
        return
    import sentry_sdk

    inp = out = None
    if isinstance(usage, dict):
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
    elif usage is not None:
        inp = getattr(usage, "input_tokens", None)
        out = getattr(usage, "output_tokens", None)

    try:
        if inp is not None:
            span.set_data("llm.input_tokens", inp)
        if out is not None:
            span.set_data("llm.output_tokens", out)
        if cost_usd is not None:
            span.set_data("llm.cost_usd", cost_usd)
        # 트랜잭션 measurement — Sentry Performance 에서 분포/p95 로 본다.
        if inp is not None:
            sentry_sdk.set_measurement("llm_input_tokens", inp, "none")
        if out is not None:
            sentry_sdk.set_measurement("llm_output_tokens", out, "none")
        if cost_usd is not None:
            sentry_sdk.set_measurement("llm_cost_usd", cost_usd, "none")
    except Exception:
        pass


def flush(timeout: float = 2.0) -> None:
    """버퍼된 이벤트 강제 전송(프로세스 종료 직전 등)."""
    if not _initialized:
        return
    import sentry_sdk

    sentry_sdk.flush(timeout=timeout)
