# Langfuse 데이터 체계화 & RAG 재사용 아키텍처 설계안

> 상태: 제안(Draft) · 대상: edu-agent · 작성 기준일: 2026-07-01
> 목적: (1) 프로세스 흐름을 체계적으로 기록하고 (2) 축적 데이터를 RAG 코퍼스로 재사용해 생성 비용을 절감한다.

> **⚠ 최신·검증본은 `langfuse-rag-report.html` (시각 리포트)와 `test_cost_model.py` (검증 테스트)입니다.**
> 아래 문서의 일부 초기 수치(§6-B 등, Haiku 3.5 기준 추정)는 이후 Haiku 4.5·카테고리 기반 모델로
> 재계산·검증되어 리포트/계산기와 다를 수 있습니다. 벡터스토어도 MySQL 원천 + 별도 Redis Stack로 확정.

---

## 0. 핵심 결론 (TL;DR)

1. **Langfuse는 관측 저장소이지 검색(RAG) 저장소가 아니다.** Langfuse를 그대로 RAG 백엔드로 쓰려 하면 안 된다. 대신 *Langfuse = 트레이스의 원천(source of truth)*, *별도 벡터/문서 저장소 = 재사용 코퍼스* 로 **역할을 분리**한다.
2. 현재 관측 기록은 성숙도 7.5/10로 좋다. 그러나 **RAG에 필요한 3가지가 없다**: ① 통제된 메타데이터 분류체계(taxonomy) ② 사용자 피드백(정답 여부) 신호 ③ "요청→결과"를 하나의 재사용 단위로 증류한 레코드.
3. **비용 절감의 전제 조건을 먼저 검증해야 한다.** 현재 모델이 Haiku(매우 저렴)라서, "임베딩 + 벡터검색 + 적응 LLM 호출" 비용이 원래 생성 비용을 넘을 위험이 실재한다. 경제성 검증 없이 RAG 인프라부터 짓는 것이 이 설계의 최대 리스크다.
4. 교육 에이전트 특성상 **틀린 과거 답변을 재사용하면 오류가 전파**된다. 재사용은 반드시 `build_success` 같은 검증된 품질 신호로 게이팅해야 한다.

---

## 1. 현재 상태 진단

### 1.1 이미 잘 기록되는 것 (관측 계층)

| 항목 | 위치 | 상태 |
|---|---|---|
| Trace 트리 구조 (chat_turn → agent_loop → llm_call/tool/subagent) | `orchestrator_stream.py` | ✅ 명확 |
| session_id / user_id 전파 | `orchestrator_stream.py:506` | ✅ |
| tags (coding_type, mode, model) | `:508` | ✅ |
| token / cost / cache 토큰 | `agent/usage.py:20-51` | ✅ 정확 |
| 휴리스틱 품질 점수 (도구 호출수, 에러율, 빌드 성공) | `:542-564` | ✅ |
| PII 마스킹 (이중 방어) | `guardrails.langfuse_mask` + 입력 redact | ✅ |

### 1.2 관측 계층의 GAP (보강 필요)

| # | 누락 | 영향 |
|---|---|---|
| G1 | 재시도(retry) 횟수 미기록 (`:1244-1250`) | 실패-후-성공 패턴 분석 불가 |
| G2 | phase 전환 **사유** 미기록 | "왜 설계→구현으로 갔나" 추적 불가 |
| G3 | tool 결과가 원문 문자열만 저장 (`:368`) | 성공/실패 **유형** 분류 불가 |
| G4 | guardrail `verdict.category`가 metadata에 미기록 (`:649`) | 요청 성격 집계 불가 |
| G5 | **사용자 피드백 점수 없음** | RAG 품질 게이팅의 정답 신호 부재 — **가장 치명적** |

### 1.3 RAG 관점의 근본적 부재

- 요청을 분류할 **통제 어휘(taxonomy)**가 없다 → "어떤 종류의 업무 지시인지" 필터 불가.
- 트레이스는 "무슨 일이 일어났나"를 기록할 뿐, "이 답은 재사용해도 되는가"를 판단할 **재사용 단위(Interaction Record)**가 없다.
- 임베딩 / 벡터 저장소 / 검색 파이프라인 미도입 (`pyproject.toml`에 관련 의존성 없음).

---

## 2. 목표 아키텍처 (5계층)

