-- 온프렘 MySQL 8.0 스키마 — RAG 원천(source of truth).
-- 설계: docs/design/rag-db-schema.md §2. Aurora 관리형 대신 온프렘 컨테이너 기준.
-- 벡터 검색은 Redis Stack(HNSW)이 담당하고, 여기엔 임베딩을 JSON으로 원천 보존한다.

CREATE DATABASE IF NOT EXISTS edu_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE edu_agent;

-- 2.1 세션 원천 (기존 projects/<uid>/session_*.json 대체)
CREATE TABLE IF NOT EXISTS sessions (
  session_id   VARCHAR(64)  PRIMARY KEY,
  user_id      VARCHAR(64)  NOT NULL,
  title        VARCHAR(255),
  description  TEXT,
  coding_type  VARCHAR(32),
  app_type     VARCHAR(64),
  phase        VARCHAR(16),
  raw          JSON NOT NULL,               -- 세션 전체 원본(무손실)
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_user (user_id),
  INDEX idx_type (coding_type, app_type),
  INDEX idx_phase (phase)
);

-- 2.2 세분화 지식 청크 (임베딩·재사용 최소 단위)
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  chunk_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id    VARCHAR(64) NOT NULL,
  chunk_type    VARCHAR(32) NOT NULL,       -- learning_note|design_doc|code|diagram|...
  seq           INT DEFAULT 0,
  coding_type   VARCHAR(32),                -- sessions에서 비정규화 복제(필터 성능)
  concept_key   VARCHAR(96),                -- 온톨로지 개념 배정(realized_by)
  intent        VARCHAR(32),
  domain        VARCHAR(32),
  difficulty    VARCHAR(8),
  modi_keys     JSON,
  title         VARCHAR(255),
  content       MEDIUMTEXT NOT NULL,        -- 임베딩·검색 대상
  payload       JSON,
  embedding     JSON,                       -- BGE-m3 1024 float 배열(원천·재구성용)
  embed_model   VARCHAR(32) DEFAULT 'bge-m3',
  source        VARCHAR(16) DEFAULT 'base', -- base | registered(되먹임 등록)
  outcome       VARCHAR(16),
  reusability_score FLOAT DEFAULT 1.0,
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  INDEX idx_filter (chunk_type, coding_type, concept_key),
  INDEX idx_session (session_id),
  INDEX idx_source (source)
);

-- 7.1 경량 온톨로지 (사람이 큐레이션 — LLM 추출 아님)
CREATE TABLE IF NOT EXISTS ontology_nodes (
  node_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
  node_type VARCHAR(24) NOT NULL,           -- concept|modi_module|curriculum|persona
  key_name  VARCHAR(96) NOT NULL,
  label     VARCHAR(255),
  level     INT DEFAULT 0,
  meta      JSON,
  UNIQUE KEY uq_node (node_type, key_name),
  INDEX idx_key (key_name)
);
CREATE TABLE IF NOT EXISTS ontology_edges (
  src_key VARCHAR(96) NOT NULL,
  dst_key VARCHAR(96) NOT NULL,
  rel     VARCHAR(24) NOT NULL,             -- prerequisite|contains|realized_by|uses|relates_to
  weight  FLOAT DEFAULT 1.0,
  PRIMARY KEY (src_key, dst_key, rel),
  INDEX idx_src (src_key, rel)
);

