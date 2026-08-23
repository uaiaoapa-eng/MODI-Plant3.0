# MODI Planet 3.0 — Product & System Architecture

> 목적: 기존 edu-agent의 생성/RAG/MODI 코어를 보존하면서, 사용자 경험을 **교육과정(LMS)** 과 **만들기(Studio)** 두 제품 모드로 재구성한다.
> 최우선 원칙: 기존 생성 엔진을 갈아엎지 않는다. `StreamOrchestrator`, React/Blockly/Hybrid 생성, 빌드 검증, RAG/온톨로지, 세션/쿼터/관측성은 공통 코어로 재사용한다.

---

## 구현 상태 — P1

- 완료: Home의 Learn/Create 분리, Learn 학교급·9차시 선택 셸, Create의 Web/MODI/Web+MODI 선택, 기존 guided design 파이프라인 어댑터, `/api/v3` 계약, 로컬 웹 작업공간.
- 호환: 기존 `/chat`과 내부 `quick` 경로는 유지하지만, MODI Planet 3.0 사용자 UI에는 `quick`을 노출하지 않는다.
- 미구현(P2): 실제 Lesson Engine, `LessonSession`, 초등 1차시 콘텐츠와 45분 상태 흐름. P1의 차시 제목은 화면 구조 확인용 예시/placeholder이며 확정 교안이 아니다.

---

## 1. 제품 구조

첫 진입은 빈 채팅이 아니라 두 개의 명확한 진입점이다.

### A. 교육과정 / Learn
- 목표: 선생님이 PPT 없이 MODI Planet 자체를 화면에 띄워 40~50분 수업 진행
- 대상: 초등 / 중등 / 고등
- 각 학교급 9차시, 총 27차시
- 수업 유형: Web / H/W(MODI) / Web+H/W
- 최종 목표: 학생이 MODI와 연동되는 Web 프로젝트를 스스로 완성
- 핵심 UX: 정해진 차시 안에서 LLM이 교사처럼 설명 → 질문 → 실습 → 제작 → 점검 → 회고를 진행

### B. 만들기 / Create
- 목표: 학생이 자유롭게 아이디어를 대화로 구체화하고 결과물 제작
- 기존의 `같이 만들기`만 유지
- `한번에 만들기(quick)`는 사용자 UI에서 제거
- Web / H/W / Web+H/W 모두 지원
- 기존 `design → implement → verify` 에이전트 흐름을 최대한 재사용

---

## 2. 절대 분리해야 하는 두 개의 상태 머신

### 2.1 LearnStateMachine
교육 모드는 자유 채팅이 아니다. 차시가 가진 **수업 시나리오가 주도권**을 가져야 한다.

```text
LESSON_INTRO
  → CONCEPT
  → GUIDED_DISCOVERY
  → MINI_PRACTICE
  → BUILD_STEP_1
  → BUILD_STEP_2
  → BUILD_STEP_N
  → CHECKPOINT
  → CHALLENGE
  → REFLECTION
  → COMPLETE
```

LLM은 현재 단계에서 허용된 역할만 수행한다.
예: CONCEPT 단계에서 학생이 "그냥 완성해줘"라고 해도 전체 프로젝트를 즉시 생성하지 않는다.

### 2.2 CreateStateMachine
현재 코어를 유지한다.

```text
IDEA
  → DESIGN
  → IMPLEMENT
  → VERIFY
  ↔ MODIFY
```

기존 `Phase.DESIGN / IMPLEMENT / VERIFY`를 사용한다.

---

## 3. 공통 생성 코어

기존 자산 중 유지 대상:

- `agent/orchestrator_stream.py`
- `agent/tools.py`
- `agent/builder.py`
- `agent/modi_modules.py`
- React 생성
- Blockly 생성
- Hybrid(Web+MODI) 생성
- RAG / Ontology / direct_serve / near / cold
- 프로젝트 저장/복원
- Langfuse/Sentry/usage/quota

새 제품 계층은 이 코어 **앞에** 붙인다.

```text
Client
  ↓
Product Router
  ├─ Learn Controller ── Lesson Engine ── Tutor Policy
  │                                  ↓
  │                           Shared Generation Core
  │
  └─ Create Controller ── Create Orchestrator Adapter
                                     ↓
                              Shared Generation Core
```

`StreamOrchestrator` 안에 LMS 조건문을 수십 개 넣는 방식은 금지한다.