```
[사용자 턴]
   │
   ▼
┌─────────────────────────────────────────────┐
│ L1  관측 계층 (Langfuse)  ← 원천/디버깅용     │  ← 기존 + GAP 보강
│     trace/span/generation + 표준 메타데이터    │
└─────────────────────────────────────────────┘
   │  (비동기 ETL, Langfuse API로 export)
   ▼
┌─────────────────────────────────────────────┐
│ L2  분류 계층 (Taxonomy)  ← 통제 어휘/스키마   │
│     intent · domain · actor · target · outcome │
└─────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────┐
│ L3  재사용 단위 (Interaction Record)          │
│     요청→결과 증류 + 품질 신호 + 임베딩         │
└─────────────────────────────────────────────┘
   │  (품질 게이트 통과분만)
   ▼
┌─────────────────────────────────────────────┐
│ L4  RAG 코퍼스 (MySQL 원천 + Redis Stack 인덱스)│
└─────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────┐
│ L5  재사용 결정 (Retrieval + Reuse Policy)    │
│     유사도·intent·품질 임계 → 재사용 vs 생성   │
│     재사용율을 다시 Langfuse score로 기록(폐루프)│
└─────────────────────────────────────────────┘
```

**핵심 설계 원칙: 관측(L1)과 검색(L4)의 저장소를 물리적으로 분리한다.** Langfuse는 변경 불가한 이벤트 로그, RAG 코퍼스는 큐레이션된 지식 베이스다.

---

## 3. L2 — 메타데이터 분류체계 (Taxonomy)

RAG 필터링과 "어떤 종류의 지시/누가/대상/결과" 요구를 충족하는 **통제 어휘**. 자유 텍스트 금지, enum으로 강제.

```python
# agent/taxonomy.py (신규 제안)
class Intent(str, Enum):          # 업무 지시의 종류
    CREATE_COMPONENT = "create_component"
    FIX_BUG          = "fix_bug"
    REFACTOR         = "refactor"
    EXPLAIN_CONCEPT  = "explain_concept"
    DESIGN_REVIEW    = "design_review"
    MODIFY_REQUEST   = "modify_request"
    OTHER            = "other"

class Domain(str, Enum):          # 대상 주제
    REACT_STATE = "react_state"
    REACT_UI    = "react_ui"
    BLOCKLY     = "blockly"
    ALGORITHM   = "algorithm"
    # ...교육 커리큘럼 축과 정렬

class ActorRole(str, Enum):       # 누가
    STUDENT = "student"
    TEACHER = "teacher"
    ANON    = "anon"

class Outcome(str, Enum):         # 결과 카테고리
    SUCCESS      = "success"       # 빌드/검증 통과
    PARTIAL      = "partial"
    FAILED       = "failed"
    BLOCKED      = "blocked"       # guardrail 차단

class RequestMeta(BaseModel):     # 쓰기 시점에 검증
    schema_version: int = 1
    intent: Intent
    domain: Domain
    actor_role: ActorRole
    target: str                   # 산출물 종류 (component/diagram/note...)
    outcome: Outcome | None = None
    difficulty: Literal["easy","medium","hard"] | None = None
```

**적용 방식**
- `RequestMeta`를 Pydantic으로 검증 후 `metadata=`에 실어 Langfuse에 기록 → 관측/RAG 스키마 일원화.
- `schema_version`으로 진화 대비. 스키마 변경 시 재임베딩 트리거.
- **분류는 어떻게?** 신규 분류 LLM 호출을 추가하지 말고, **이미 존재하는 guardrail 분류 결과(G4)** 를 재활용하거나 턴 종료 후 **비동기**로 경량 분류. (턴 지연/비용 증가 방지)

---

## 4. L3 — Interaction Record (재사용 단위)

RAG의 최소 단위는 원시 트레이스가 아니라, **증류된 "요청→결과" 레코드**다.

```python
class InteractionRecord(BaseModel):
    id: str
    trace_id: str; session_id: str; user_id: str
    schema_version: int

    request:  dict   # {normalized_text, intent, domain, target, params}
    response: dict   # {summary, artifacts:[{type, ref}], code_refs}
    context:  dict   # {phase, coding_type, mode, prior_turn_count}
    metrics:  dict   # {turns, cost_usd, latency_ms, tokens, cache_hit_rate}
    quality:  dict   # {build_success, tool_error_rate, human_feedback, reusability_score}

    request_embedding: list[float]   # 요청+intent 임베딩
    created_at: str
```

