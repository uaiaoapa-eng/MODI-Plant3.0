# Sonnet 업무지시서 (Work Orders) — 2026-07

상위 계획: `docs/design/progress-report-cost-rag-2026-07.md` (Fable 5 작성).
이 문서는 각 작업을 **Sonnet 단독 세션 하나로 완결 가능한 단위**로 쪼갠 지시서다. 작업자는 각 항목의 "지시 프롬프트" 블록을 그대로 받아 수행한다.

## 공통 규칙 (모든 작업에 적용)
- 브랜치: `fix/…` 또는 `feat/…` + 이슈번호 (예 `fix/short-confirmation-EDU-agent-9`). master 직접 커밋 금지, PR로 제출.
- 새 동작에는 반드시 **env 킬스위치**를 두고 기본값을 보수적으로 설정 (기존 관례: `DIRECT_SERVE`, `REUSE_SEED_EDIT` 참고).
- 테스트: `pytest tests/ -x -q` 통과 + 변경 기능의 회귀 테스트 추가. 린트: `ruff check`.
- Langfuse 계측을 건드리면 스코어 이름·타입을 기존 컨벤션(`agent/orchestrator_stream.py` `_emit_turn_scores`)에 맞출 것.
- 프로덕션은 CLI 구독 모드(`USE_LOCAL_CLAUDE=true`) — API 전용 기능(max_tokens, cache_control)에 의존하는 수정 금지.
- 완료 보고: 변경 요약 + 검증 로그(테스트 출력) + 롤백 방법 1줄.

---

## P0 — 안정화 (우선순위 ★★★, 순서대로)

### W1. 이슈 #73 — `_looks_like_short_confirmation` AttributeError 수정
- **배경**: Sentry EDU-AGENT-9. `StreamOrchestrator`에 없는 메서드를 호출해 런타임 크래시.
- **대상**: `agent/orchestrator_stream.py`
- **지시 프롬프트**:
  > `agent/orchestrator_stream.py`에서 `_looks_like_short_confirmation` 호출부를 찾아라. 이 메서드는 리팩토링 중 삭제/이름변경된 것으로 보인다. git log로 원래 구현을 찾아 복원하거나, 호출 의도(짧은 긍정 답변 감지)에 맞는 최소 구현을 추가하라. 크래시 재현 테스트를 `tests/`에 먼저 작성하고(red), 수정 후 green을 확인하라. 이슈 #73 참조.
- **완료 기준**: 재현 테스트 green, 기존 테스트 전체 통과, PR에 `Fixes #73`.

### W2. 이슈 #81 — `'list' object has no attribute 'get'` 수정
- **배경**: Sentry EDU-AGENT-B. LLM 응답 파싱 경로에서 dict 가정이 깨짐(리스트 반환 케이스).
- **대상**: Sentry 스택트레이스가 가리키는 파싱 지점 (orchestrator 또는 intent/tool 파서)
- **지시 프롬프트**:
  > 이슈 #81의 Sentry 스택트레이스를 확인하고, `.get()`을 호출하는 지점에서 리스트가 들어오는 경우를 재현하는 단위 테스트를 작성하라. `isinstance` 방어 + 리스트인 경우의 합리적 처리(첫 dict 요소 사용 또는 스킵+로그)를 구현하라. 유사 패턴이 같은 파일에 더 있는지 grep으로 훑고 같은 방어를 적용하라.
- **완료 기준**: 재현 테스트 green, `Fixes #81`.

### W3. 이슈 #80 — 세션 리밋(429) 대응
- **배경**: CLI 구독 모드에서 세션 리밋 도달 시 원시 에러가 그대로 노출되고 턴이 실패함.
- **대상**: `agent/claude_client.py` (CLI 호출 경로), 에러 표면화 지점
- **지시 프롬프트**:
  > CLI 모드 응답에서 `api_error_status: 429` / "session limit" 문자열을 감지하는 처리를 추가하라. 동작: (1) 지수 백오프 재시도 N회(env `CLI_429_RETRIES`, 기본 2), (2) 최종 실패 시 사용자에게 한국어 안내 메시지("잠시 후 다시 시도해 주세요" + 리셋 시각이 파싱되면 포함)를 스트림으로 반환하고 세션은 정상 종료, (3) Sentry에는 warning 레벨로만 보고(에러 스팸 방지), (4) Langfuse BOOLEAN 스코어 `rate_limited` 추가. 429 응답을 mock한 테스트 포함.
- **완료 기준**: mock 테스트 green, 429 시 크래시 없이 안내 메시지 반환, `Fixes #80`.

