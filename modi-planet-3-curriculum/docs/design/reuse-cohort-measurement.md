# 재사용 코호트 측정 + 직접서브 임계 하향 검토 설계문서

> 작성: Claude Opus 4.8 + walter | 날짜: 2026-07-08 | 상태: Draft
> 규칙: 이 문서를 쓰기 **전에** 관련 코드를 실제로 읽고 검증할 것. 상상으로 쓴 앵커는 구현을 실패시킨다.

## 1. 배경과 목표

EDU-27 재사용 파이프라인(PR#85 직접서브 → PR#88 kind 한정 검색 → PR#89 vec 승격 → PR#90 승격 계측 → PR#91 문서 복원)이 2026-07-08 프로덕션 전 구간 검증됐다. 단건 실측으로는 직접서브가 생성 대비 비용 -95%($0.203→$0.011)·지연 -79%(94s→20s)를 보였으나, **집계 도구가 저장소에 없어** 실트래픽이 쌓여도 코호트(티어별·vec승격별·문서복원별) 절감을 재현 가능하게 측정할 수 없다(이번까지 세션 스크래치패드 일회성 스크립트로만 측정 — 유실됨).

또한 직접서브 만족도 거절 사례 2건(85점 "장애물 회피"=아까운 거절, 60점 "리듬게임"=정당한 거절)이 관측됐다. **85~89 구간**은 임계(`DIRECT_SERVE_MIN_SCORE=90`)를 5점만 낮추면 절감으로 전환되는 후보군이므로, 표본이 쌓이면 하향 여부를 데이터로 판단해야 한다.

이 설계 후: ① `scripts/lf_cohort.py` 한 번 실행으로 코호트 리포트가 나오고, ② `docs_restored`가 Langfuse 스코어로 발행돼 UI에서 필터 가능하며, ③ 85~89 구간 판단 기준과 실행 절차가 문서화된다.

## 2. 현재 상태 (검증됨)

| 확인한 사실 | 근거 (파일:라인) |
|---|---|
| 직접서브 accept 규칙: `score >= MIN_SCORE and not delta` | `agent/direct_serve.py:115` |
| `MIN_SCORE` = env `DIRECT_SERVE_MIN_SCORE`, 기본 90, 0~100 클램프 | `agent/direct_serve.py:41` |
| Langfuse 스코어 발행 지점은 `_emit_turn_scores` 한 곳 | `agent/orchestrator_stream.py:744-770` |
| 발행 중인 코호트 스코어: `reuse_vec_promoted`(BOOLEAN, 게이트 돈 턴만) / `reuse_tier`(CATEGORICAL: direct_serve·near·cold) / `direct_served`(BOOLEAN) / `direct_serve_score`(NUMERIC) — 뒤 2개는 검증 돈 턴만 | `agent/orchestrator_stream.py:747,761,764,767` |
| **`docs_restored`는 스코어 미발행** — `self._direct_served["docs_restored"]`로만 존재, trace metadata·done 이벤트에 실림 | `agent/orchestrator_stream.py:2473,657,2599` |
| `self._direct_served` dict 구조: `{score, accept, delta, ok, source_title, docs_restored}` | `agent/orchestrator_stream.py:2448-2473` |
| 코호트 측정 스크립트 저장소에 없음 (`scripts/`에 cohort/langfuse/lf_ 매칭 0건) | `ls scripts/` (2026-07-08) |
| Langfuse 공개 API: `GET /api/public/traces?limit=&orderBy=timestamp.desc`(목록, metadata 포함) + `GET /api/public/traces/{id}`(상세, scores 배열 포함), Basic auth(public:secret) | 이번 세션 실사용 검증 (trace 23949925 등) |
| 트레이스 상세에 `totalCost`(USD)·`latency`(초) 필드 존재 | 이번 세션 실사용 검증 |
| 합성/테스트 유저 프리픽스: `d3u-`, `qu-`, `meas-user`, `u1`, `sim-`, `verify-` (userId 필드) | 메모리 edu-27-code-reuse-rootcause §Langfuse 실트래픽 검증 |
| 루트 채팅턴 트레이스 이름 형식: `haiku · react · quick` 류 (`모델 · coding_type · mode`) | 이번 세션 실사용 검증 |
| 기존 계측 테스트 스타일: orch 인스턴스에 `_reuse_flag`/`_direct_served` 주입 후 `_emit_turn_scores` 검증 | `tests/test_reuse_instrumentation.py:24-30`, `tests/test_direct_serve.py:119,190` |
| 테스트 실행: `python3 -m pytest tests/<파일> -q` (외부 의존 없이 monkeypatch) | 이번 세션 실행 확인 |

미확인: Langfuse 공개 API의 scores 전용 목록 엔드포인트(v2/scores)의 페이지네이션 규격 — 구현 시 트레이스 상세 API 기반으로 우회 가능하므로 설계는 트레이스 API만 전제한다.

## 3. 설계

### 3.1 변경 개요

```
[R1 코호트 측정]
  Issue 1: docs_restored NUMERIC 스코어 발행 (신규 턴부터 UI 필터 가능)
  Issue 2: scripts/lf_cohort.py — Langfuse API → 코호트 집계 리포트 CLI
           · 티어별(direct_serve/near/cold) 비용·지연·출력토큰 평균/중앙값/건수
           · vec_promoted True/False 코호트 분리 (승격이 만든 절감 vs 원래 절감)
           · docs_restored 분포 (스코어 우선, 과거 트레이스는 metadata 폴백)
           · 직접서브 만족도 히스토그램 + **85~89 거절 구간 상세표**
             (쿼리, 후보 제목, delta 여부, 거절 후 실제 지불한 생성 비용)
[R2 임계 하향 검토]
  Issue 3: Issue 2 리포트 기반 판단 실행 (데이터 게이트, §5 기준 적용)
```

### 3.2 인터페이스 계약

```python
# Issue 1 — agent/orchestrator_stream.py::_emit_turn_scores, 기존 direct_serve 블록(764-768) 내부에 추가
if ds is not None:
    ...기존 direct_served / direct_serve_score...
    if ds.get("docs_restored") is not None:
        lf.score_current_trace(name="직접서브 문서복원 (docs_restored)",
                               value=int(ds["docs_restored"]), data_type="NUMERIC")

# Issue 2 — scripts/lf_cohort.py (신규, stdlib만: urllib/json/statistics. httpx·pandas 금지)
# 사용법: LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_BASE_URL 환경변수 필수
#   python3 scripts/lf_cohort.py [--days 7] [--limit 500] [--json out.json] [--include-synthetic]
def fetch_traces(days: int, limit: int) -> list[dict]: ...   # 목록 API 페이지네이션, 이름에 '·' 포함(루트 채팅턴)만
def is_synthetic(user_id: str | None) -> bool: ...           # d3u-/qu-/meas-user/u1/sim-/verify- 프리픽스
def cohortize(traces: list[dict]) -> dict: ...
# 반환 스키마(합성 예시값):
# {"period": {"from": "2026-07-01", "to": "2026-07-08"}, "n_total": 120, "n_synthetic_excluded": 15,
#  "tiers": {"direct_serve": {"n": 4, "cost_avg": 0.012, "cost_med": 0.011,
#            "latency_avg": 18.0, "gen_tokens_avg": 0.0}, "near": {...}, "cold": {...}},
#  "vec_promoted": {"true": {...동일 집계...}, "false": {...}},
#  "docs_restored": {"served_turns": 4, "restored_turns": 3, "avg_docs": 5.5},
#  "satisfaction": {"histogram": {"0-59": 1, "60-84": 2, "85-89": 3, "90-100": 4},
#                   "band_85_89": [{"trace_id": "...", "query": "...", "source_title": "...",
#                                    "delta": false, "cost_paid": 0.2, "latency": 90.1}]}}
def render_markdown(report: dict) -> str: ...                 # 사람이 읽는 리포트(표)
```

### 3.3 데이터 변경

없음. (Langfuse 스코어 1종 추가는 append-only 관측 데이터 — 스키마/마이그레이션 불필요. 과거 트레이스의 docs_restored는 metadata 폴백으로 읽는다.)

## 4. 하지 않는 것 (Non-goals)

- **게이트 로직 변경 금지**: `agent/reuse.py`의 TAU_REUSE/TAU_REUSE_VEC/판정 로직, `agent/direct_serve.py`의 accept 규칙은 이번 범위에서 손대지 않는다 (Issue 3에서 env 기본값 1줄만, 그것도 데이터 기준 충족 시).
- **Langfuse 대시보드/웹 UI 구축 금지**: 리포트는 CLI 출력(markdown/JSON)까지만. 시각화·대시보드는 별도 과제.
- **만족도 판정 프롬프트(`check_satisfaction`) 수정 금지**: 85~89 구간이 애매한 원인이 judge 프롬프트일 수 있으나, 프롬프트를 바꾸면 과거 점수와 비교 불가능해져 측정 자체가 무효화된다.
- **스코어 소급 백필 금지**: 과거 트레이스에 docs_restored 스코어를 소급 기록하지 않는다(metadata 폴백으로 충분).
- **기존 스코어 이름 변경 금지**: `재사용 top1 (reuse_top1)` 등 기존 발행 스코어의 이름·타입은 코호트 연속성을 위해 불변.

## 5. 엣지 케이스와 결정 사항

| 상황 | 결정 |
|---|---|
| 과거 트레이스에 docs_restored 스코어 없음 | metadata `direct_served.docs_restored` 폴백. 둘 다 없으면 해당 턴은 docs 집계에서 제외(N/A) |
| 트레이스 상세 API 호출량 (턴당 1회) | `--limit` 기본 500, 페이지당 50건. 상세 조회는 목록 metadata로 부족한 스코어 필요 턴만 |
| 합성 유저(d3u- 등) 혼입 | 기본 제외 + `--include-synthetic` 플래그로만 포함. 제외 건수를 리포트에 명시(침묵 필터 금지) |
| Langfuse API 인증 실패/네트워크 오류 | 즉시 비정상 종료(exit 1) + 원인 출력. 부분 데이터로 리포트 생성 금지(오독 방지) |
| 만족도 검증이 안 돈 턴(near/cold) | satisfaction 집계에서 제외. tier 집계에는 포함 |
| **임계 하향 판단 기준** (Issue 3에서 적용) | 85~89 거절 구간 **표본 n≥10** 축적 후: 각 건의 (쿼리 vs 후보 제목) 사람 대조로 "그대로 서브해도 만족했을" 비율 **≥80%면 기본값 90→85 하향** PR, **<80%면 유지 결정을 이 문서 §8에 기록**. n<10이면 판단 보류, 재측정만 |
| 하향 시 안전장치 | env `DIRECT_SERVE_MIN_SCORE`는 이미 존재(direct_serve.py:41) — 코드 기본값만 90→85로 바꾸고, 문제 시 env로 즉시 90 복귀 |
| build_success=False 턴의 비용 | tier 집계에 포함하되 리포트에 빌드실패 건수 병기(절감 착시 방지) |

## 6. 구현 이슈 분해

| # | 이슈 제목 | 의존 | 검증 명령어 |
|---|---|---|---|
| 1 | [Task] docs_restored Langfuse NUMERIC 스코어 발행 | 없음 | `python3 -m pytest tests/test_reuse_instrumentation.py tests/test_direct_serve.py -q` |
| 2 | [Task] scripts/lf_cohort.py 코호트 측정 CLI (티어·vec승격·docs복원·만족도 밴드) | #1 (스코어명 참조) | `python3 -m pytest tests/test_lf_cohort.py -q` + 라이브 스모크 `python3 scripts/lf_cohort.py --days 1 --limit 50` |
| 3 | [Task] 만족도 85~89 구간 리포트 검토 → DIRECT_SERVE_MIN_SCORE 하향 결정 실행 | #2 + 실트래픽 n≥10 | `python3 scripts/lf_cohort.py --days 14` 리포트 첨부 + (하향 시) `python3 -m pytest tests/test_direct_serve.py -q` |

각 이슈는 파일 3개·150라인 diff 이내(세션 1개 크기). #3은 데이터 게이트라 실트래픽 축적(1~2주) 후 착수.

## 7. 전체 완료 기준

- [ ] `python3 -m pytest tests/test_reuse_instrumentation.py tests/test_direct_serve.py tests/test_lf_cohort.py -q` 전부 통과
- [ ] 프로덕션 직접서브 턴 트레이스에 `직접서브 문서복원 (docs_restored)` NUMERIC 스코어 발행 확인
- [ ] `python3 scripts/lf_cohort.py --days 7` 이 tier 3종·vec_promoted 2종·만족도 밴드 표를 출력
- [ ] 85~89 구간 n≥10 시점에 §5 기준으로 하향/유지 결정이 §8에 기록됨

## 8. 결정 기록

(Issue 3 실행 시 기입)