- `reusability_score`: 품질 신호 조합으로 계산되는 파생값. 이 점수가 임계 이상인 레코드만 L4 코퍼스에 진입.
- `request_embedding`: **요청 텍스트 + 정규화된 intent**를 임베딩(정규화가 핵심 — 표현이 달라도 같은 의도를 묶기 위함).

---

## 5. L4/L5 — 코퍼스 & 재사용 정책

### 5.1 저장소
- **MySQL 샵**이므로 pgvector(=Postgres 신규 엔진)는 제외. 검증은 **MySQL(임베딩 JSON)+앱 코사인**(신규 인프라 0), 운영은 **별도 Redis Stack 인스턴스**(RediSearch 벡터+메타 필터, 세션락 Redis와 분리). 원천=MySQL, Redis=재구성 가능한 인덱스.

### 5.2 ETL 파이프라인
1. Langfuse API로 트레이스 export (스케줄 or 이벤트 기반)
2. → InteractionRecord 변환 + PII 재검증
3. → 품질 게이트: `outcome==SUCCESS AND build_success AND reusability_score>=τ AND PII-clean`
4. → 임베딩 계산 → 코퍼스 upsert

### 5.3 재사용 결정 정책 (L5)
```
신규 요청 → intent 분류 → 임베딩 → top-k 검색
if  similarity >= τ_sim
and same(intent, domain)
and candidate.quality >= τ_q:
      → 재사용 (필요 시 경량 LLM 어댑테이션)
else: → 정상 생성 (그리고 로깅)
```
- **콜드 스타트**: 코퍼스 < N건이면 항상 생성. (사용자가 언급한 "1만 건" 도달 전까지)
- **폐루프**: 재사용 여부/재사용율을 다시 Langfuse `score`로 기록 → 실제 절감 효과를 관측 계층에서 측정.

---

## 6. 문제점 / 리스크 분석 (요청하신 "문제 없는지")

| # | 리스크 | 심각도 | 완화책 |
|---|---|---|---|
| R1 | **경제성 역전** — Haiku가 이미 저렴. 임베딩+검색+어댑테이션 비용이 생성 비용을 초과할 수 있음 | 🔴 높음 | PoC로 단위비용 실측. 임베딩은 저가/로컬 모델, 재사용 시 어댑테이션 생략 or 초경량화 |
| R2 | **품질 오염** — 낮은 품질 답이 코퍼스에 유입되면 검색 품질 저하 | 🔴 높음 | 품질 게이트 필수. `build_success` 등 검증된 신호만 통과 |
| R3 | **교육적 정확성** — 미묘하게 틀린 과거 답 재사용 시 오류 전파 | 🔴 높음 | 검증된 outcome만 재사용. 교사 검수 신호 도입 |
| R4 | **정답 신호 부재(G5)** — 사용자 피드백이 없어 "재사용 가능"을 판단할 근거 부족 | 🟠 중 | 피드백 UI/스코어 우선 도입 (👍/👎, 재요청 여부) |
| R5 | **PII/프라이버시** — 한 학생의 코드/데이터를 타 학생에게 재사용 | 🟠 중 | 코퍼스 자체 PII 재검증 + 학생 데이터 재사용 정책 정의 |
| R6 | **모델 드리프트** — 구 모델 답변을 신 모델 컨텍스트에서 재사용 | 🟡 낮 | 레코드에 model 버전 저장, 임계 밖은 폐기/재생성 |
| R7 | **스키마 진화** — taxonomy 변경 시 재임베딩 필요 | 🟡 낮 | `schema_version` 관리 + 재임베딩 배치 |
| R8 | **분류 비용/지연** — 턴마다 분류 LLM 호출 추가 | 🟡 낮 | guardrail 결과 재활용 or 비동기 분류 |

---

## 6-B. 비용 시뮬레이션 & 코퍼스 규모 (핵심 질문: 1,000 vs 10,000)

> 모든 수치는 Haiku 3.5 실단가 기반 **추정**이며, Phase 0 실측으로 대체한다.

**단가 가정**
- 풀 생성 원가 ≈ **$0.020/턴** (main + 서브에이전트 3 + 가드레일, 캐시 반영)
- 재사용 원가 ≈ **$0.005/턴** (경량 어댑테이션)
- 조회(분류+임베딩+검색) ≈ **$0.0005/턴** (거의 무시)
- 블렌디드 단가 = `hit×0.0055 + (1−hit)×0.0205`