---

## 4. 권장 서버 디렉터리

```text
agent/
  core/
    generation/         # 현 생성/수정/검증 로직
    rag/                # 기존 prime/reuse adapter
    safety/
    modi/

  create/
    orchestrator.py     # 기존 StreamOrchestrator를 adapter 형태로 사용
    prompts.py
    policy.py

  learn/
    orchestrator.py     # 차시 상태 머신
    lesson_engine.py
    tutor_policy.py
    assessment.py
    pacing.py
    prompts.py
    tools.py

curriculum/
  schema.py
  loader.py
  validator.py
  elementary/
    lesson-01.yaml
    ... lesson-09.yaml
  middle/
    lesson-01.yaml
    ... lesson-09.yaml
  high/
    lesson-01.yaml
    ... lesson-09.yaml

api/
  routes/
    home.py
    learn.py
    create.py
    projects.py
    teacher.py
  schemas/

services/
  project_service.py
  progress_service.py
  curriculum_service.py
  artifact_service.py
  analytics_service.py
```

1차 프로토타입에서는 물리적 이동을 최소화한다. 먼저 새 모듈을 추가하고, 안정화 뒤 기존 파일을 `core/`로 이동한다.

---

## 5. 프론트엔드 IA

### 5.1 Home
기존 브랜드/큰 레이아웃을 최대한 유지하되 첫 화면의 질문 입력창을 바로 노출하지 않는다.

```text
MODI Planet 3.0

오늘 무엇을 해볼까요?

[ 교육과정으로 배우기 ]
학교 수업에 맞춰 차근차근 배우고 프로젝트를 완성해요.

[ 자유롭게 만들기 ]
AI와 이야기하며 나만의 Web/MODI 프로젝트를 만들어요.

최근 프로젝트
```

### 5.2 Learn 선택 흐름

```text
교육과정
 → 학교급 선택 (초등 / 중등 / 고등)
 → 9개 차시 맵
 → 차시 상세 (목표, 예상시간, 결과물)
 → 수업 시작
```

### 5.3 Learn Lesson Workspace

데스크톱 기준 3영역을 권장한다.

```text
┌─────────────────────────────────────────────────────────┐
│ 차시명 / 3단계 중 1 / 진행률 / 남은 예상 구간           │
├──────────────┬──────────────────────┬───────────────────┤
│ Lesson Path  │ AI Tutor / Activity  │ Preview / MODI    │
│              │                      │ Code / Blockly     │
│ 개념         │ 설명                 │                   │
│ 실습         │ 질문                 │                   │
│ 만들기       │ 선택지/입력           │                   │
│ 도전         │                      │                   │
├──────────────┴──────────────────────┴───────────────────┤
│ [도움 요청] [힌트] [내 결과 확인]                        │
└─────────────────────────────────────────────────────────┘
```

중요: 채팅 UI가 화면의 주인공이면 안 된다. **현재 학습 활동(Activity)** 가 주인공이고 채팅은 상호작용 수단이다.

### 5.4 Create Workspace
기존 UI를 최대한 유지한다.

첫 화면:
```text
무엇을 만들고 싶나요?

[ Web ] [ MODI H/W ] [ Web + MODI ]

아이디어를 말해 주세요...
```

이후 기존 설계 대화 → 구현 → 미리보기 흐름 사용.

---

## 6. Curriculum 데이터 모델 — 27차시를 코드에 박지 않는다

차시는 YAML/JSON 콘텐츠로 관리한다. 교과과정이 바뀌어도 코드 수정 없이 콘텐츠 교체가 가능해야 한다.

```yaml
id: elementary-01
grade_band: elementary
lesson_no: 1
title: "컴퓨터와 명령"
duration_min: 45
project_type: web
curriculum:
  subject: "실과"
  achievement_standards: []

learning_objectives:
  - "순서대로 명령하는 개념을 설명할 수 있다"
  - "간단한 인터랙션 웹을 수정할 수 있다"

final_artifact:
  title: "나를 소개하는 인터랙티브 카드"
  coding_type: react

required_concepts:
  - sequence
  - event

steps:
  - id: intro
    type: lesson_intro
    target_minutes: 3
  - id: concept-1
    type: concept
    target_minutes: 7
    content_ref: sequence-basic
  - id: guided-1
    type: guided_discovery
    target_minutes: 5
  - id: build-1
    type: build
    target_minutes: 15
    allowed_tools:
      - edit_code
  - id: challenge
    type: challenge
    target_minutes: 8
  - id: reflection
    type: reflection
    target_minutes: 5

assessment:
  checkpoints:
    - concept: sequence
      method: conversational
  rubric:
    - criterion: "동작 완성"
      levels: ["도움 필요", "부분 완성", "완성", "응용"]
```

