# TODO / 다음에 할 것

## 우선순위 높음
- [ ] 설계 문서 자동 생성 (design_doc 구조화 데이터)
  - update_design_doc tool 추가
  - Phase 전환 조건을 코드로 체크
- [ ] 태스크 플래너
  - 설계 문서 기반 태스크 리스트 자동 생성
  - 태스크 순서대로 실행, 진행 상태 코드가 추적
- [ ] 질문 카운팅을 코드로 강제 (프롬프트 의존 X)

## 우선순위 중간
- [ ] 멀티 에이전트 (설계/코드/리뷰 분리)
- [ ] web_search tool 핸들러 구현
- [ ] 코드 공유 기능 (CodeSandbox API로 링크 생성)
- [ ] Sonnet 모델 전환 (API 접근 가능해지면)

## 우선순위 낮음
- [ ] 시스템 프롬프트 압축 (입력 토큰 절약)
- [ ] 병렬 코드 생성 (여러 LLM 호출 동시 실행)

## 알려진 이슈
- Haiku가 가끔 generate_code에 code 필드 빼먹음 → .get() 방어 처리 완료
- 프롬프트 내 중괄호가 .format()과 충돌 → import 예시 제거로 해결
- Rate limit (50k input tokens/min) → 라우터 키워드 기반 전환, 재시도 로직 추가