-- 2.3 사용량 영속 기록(#133) — Redis 쿼터 카운터(집행, 48h TTL)와 분리된 분석 원천.
-- 설계: docs/design/token-quota-and-error-structure.md §3.3. 턴당 1행, 영구 보존.
-- 일별/사용자별 집계는 쿼리로 파생(트래픽 규모상 롤업 테이블 불필요).
CREATE TABLE IF NOT EXISTS usage_turns (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  -- 턴 **종료** 시각. **UTC 로 저장된다.**
  --   앱은 tz-aware ISO(+09:00)를 보내고 MySQL 이 세션 타임존(UTC)으로 변환해 넣는다.
  --   2026-08-21 실측: KST 20:11:51 턴 → 11:11:51 로 저장.
  --   ⚠ 그래서 조회할 때 반드시 CONVERT_TZ(ts,'+00:00','+09:00') 로 KST 로 바꿔야 한다.
  --     그러지 않으면 시간대별 집계가 9시간 어긋나고, 오전 09시 이전 턴이 전날로 새어 나간다.
  ts           DATETIME NOT NULL,
  subject      VARCHAR(72) NOT NULL,                    -- u:/s:/m: 접두사 — 쿼터 카운터와 동일 키
  user_id      VARCHAR(64) NOT NULL DEFAULT '',
  session_id   VARCHAR(64) NOT NULL DEFAULT '',
  mode         VARCHAR(16) DEFAULT '',
  coding_type  VARCHAR(16) DEFAULT '',
  input_tokens INT NOT NULL DEFAULT 0,
  output_tokens INT NOT NULL DEFAULT 0,
  cache_read_tokens INT NOT NULL DEFAULT 0,
  cache_creation_tokens INT NOT NULL DEFAULT 0,
  weighted_tokens INT NOT NULL DEFAULT 0,               -- 기록 시점 가중치로 계산(과거값 불변)
  trace_id     VARCHAR(64) DEFAULT '',                  -- Langfuse trace 조인 키(가능할 때만)
  -- 실제로 어느 경로로 나갔는가: cli | api | api_fallback_cli
  -- ⚠ 설정값(USE_LOCAL_CLAUDE)이 아니라 **그 턴이 실제로 탄 경로**다. 둘은 어긋난다:
  --   ① API 인증 실패 폴백 → 그 턴은 구독으로 나가 실청구가 0
  --   ② 운영 중 모드 전환 → 하루 안에 두 경로가 섞인다(2026-08-21 실제 발생)
  --   날짜 단위 라벨로는 실과금을 못 가른다. 그래서 턴마다 남긴다.
  llm_mode     VARCHAR(20) NOT NULL DEFAULT '',
  -- 재사용 라우팅 결과: direct_serve | near | cold
  -- "재사용으로 비용이 얼마나 줄었나"는 티어별 단가를 대조해야만 나온다.
  reuse_tier   VARCHAR(16) NOT NULL DEFAULT '',
  -- ── 부하 관측(2026-08-22 40명 동시 수업 대비) ─────────────────────────────
  -- ts 는 턴 **종료** 시각이다. 시작 시각을 함께 남기면 동시 접속 수를 별도 수집
  -- 없이 **구간 겹침**으로 정확히 셀 수 있다(샘플링이 아니라 전수라 피크를 안 놓친다).
  started_at   DATETIME NULL,
  duration_ms  INT NOT NULL DEFAULT 0,                   -- 턴 총 소요
  -- 학생 체감 대기는 총 소요가 아니라 **첫 글자가 뜨기까지**가 지배한다. 90초 턴이라도
  -- 3초에 첫 글자가 나오면 기다린다. 둘을 갈라야 "느리다"의 정체를 안다.
  ttft_ms      INT NOT NULL DEFAULT 0,
  status       VARCHAR(12) NOT NULL DEFAULT 'ok',        -- ok | error | aborted
  error_code   VARCHAR(32) NOT NULL DEFAULT '',          -- INTERNAL 등(ok 면 빈 값)
  replica      VARCHAR(24) NOT NULL DEFAULT '',          -- edu-agent-1|2|3 (컨테이너 호스트명)

  -- ── 질문 유형·결과 (유형별 비용/성공률) ────────────────────────────────────
  -- intent 는 이미 매 턴 _classify_turn_intent 가 계산하는 값이다. 저장만 안 하고
  -- 버려 왔다 — 분류기를 새로 돌리는 게 아니라 있던 걸 남기는 것뿐이라 비용이 0이다.
  intent       VARCHAR(24) NOT NULL DEFAULT '',          -- question|chat|modify_request|...
  phase        VARCHAR(12) NOT NULL DEFAULT '',          -- design | implement
  -- 이 턴이 실제로 뭘 내놨나. 같은 "질문"이어도 코드가 나온 턴과 대화만 한 턴은
  -- 비용이 10배 다르다. coding_type(react=SW / blockly=HW)과 교차하면 유형별 단가가 나온다.
  outcome      VARCHAR(16) NOT NULL DEFAULT '',          -- code|blockly|doc|chat|none

  -- ── 비용 절감 분석 ─────────────────────────────────────────────────────────
  -- 재사용 후보의 최고 유사도. 티어(reuse_tier)는 "결과"만 알려 주는데, top1 분포가
  -- 있어야 **임계값을 얼마로 내리면 재사용이 몇 % 늘고 얼마가 절감되는지**를 사후에
  -- 계산할 수 있다. 비용 절감의 직접적인 레버라 반드시 원장에 남긴다.
  -- ── 접속 환경 ─────────────────────────────────────────────────────────────
  -- "누가 무엇으로 접속했나". 기기·브라우저별로 실패나 지연이 갈리는지 보려면 필요하고,
  -- 부수적으로 **사람과 스크립트를 가른다** — 부하 테스트 계정은 UA 가 curl 이라
  -- 실제 학생 수와 섞이지 않는다(2026-08-21: 사용자 46명 중 45명이 테스트였다).
  -- 원문을 자른 채로 보관한다 — 파싱 규칙이 바뀌어도 과거를 다시 해석할 수 있게.
  user_agent   VARCHAR(255) NOT NULL DEFAULT '',
  -- 접속 네트워크 구분용(학교/집/모바일). 학교에서는 대부분 같은 공인 IP 로 보인다.
  -- ⚠ 개인정보에 해당하므로 리포트 토큰 뒤에서만 노출한다.
  client_ip    VARCHAR(45) NOT NULL DEFAULT '',
  -- 턴 종료 시점의 컨테이너 메모리(MB). 레플리카에 1g 상한이 걸려 있어(compose)
  -- 상한에 닿으면 그 컨테이너만 재시작된다 — **닿기 전에** 보여야 손을 쓸 수 있다.
  mem_mb       INT NOT NULL DEFAULT 0,

  reuse_top1     FLOAT NOT NULL DEFAULT 0,
  direct_served  TINYINT NOT NULL DEFAULT 0,             -- 저장물을 그대로 서브해 생성 LLM 0회
  docs_restored  INT NOT NULL DEFAULT 0,                 -- 직접서브가 복원한 문서 수(RAG 투자 회수 근거)

  INDEX idx_subject_ts (subject, ts),
  INDEX idx_user_ts (user_id, ts),
  INDEX idx_mode_ts (llm_mode, ts),
  INDEX idx_tier_ts (reuse_tier, ts),
  INDEX idx_status_ts (status, ts),
  INDEX idx_intent_ts (intent, ts),
  INDEX idx_started (started_at)
);

-- ⚠ 기존 배포에 이 두 컬럼을 더하는 일은 여기서 하지 않는다.
--    apply_schema.py 는 파일을 세미콜론으로 쪼개 순차 실행하므로 CREATE PROCEDURE 처럼
--    본문에 세미콜론이 있는 문장을 넣으면 깨진다. ALTER 는 apply_schema.ensure_columns()
--    가 information_schema 를 보고 멱등하게 처리한다.

-- 2.5 운영 사건 기록 — **턴이 만들어지지 않는 사건** 전용.
--
-- 왜 usage_turns 로 부족한가: 동시 접속 거절(session_busy)·쿼터 소진·차단은 /chat 이
-- 세션 락을 잡기 **전에** 곧바로 return 한다. 그래서 사용량 기록이 도는 finally 에
-- 아예 도달하지 못하고, 원장에는 흔적이 남지 않는다. 40명 동시 수업에서 가장 중요한
-- 숫자가 바로 "몇 명이 튕겼나"인데 그게 Sentry 에만 있고 리포트에는 없었다.
--
-- 컨테이너 재시작·헬스체크 실패처럼 세션과 무관한 사건도 같은 테이블에 모은다 —
-- "그 시각에 응답이 느렸다"와 "그 시각에 서버가 재시작했다"를 한 시간축에서 겹쳐 봐야
-- 원인이 보이기 때문이다.
--
-- ts 는 usage_turns 와 동일하게 **UTC** 다(조회 시 CONVERT_TZ).
CREATE TABLE IF NOT EXISTS ops_events (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts         DATETIME NOT NULL,
  kind       VARCHAR(24) NOT NULL,                   -- session_busy|user_quota|blocked|error|restart|health_fail
  code       VARCHAR(32) NOT NULL DEFAULT '',        -- 에러코드 등 세부 구분
  user_id    VARCHAR(64) NOT NULL DEFAULT '',
  session_id VARCHAR(64) NOT NULL DEFAULT '',
  replica    VARCHAR(24) NOT NULL DEFAULT '',
  detail     VARCHAR(255) NOT NULL DEFAULT '',
  INDEX idx_ts (ts),
  INDEX idx_kind_ts (kind, ts),
  INDEX idx_user_ts (user_id, ts)
);

-- 2.4 일별 리포트 확정본 — 파일이 아니라 DB 가 원천이다.
--
-- 왜 테이블인가: 리포트는 "열 때마다 다시 계산"으로는 청구 근거가 못 된다. 원본
-- usage_turns 가 정리되거나 집계 로직이 바뀌면 과거 수치가 조용히 달라지기 때문이다.
-- 그날 값을 굳혀 두고, 이후에는 이 테이블을 읽는다.
--
-- 시간 규약(중요):
--   day             = **KST 기준 영업일 라벨**이다. 타임스탬프가 아니라 "8월 22일 수업"
--                     이라는 회계 날짜다. 그래서 시간대 변환 대상이 아니다.
--   *_at_utc        = 실제 시각. **UTC 로 저장**하고 화면에서 KST 로 변환해 보여준다.
--                     서버 로케일·컨테이너 TZ 가 바뀌어도 값이 흔들리지 않게 하기 위함.
--
-- 스칼라 컬럼 + payload JSON 을 함께 둔다: 목록/추세는 스칼라로 빠르게 훑고,
-- 상세 화면은 payload 로 무손실 복원한다(집계 로직이 바뀌어도 그날 본 화면 그대로).
CREATE TABLE IF NOT EXISTS usage_reports (
  day                   DATE PRIMARY KEY,               -- KST 영업일 (라벨)
  generated_at_utc      DATETIME NOT NULL,              -- 굳힌 시각 (UTC)
  turns                 INT     NOT NULL DEFAULT 0,
  users                 INT     NOT NULL DEFAULT 0,
  sessions              INT     NOT NULL DEFAULT 0,
  projects              INT     NOT NULL DEFAULT 0,
  input_tokens          BIGINT  NOT NULL DEFAULT 0,
  output_tokens         BIGINT  NOT NULL DEFAULT 0,
  cache_read_tokens     BIGINT  NOT NULL DEFAULT 0,
  cache_creation_tokens BIGINT  NOT NULL DEFAULT 0,
  weighted_tokens       BIGINT  NOT NULL DEFAULT 0,
  usd                   DECIMAL(12,4) NOT NULL DEFAULT 0,
  krw                   BIGINT  NOT NULL DEFAULT 0,
  llm_mode              VARCHAR(8)  DEFAULT '',         -- cli | api (실청구 해석에 필요)
  payload               JSON    NOT NULL,               -- 리포트 전체 원본(무손실)
  insight               MEDIUMTEXT,                     -- AI 분석 본문(마크다운)
  insight_model         VARCHAR(64) DEFAULT '',
  insight_at_utc        DATETIME NULL,
  INDEX idx_generated (generated_at_utc)
);