---

## 7. LMS에서 LLM의 역할

LLM이 차시 전체를 즉흥적으로 지어내면 안 된다.

### Content = Deterministic
- 학습 목표
- 교과 성취기준
- 차시 순서
- 핵심 개념
- 실습 목표
- 평가 기준
- 준비물
- 정답/교사용 가이드

### LLM = Adaptive
- 학생 수준에 맞는 설명 난이도 조절
- 학생 답변 이해
- 추가 질문
- 힌트
- 코드/Blockly 생성 및 부분 수정
- 오개념 탐지
- 회고 피드백

즉, **교육의 골격은 콘텐츠가 통제하고, 대화의 표현과 적응만 LLM이 담당**한다.

---

## 8. Pacing Engine — 40~50분 수업을 실제로 맞추는 핵심

수업시간은 프롬프트 한 줄로 맞출 수 없다.

LessonSession에 다음 상태가 필요하다.

```text
started_at
elapsed_minutes
current_step_id
step_started_at
completed_steps
skipped_steps
help_count
hint_count
attempt_count
mastery_by_concept
```

규칙 예:
- 45분 수업의 70%가 지났는데 BUILD 진입 전이면 설명을 압축
- 학생이 빠르면 Extension Challenge 제공
- 학생이 막히면 같은 문제를 반복 질문하지 말고 힌트 단계 상승
- 종료 5분 전에는 무조건 결과 저장 + 회고 단계 진입 가능

---

## 9. Assessment 모델

점수부터 만들지 않는다. 우선 `mastery`를 concept 단위로 저장한다.

```text
NOT_SEEN
INTRODUCED
GUIDED
APPLIED
MASTERED
```

증거(evidence):
- 학생 대답
- 실행 성공
- 코드 변경
- Blockly 동작
- challenge 성공

교사용으로는 "몇 점"보다 아래가 먼저다.

```text
32명 중
- 이벤트 개념 MASTERED: 24명
- 조건문 GUIDED 이하: 8명
- 프로젝트 완성: 29명
- 도움이 많이 필요한 학생: 4명
```

---

## 10. 프로젝트 타입

기존 `coding_type` 계약을 유지한다.

```text
react   = Web
blockly = MODI H/W
hybrid  = Web + MODI
```

새로운 enum을 또 만들어 이중 매핑하지 않는다.

교육 차시에서 `coding_type`은 lesson metadata가 결정한다.
Create에서는 학생이 선택한다.

---

## 11. API 계약

기존 `/chat`은 Create 호환을 위해 유지한다.

추가 권장 API:

```text
GET  /api/v3/home
GET  /api/v3/curriculum
GET  /api/v3/curriculum/{grade_band}
GET  /api/v3/lessons/{lesson_id}
POST /api/v3/learn/sessions
GET  /api/v3/learn/sessions/{id}
POST /api/v3/learn/sessions/{id}/interact      # SSE
POST /api/v3/learn/sessions/{id}/hint
POST /api/v3/learn/sessions/{id}/checkpoint
POST /api/v3/learn/sessions/{id}/complete

POST /api/v3/create/sessions
POST /api/v3/create/sessions/{id}/chat         # 내부적으로 기존 /chat 코어 활용

GET  /api/v3/progress/me
GET  /api/v3/teacher/classes/{class_id}/progress
```

Learn과 Create 요청 payload를 억지로 한 `/chat` 스키마로 합치지 않는다.

---

## 12. 상태 저장 모델

기존 Project Session과 LMS Lesson Session은 별도 엔티티다.

### ProjectSession
- session_id
- user_id
- coding_type
- phase
- conversation
- generated_code
- blockly_xml
- design_doc
- learning_notes

### LessonSession
- lesson_session_id
- user_id
- lesson_id
- project_session_id (nullable/linked)
- current_step_id
- progress_percent
- mastery
- attempts
- hints
- started_at
- completed_at

교육 중 실제 제작 단계에 들어가면 ProjectSession을 생성/연결한다.

---

## 13. Teacher Mode는 학생 UI와 분리

