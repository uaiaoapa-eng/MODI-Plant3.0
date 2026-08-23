# MODI Planet 3.0 — Claude Code Working Contract

이 저장소는 기존 edu-agent를 MODI Planet 3.0으로 확장한다.
코드를 수정하기 전에 반드시 `docs/design/modi-planet-3-architecture.md`를 읽는다.

## Product invariant

제품은 두 모드다.

1. `learn` — 학교 교육과정 기반 LMS
2. `create` — 학생 자유 제작

사용자용 `quick/한번에 만들기`는 MODI Planet 3.0에서 제거한다.
기존 `design` 기반 같이 만들기 경험은 Create의 기반으로 유지한다.

## Architecture invariant

- 기존 `StreamOrchestrator`, generation tools, build validation, RAG/Ontology, MODI, session, quota, observability를 가능한 그대로 재사용한다.
- LMS 로직을 `orchestrator_stream.py` 내부 조건문으로 누적하지 않는다.
- LMS는 `agent/learn/`의 별도 Lesson Engine / state machine으로 구현한다.
- 자유 제작은 `agent/create/` adapter를 통해 기존 생성 코어를 사용한다.
- lesson content는 `curriculum/*.yaml` 데이터로 관리한다. prompt/Python 코드에 차시 내용을 하드코딩하지 않는다.

## Change discipline

작업마다:
1. 현재 관련 코드를 먼저 읽는다.
2. 기존 동작을 깨지 않는 최소 변경안을 쓴다.
3. 구현한다.
4. 기존 테스트 + 새 테스트를 실행한다.
5. 실패한 테스트가 있으면 원인 해결 전 완료라 하지 않는다.
6. 구조적 변경 시 architecture 문서를 같이 갱신한다.

## Forbidden

- 대규모 rewrite 금지.
- 사용자 요청 없이 framework 교체 금지.
- 기존 RAG/세션/검증 기능 복제 금지.
- 27차시 동시 구현 금지.
- LMS를 단순 system prompt 변경으로 구현 금지.
- giant component / giant orchestrator 금지.
- UI를 기존 MODI Planet의 큰 틀과 무관하게 새 디자인으로 갈아엎지 않는다.

## First implementation milestone

P1만 먼저 구현한다.

- 첫 화면에서 `교육과정으로 배우기` / `자유롭게 만들기` 선택
- Create에서 Web / MODI / Web+MODI 선택
- Create는 기존 design pipeline 연결
- 사용자용 quick mode 제거
- Learn은 curriculum 선택 shell까지만 구현
- elementary lesson 01의 실제 Lesson Engine은 다음 milestone

PR/commit은 milestone 단위로 작게 유지한다.
