# Sentry 관측 연동 (에러 · 성능 · 부하)

self-hosted Sentry( https://sentry.luxrobo.net )의 **edu-agent** 프로젝트(id 11, platform `python-fastapi`)로
에러·성능·부하 신호를 보낸다. 코드는 `agent/observability.py` 한 곳에 모여 있고, `SENTRY_DSN`이 없으면
전체가 **no-op**(로컬/테스트 자동 비활성)이다.

## 무엇을 잡는가 (이슈 요구사항 매핑)

| 요구 | 어떻게 |
|------|--------|
| **서버 이슈** | FastAPI/Starlette 통합으로 처리되지 않은 예외 자동 캡처 + SSE 스트리밍 중 예외를 `server.py`에서 명시 캡처(`capture_chat_exception`). |
| **퍼포먼스 문제** | 트랜잭션(traces) + 프로파일링(profiles). LLM 호출은 `claude_client.py`에서 커스텀 span(`ai.run.claude_cli`, `…stream`)으로 감싸 지연·입출력 토큰·비용을 measurement 로 집계. |
| **부하(로드) 제약** | 세션 동시성 거절(`session_busy`)과 LLM 레이트리밋 소진(`llm_rate_limit`)을 `load_constraint` 태그로 캡처. |
| **기타 이슈** | 재시도/취소/가드레일 오류 등을 breadcrumb 으로 남겨 이벤트 직전 맥락에 첨부. |

PII 보호: Sentry 로 나가는 메시지·예외값·요청 데이터는 `before_send`/`before_send_transaction`에서
`guardrails.redact_pii`로 한 번 더 마스킹된다(주민/전화/카드/이메일). `send_default_pii=False`.

## 환경 (3개)

`SENTRY_ENVIRONMENT` 값으로 구분한다.

| 환경 | 용도 | traces/profiles | 비고 |
|------|------|-----------------|------|
| `dev` | 개발/로컬 검증 | 1.0 / 1.0 | 전수 수집 |
| `onprem` | **사내 서버(현재 운영)** | 1.0 / 1.0 | docker 기본 오버레이 |
| `release` | 클라우드 실서버(향후) | 0.2 / 0.2 | 트래픽 대비 표본 |

환경별 비밀-아닌 설정은 `deploy/dev.env`, `deploy/onprem.env`, `deploy/release.env`에 분리돼 있다.
비밀값(LLM 키, DSN)은 `.env`(또는 시크릿 매니저)에만 둔다.

## 배포

```bash
# 사내 서버(onprem) — 기본
docker compose up -d --build

# dev
EDU_AGENT_ENV_FILE=deploy/dev.env docker compose up -d --build

# 클라우드 실서버(release)
EDU_AGENT_ENV_FILE=deploy/release.env docker compose up -d --build
```

docker-compose 는 `.env`(공통+비밀) 다음에 `deploy/<env>.env`(환경 오버레이)를 덧씌운다.
(env_file 장문 문법은 Docker Compose v2.24+ 필요.)

로컬 직접 실행은 `.env`의 `SENTRY_ENVIRONMENT` 값을 그대로 쓴다.

## 설정 키 (`.env`)

| 키 | 설명 |
|----|------|
| `SENTRY_DSN` | 공개 인입 키. 비우면 관측 전체 비활성. |
| `SENTRY_ENVIRONMENT` | `dev` / `onprem` / `release` |
| `SENTRY_TRACES_SAMPLE_RATE` | 트랜잭션 샘플링 0.0~1.0 |
| `SENTRY_PROFILES_SAMPLE_RATE` | 프로파일 샘플링 0.0~1.0 |
| `SENTRY_RELEASE` | 비우면 git short sha → 패키지버전 순 자동 |
| `SENTRY_CA_CERTS` | 사설 CA/TLS 인터셉션 환경에서만 ca 번들 경로 |

## TLS 참고

Sentry 호스트 인증서는 Let's Encrypt(공개 CA, 전체 체인 정상)라 표준 Linux/Docker(`ca-certificates`)에서
검증·전송이 정상 동작한다. 사내망이 TLS 를 사설 CA 로 인터셉트하는 경우에만 `SENTRY_CA_CERTS`로 해당
번들 경로를 지정한다.

## 점검

```python
# DSN 설정 후 한 줄 검증(에러 1건 발생 → Sentry 에 떠야 함)
SENTRY_DSN=... SENTRY_ENVIRONMENT=dev python -c "import sentry_sdk, agent.observability as o; o.init_sentry('cli'); 1/0"
```

> 주의: 이벤트가 Sentry UI 에 보이려면 서버측 이벤트 처리 파이프라인(Relay→Kafka→Snuba→ClickHouse)이
> 살아 있어야 한다. Relay 가 HTTP 200 을 줘도 파이프라인이 멈춰 있으면 UI 에 안 뜬다(인프라 점검 필요).