1차 프로토타입 필수:
- 차시 선택
- 수업용 화면 바로 실행
- 학생 진도 상태 확인

2차:
- 반 생성
- 학생 초대/코드
- 실시간 진행률
- 막힌 학생 표시
- 결과물 모아보기
- 차시 리포트

Teacher 기능을 학생용 LLM prompt에 섞지 않는다.

---

## 14. RAG / Ontology의 역할 변경

현재 RAG는 생성물 재사용에 강하다. LMS에서는 두 레이어로 사용한다.

### Curriculum RAG
검수된 교안/개념/성취기준/교사용 자료만 검색.

### Creation RAG
기존 코드/학습노트/프로젝트 재사용.

두 코퍼스의 source/type을 반드시 구분한다.
교육 중에는 인터넷 검색 결과나 과거 학생 생성물이 교과 사실처럼 우선되지 않게 한다.

---

## 15. Prompt Architecture

거대한 system prompt 1개 금지.

Learn prompt는 조립식으로 만든다.

```text
SAFETY
+ TUTOR_IDENTITY
+ GRADE_BAND_POLICY
+ CURRENT_LESSON_OBJECTIVES
+ CURRENT_STEP_POLICY
+ ALLOWED_ACTIONS
+ STUDENT_MASTERY_CONTEXT
+ CURRENT_PROJECT_CONTEXT(optional)
```

Create:
```text
SAFETY
+ CREATE_DESIGN_POLICY / IMPLEMENT_POLICY
+ coding_type policy
+ project context
+ RAG prime
```

---

## 16. UI에서 제거/변경할 것

- 사용자용 `quick / 한번에 만들기` 제거
- 빈 화면의 “무엇을 만들까요?” 단일 진입 제거
- 첫 진입에서 Learn/Create 선택
- LMS에서는 채팅 로그 자체가 메인 콘텐츠가 되지 않게 변경
- 학습노트는 생성 후 덤프가 아니라 현재 lesson step과 연결
- 코드 설명은 모든 줄을 설명하는 것이 아니라 현재 학습목표와 관련된 부분만 강조

---

## 17. 프로토타입 순서

### P0 — Architecture lock
- ProductMode = learn/create
- lesson schema
- lesson state machine
- API contract
- 기존 생성 코어 경계 고정

### P1 — Home + Create migration
- 첫 페이지 Learn/Create 선택
- Create 진입
- quick UI 제거
- 기존 design pipeline 연결

### P2 — LMS vertical slice 1개
전체 27차시를 만들지 말고 **초등 1차시 하나를 완벽히** 만든다.
- lesson YAML
- 45분 state flow
- concept → guided → build → challenge → reflection
- React 프로젝트 생성/수정 연동

### P3 — Hybrid vertical slice
Web+MODI 수업 1개를 만들어 end-to-end 검증한다.

### P4 — 27차시 확장
검증된 schema에 콘텐츠만 추가한다.

### P5 — Teacher dashboard
반/학생/진도/결과물.

---

## 18. 금지 사항

1. `StreamOrchestrator`에 `if learning_mode ...` 조건을 계속 추가하지 말 것.
2. 27개 차시 내용을 Python prompt 문자열에 하드코딩하지 말 것.
3. LLM에게 “45분짜리 수업 알아서 진행해”라고 맡기지 말 것.
4. LMS와 자유 제작의 conversation/state를 하나로 섞지 말 것.
5. 기존 빌드검증/RAG/세션/쿼터를 새로 재작성하지 말 것.
6. prototype 전에 DB/마이크로서비스를 과도하게 분리하지 말 것.
7. 첫 버전에서 27차시를 동시에 구현하지 말 것. vertical slice 우선.

---

## 19. 성공 기준

첫 프로토타입 성공은 예쁜 화면이 아니다.

- 초등 1개 차시가 40~50분 수업 구조로 끝까지 진행된다.
- 학생이 중간에 엉뚱한 요청을 해도 차시 목표를 잃지 않는다.
- 학생 수준에 따라 힌트/설명이 달라진다.
- 실제 Web 또는 MODI 산출물이 만들어진다.
- 현재 어떤 개념을 배웠는지 시스템이 증거 기반으로 기록한다.
- 교사는 PPT 없이 화면만으로 수업 흐름을 따라갈 수 있다.
- Create 모드에서는 기존 제작 경험이 훼손되지 않는다.
