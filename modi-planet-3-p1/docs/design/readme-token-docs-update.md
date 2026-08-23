# README에 토큰 쿼터·에러 문서 반영 설계문서

> 작성: Claude Opus 4.8 + walter | 날짜: 2026-07-14 | 상태: Draft
> 규칙: 실제 README·코드를 읽고 검증한 뒤 작성(§2 근거).

## 1. 배경과 목표

최근 토큰 쿼터·에러 구조화·채팅 무응답 해소 작업으로 3개 문서가 추가됐으나 **README.md에서 전혀 발견되지 않는다**(설계 문서 표·env·API 표 어디에도 없음). 신규 협업자·운영자가 토큰 쿼터 정책, SSE 에러 계약, 무응답 원인을 README만 보고는 알 수 없다. 이 설계는 README.md 한 파일만 편집해 (A) 3개 문서를 설계 문서 표에 링크하고, (B) 문서가 기술하는 **실제 표면**(`QUOTA_*`·`SSE_ERROR_AS_TOKEN` env, `GET /quota` 엔드포인트, SSE 에러 이벤트)을 README의 해당 섹션에 짧게 반영해 문서를 실제로 "찾아갈 수 있게" 한다.

## 2. 현재 상태 (검증됨)

| 확인한 사실 | 근거 |
|---|---|
| README 설계 문서 표에 토큰 3개 문서 누락 | `README.md:430-442` (표에 rag/langfuse/session 등만) |
| 대상 문서 3개 실재 | `docs/design/token-quota-and-error-structure.md` · `docs/api/sse-error-contract.md` · `docs/design/chat-error-surfacing-and-usage-turns-fix.md` |
| env 섹션에 QUOTA_*·SSE_ERROR_AS_TOKEN 없음 | `README.md:296-320` (LLM/RAG/관측/런타임만) |
| API 표에 GET /quota 없음 | `README.md:341-347` (/chat·/health만) |
| `GET /quota` 실재 | `server.py:467` `quota_status()` (enabled·scope·limit·max_turns·turns_remaining 반환) |
| QUOTA_* env 실재 | `server.py:88-100` (ENABLED·SCOPE·DAILY_WEIGHTED_TOKENS·DAILY_MAX_TURNS·PER_IP) |
| SSE_ERROR_AS_TOKEN 실재(기본 true) | `server.py`(#147 머지, `SSE_ERROR_AS_TOKEN` 선언) |
| SSE 에러 이벤트 계약 | `docs/api/sse-error-contract.md`(#148 머지) / `agent/errors.py:45-122` |
| Sentry load_constraint 안내는 이미 있음 | `README.md:365-366` |

## 3. 설계

### 3.1 변경 개요 (README.md만 편집)

1. **설계 문서 표**(`README.md:430-442`)에 3행 추가 — 각 문서 한 줄 요약과 함께:
   - `docs/design/token-quota-and-error-structure.md` — 사용자 토큰 쿼터(3계층: Redis 집행/MySQL usage_turns 분석/Langfuse 관측) + 에러 응답 구조화 설계.
   - `docs/api/sse-error-contract.md` — `/chat` SSE 에러 이벤트 계약(프론트 인계용): code·message·retryable·retry_after·token 폴백.
   - `docs/design/chat-error-surfacing-and-usage-turns-fix.md` — 쿼터 차단 무응답 원인·해소(SSE_ERROR_AS_TOKEN) + usage_turns 500.
2. **env 섹션**(`README.md:296-320`)에 쿼터 블록 주석 추가(값 설명은 간단히, 상세는 `.env.example`·설계문서로):
   `QUOTA_ENABLED`·`QUOTA_SCOPE`·`QUOTA_DAILY_MAX_TURNS`·`QUOTA_DAILY_WEIGHTED_TOKENS`·`SSE_ERROR_AS_TOKEN`.
3. **API 표**(`README.md:341-347`)에 `GET /quota` 1행 추가 + `/chat` 설명에 "오류는 SSE `type:error` 이벤트로 전달(상세: sse-error-contract.md)" 한 줄.

### 3.2 인터페이스 계약

문서/표의 사실은 코드에서 그대로 옮긴다(상상 금지). 예:

```
| GET | `/quota` | 남은 일 쿼터 조회 (enabled·scope·limit·max_turns·turns_remaining). QUOTA_ENABLED off여도 현재 누적 표시. |
```

### 3.3 데이터 변경

없음(문서만).

## 4. 하지 않는 것 (Non-goals)

- 코드·env 기본값·쿼터 정책 변경 — README 텍스트만 편집. `server.py`·`.env.example`·`deploy/*.env` 건드리지 않는다.
- 토큰 관련 3개 문서 자체의 내용 수정 — 링크·요약만.
- README 다른 섹션 리라이팅·구조 개편 — 위 3곳에 최소 추가만.
- `.env.example` 동기화 — 별개 작업(이번 범위 아님).
- 영어 번역·다국어 — 기존 README는 한국어, 그대로.

## 5. 엣지 케이스와 결정 사항

| 상황 | 결정 |
|---|---|
| env 상세를 README에 다 적을까 | 아니오 — README엔 핵심 5개 키만, 전체는 `.env.example`·설계문서로 위임(기존 컨벤션 `README.md:296` "전체 목록은 .env.example"). |
| SSE_ERROR_AS_TOKEN 위치 | env 쿼터 블록에 함께(에러 가시화가 쿼터 UX의 일부). |
| 링크 형식 | 기존 표와 동일한 `[`경로`](경로)` 마크다운. |

## 6. 구현 이슈 분해

단일 파일(README.md) 편집이라 이슈 1개. 상위 추적 이슈 없음.

| # | 이슈 제목 | 의존 | 검증 명령어 |
|---|---|---|---|
| 1 | [Task] README에 토큰 쿼터·SSE 에러 문서 반영 (링크 표 + env + /quota) | 없음 | `grep -q "token-quota-and-error-structure" README.md && grep -q "sse-error-contract" README.md && grep -q "/quota" README.md && grep -q "SSE_ERROR_AS_TOKEN" README.md` |

## 7. 전체 완료 기준

- [ ] 설계 문서 표에 토큰 3개 문서 링크(경로 실재·오타 없음)
- [ ] env 섹션에 QUOTA_*·SSE_ERROR_AS_TOKEN 블록
- [ ] API 표에 GET /quota + /chat 에러 안내 한 줄
- [ ] 링크 경로가 실제 파일과 일치(`ls` 대조)