**시나리오: 학생 500명 × 월 40턴 = 월 20,000턴**

| 코퍼스 | 커버리지 | hit rate | 블렌디드 | 월 비용 | 절감 |
|---|---|---|---|---|---|
| 0 (콜드) | 없음 | 0% | $0.0205 | $410 | −2% * |
| ~500 | 상위 5개 셀 부분 포화 | 25% | $0.0168 | $335 | 16% |
| **~1,500** | **상위 15개 셀 포화** | **50%** | **$0.0130** | **$260** | **35%** |
| ~4,000 | 상위 완전+중위 진입 | 65% | $0.0107 | $215 | 46% |
| ~10,000 | 롱테일 커버 | 72% | $0.0097 | $194 | 52% |

\* 콜드 상태에선 조회 오버헤드만 붙어 손해 → 코퍼스 < N건이면 조회 스킵.

### 정답은 "총량"이 아니라 "카테고리 포화"
- hit rate는 코퍼스 총 개수가 아니라 **(intent × domain) 셀별 포화도**의 함수.
- 교육 코딩 요청은 Zipf 분포 → **상위 ~15개 셀이 트래픽의 75%**.
- 단일 셀 포화 곡선 `hit ≈ 1 − e^(−n/60)`: n=60→0.63, **n=100→0.81**, n=300→0.99.
- → 15셀 × ~100건 = **약 1,500건이 절감의 대부분(35%)**을 만든다. 나머지 8,500건은 체감 수익 롱테일.
- **1,500 → 10,000 (6.7배 데이터)로 hit는 50%→72%(+17%p)뿐** — 강한 체감 수익.

**따라서 목표는 개수가 아니라 "커버리지 맵 채우기":** 셀별 hit rate를 추적해 **고트래픽·저커버리지 셀**에 큐레이션 집중, 포화 셀은 신규 적재 중단(dedup).

### 절감 메커니즘 — 핵심 지렛대는 출력 토큰
Haiku 출력 단가($4/M)는 입력($0.8/M)의 5배. 검색은 이 비싼 출력 생성을 순수 재사용 시 통째로(95% 절감), 어댑테이션 시 대부분(75% 절감) 제거한다.

### 규모 민감도 (hit 50% 가정)
| 규모 | 월 턴 | baseline/월 | 절감/월 | 절감/년 | 판단 |
|---|---|---|---|---|---|
| 학생 500 | 20,000 | $400 | $140 | $1,680 | 엔지니어링비 대비 애매 — 관측/품질 이득으로 정당화 |
| 학생 2,000 | 80,000 | $1,600 | $560 | $6,720 | 명확히 이득 |
| 학생 5,000 | 200,000 | $4,000 | $1,400 | $16,800 | 강한 이득 |

→ RAG는 **트래픽 규모에서 정당화**된다. 작은 규모에선 Phase 1~2(관측/품질)만으로도 충분히 가치.

---

## 6-C. 전략 전환: 반응형(Reactive) → 사전 제작(Proactive)

교육은 **커리큘럼을 미리 안다** → 트래픽을 기다리지 말고 사내 AI로 핵심 각본을 **대량 사전 제작**.

| 축 | 반응형 RAG | 사전 제작형 | 하이브리드(권장) |
|---|---|---|---|
| 코퍼스 | 라이브 수확 | 배치 사전 생성 | **머리는 저술·꼬리는 수확** |
| 콜드 스타트 | 있음 | 없음 (1일차 커버) | 없음 |
| happy-path | 검색+어댑 | **각본 서빙 ≈$0** | 각본 서빙 |
| 예상 절감 | 35~52% | **62~74%** | **65~75%** |
| 최대 약점 | 램프업 지연 | 예측 빗나감·경직성 | 운영 복잡도 |
| 품질 통제 | 사후 | 사전(사람 검수) | 사전+사후 |

- **진짜 비용은 토큰이 아니라 사람.** 시드 2,500건 토큰 제작비 ≈ $10 (배치+캐시). 투자는 커리큘럼 저술/검수로 이동 — 교육팀은 원래 그 일을 함.
- 사전 제작이 더 싼 3가지 지렛대: ① Batch API 50% 할인 ② 대량 생성 시 프롬프트 캐시 극대화 ③ 1건이 수천 명에게 서빙(amortization).
- 위험: 예측 리스크·경직성(→라이브 폴백 필수)·대규모 오류(→사람 검수 게이트 필수)·staleness.