### W4. simulate_batch 정기 회귀 상시화
- **배경**: 정확성 리그레션을 배포 전에 잡을 장치가 수동 실행뿐.
- **대상**: `.github/workflows/`, `scripts/simulate_batch.py`
- **지시 프롬프트**:
  > `scripts/simulate_batch.py`를 CI에서 돌릴 수 있게 하라. self-hosted runner에서 rag-search(:8100)와 앱이 떠 있는 환경을 전제로, (1) PR마다 또는 nightly로 실행하는 워크플로 추가, (2) PASS율이 직전 결과 대비 하락하면 실패 처리(결과 JSON을 아티팩트로 보존, 기준값은 리포지토리 내 baseline 파일), (3) 서비스 미가동 시 skip(하드 실패 금지). 기존 CI 구성(PR#6 self-hosted runner)을 먼저 읽고 같은 방식을 따르라.
- **완료 기준**: 워크플로 그린 런 1회 증빙, baseline 갱신 절차를 README 또는 워크플로 주석에 기록.

---

## P1 — 실측·튜닝 (★★☆, W1~W3 뒤 착수 가능·병렬 OK)

### W5. langfuse_cohort.py — tier별 코호트 리포트 자동화
- **배경**: 절감률 실측의 핵심 도구. 현재 스크래치패드 수준 스크립트.
- **대상**: `scripts/langfuse_cohort.py` (정식 위치로 이동/정리)
- **지시 프롬프트**:
  > Langfuse API로 최근 N일 트레이스를 조회해 코호트 리포트를 만드는 `scripts/langfuse_cohort.py`를 정식화하라. 산출: (1) `reuse_tier`(direct_serve/near/cold)별 턴 수·평균 출력토큰·평균 지연·비용, (2) `direct_served` rate와 `direct_serve_score` 분포, (3) `reuse_top1`/`reuse_cand_score` 히스토그램(TAU 튜닝 근거), (4) cold 대비 tier별 절감률 계산. 출력은 마크다운 표 + JSON. env는 `.env`의 `LANGFUSE_*` 재사용. 네트워크 없는 환경 대비 `--from-json` 오프라인 모드 포함. 스코어 이름은 `agent/orchestrator_stream.py`의 `_emit_turn_scores`에서 확인해 하드코딩 오타를 피하라.
- **완료 기준**: 실서버 1회 실행 리포트 첨부, 단위 테스트(집계 로직, mock 데이터).

### W6. 직접서브 오판 신호 계측
- **배경**: direct_serve가 잘못 발동하면 사용자가 곧바로 수정 요청을 한다 — 이게 오판율 프록시.
- **대상**: `agent/orchestrator_stream.py`
- **지시 프롬프트**:
  > direct_served 턴 직후 같은 세션의 다음 턴이 수정 요청(intent=fix/modify)이면 Langfuse BOOLEAN 스코어 `direct_serve_followup_fix`를 남겨라. 세션 상태에 "직전 턴이 direct_serve였는지" 플래그 하나만 추가하는 최소 구현. 기존 `_emit_turn_scores` 컨벤션을 따르고, 테스트는 `tests/test_reuse_instrumentation.py`에 추가.
- **완료 기준**: 테스트 green, 스코어가 시뮬레이션 시나리오에서 기록됨 확인.

### W7. TAU·MIN_SCORE 튜닝 하네스
- **배경**: TAU_REUSE=0.62/TAU_NEAR=0.48/MIN_SCORE=90은 보수적 초기값. near-miss 데이터로 최적화 필요.
- **대상**: `scripts/` 신규 스크립트
- **지시 프롬프트**:
  > W5의 near-miss 데이터(reuse_top1/cand_score 분포)와 `/api/simulate` 배치를 입력으로, TAU_REUSE/TAU_NEAR 값을 그리드 스윕하며 (reuse 발동률, 오발동 추정률) 곡선을 출력하는 `scripts/tau_sweep.py`를 작성하라. LLM 호출 없이 저장된 점수만 재판정한다("게이트 재판정"은 `scripts/search_lib.py`의 `_decision` 로직 재사용). 권장 TAU를 리포트로 제안하되 코드 기본값 변경은 하지 말 것(변경은 별도 PR로 사람이 결정).
- **완료 기준**: 스윕 리포트 1부, 로직 단위 테스트.

---

## P2 — 적중률 확대 (★★☆, P1 데이터 확인 후)

### W8. 콘텐츠 팩토리 파일럿 (사전제작)
- **배경**: 직접서브 절감률의 최대 지렛대. 설계는 `docs/design/content-factory-test-design.md`에 있음.
- **대상**: `scripts/` 신규 + rag-search write-back 경로
- **지시 프롬프트**:
  > `docs/design/content-factory-test-design.md`를 읽고, 표준 커리큘럼 요청 목록(초등 수학·과학 파일럿 범위)에 대해 (1) 각 요청을 실제 빌드 파이프라인으로 1회 생성, (2) 품질 검수 게이트(빌드 성공 + 만족도 검증 ≥90) 통과분만 `/api/writeback`으로 등록, (3) 등록 후 `/api/simulate` coverage로 해당 질의군의 reuse율 상승을 전/후 비교하는 파이프라인 스크립트를 작성하라. 생성 비용이 들므로 요청 목록은 파일로 분리하고 `--limit`·`--dry-run` 필수.
- **완료 기준**: 파일럿 N건 등록 전/후 coverage 비교 리포트, dry-run 테스트.

---

## 착수 순서 요약
```
W1 → W2 → W3 (직렬, 안정화) ─┐
W4 (병렬 가능)                ├→ W5 → W6 → W7 (실측·튜닝) → W8 (사전제작)
```
- W1~W4는 서로 독립이므로 세션을 나눠 병렬 수행 가능.
- W7의 TAU 변경, W8의 본격 확대는 **사람 승인 게이트** — Sonnet은 리포트까지만.