## 6-D. 모델 티어링 (가장 강력한 지렛대): 비쌀 땐 한 번, 쌀 땐 매번

**저술(offline·1회)과 서빙(online·매턴)은 난이도가 다르다.** 저술은 최상위 모델(Opus)로, 서빙은 Haiku가 원본을 **편집만** 한다.

> 약한 모델에게 "생성"은 어렵지만 "편집"은 쉽다. 훌륭한 원본을 주면 결과가 원본 품질에 수렴.

| 방식 | 저술 | 서빙 | 서빙/호출 | 품질 | 비고 |
|---|---|---|---|---|---|
| 현재 | — | Haiku 생성 | $0.0076 | 중 | 매번 맨바닥 |
| 라이브 Opus | — | Opus 생성 | $0.1425 | 최상 | 현재 19배 — 규모 불가 |
| **사전제작 티어링** | **Opus(1회)** | **Haiku 어댑트** | **$0.0030** | **상~최상** | **현재 1/3 · Opus급** |

- **핵심: 품질 ≠ 서빙 단가.** 품질은 저술 시 비싼 모델이 한 번, 서빙 단가는 싼 모델이 매번 결정 → 두 축 분리 = Opus급 품질을 Haiku급 단가로.
- 비싼 모델은 **저술에만 딱 한 번.** Opus 시드 2,500건 저술 ≈ $159 (1회성).

**소스 우선순위 (저술비 순):**
1. **기존 우수 콘텐츠 재활용** — 사내 교재·MODI 예제·검증 프로젝트·과거 👍 답변 → 저술비 ≈ **$0**, 품질도 검증됨. **최우선.**
2. **Opus 배치 저술** — 기존 콘텐츠 없는 핫셀만 고품질 생성 → 사람 검수 → 적재.
3. **반응형 수확** — 예측 못한 롱테일·실제 표현 변형.
4. → 소스 무엇이든 **서빙은 동일**: Haiku가 학생 문맥에 맞춰 경량 편집 ($0.003/턴).

종합 절감 **~76%** (main call 기준 보수적). 규모별: 학생 2,000 ≈ $5,568/년, 5,000 ≈ $13,920/년.

> 단가는 전부 대표값 추정 — Phase 0에서 실측으로 교체.

## 7. 단계적 실행 로드맵 (초기비용→절감 순서)

**Phase 0 — 경제성 검증 (선행 필수, 1~2일)**
- 최근 트레이스 표본으로 "생성 단가 vs (임베딩+검색+어댑테이션) 단가" 실측. **여기서 이득이 안 나오면 이후 단계 보류.**

**Phase 1 — 관측 GAP 보강 + 분류체계 (저비용, 즉효)**
- G1~G4 메타 보강, `agent/taxonomy.py` 도입, guardrail category를 metadata로 승격.
- 산출: RAG 없이도 Langfuse 대시보드 분석력 즉시 향상.

**Phase 2 — 피드백 신호 도입 (G5)**
- 사용자 👍/👎, 재요청/포기 여부를 `score`로 기록. RAG 품질 게이트의 정답 데이터 확보.

**Phase 3 — Interaction Record ETL**
- Langfuse export → InteractionRecord 변환 배치. (아직 검색 없이 적재만)

**Phase 4 — 코퍼스 + 검색 (MySQL+앱 코사인 → 운영 시 Redis Stack)**
- 품질 게이트 통과분 임베딩/적재. 검색 API 구축. **읽기 전용 shadow 모드**로 "재사용했다면?" 시뮬레이션.

**Phase 5 — 재사용 정책 활성화 + 폐루프 측정**
- 임계 튜닝 후 실제 재사용 on. 재사용율/절감액을 Langfuse score로 모니터링.

---

## 8. 즉시 착수 가능한 저위험 항목 (Phase 1 상세)

1. `orchestrator_stream.py:649` — guardrail `verdict.category`를 metadata에 기록 (G4).
2. `_llm_call` 재시도 카운터를 metadata에 기록 (G1).
3. `_run_tool` 결과에 `outcome_type`(success/validation_error/llm_error) 태그 추가 (G3).
4. phase change span에 `reason` 필드 추가 (G2).
5. `agent/taxonomy.py` + `RequestMeta` Pydantic 모델 신설, 기존 metadata dict를 이 모델로 검증.

> 이 5개는 RAG 여부와 무관하게 관측 품질을 올리므로, 경제성 검증(Phase 0)과 병행 착수 가능.
