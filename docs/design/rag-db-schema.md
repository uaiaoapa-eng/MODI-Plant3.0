# RAG/검색용 DB 스키마 설계 — 응답 콘텐츠 세분화 & 저장

> 상태: 제안(Draft) · 대상: edu-agent · 작성 기준일: 2026-07-01
> 상위 설계: [`langfuse-rag-architecture.md`](./langfuse-rag-architecture.md) (L4 코퍼스 구체화)
> 목적: 현재 `projects/<uid>/session_*.json` 통짜 저장을 → **카테고리별 임베딩 청크 + DB 원천**으로 전환하고,
> 사용자 질문 의도(intent)에 맞춰 검색/재사용/포괄 제시할 수 있게 한다.

---

## 0. TL;DR (결론부터)

1. **저장은 2계층.** ① **MySQL = 원천(source of truth)** — 세션 원본 + 세분화된 지식 청크 + 임베딩. ② **벡터 인덱스** = 검색 가속층(재구성 가능).
2. **검색 엔진 권장: Redis Stack (RediSearch).** 이미 compose에서 Redis를 돌리고 있으므로 `redis:7-alpine` → `redis/redis-stack-server`로 **바꾸기만** 하면 HNSW 벡터 + 태그 필터 + BM25 키워드를 한 서비스에서 얻는다. 신규 인프라 최소.
   - PoC/초기엔 **MySQL만으로도 충분**(앱 코사인 or MySQL 9.x `VECTOR`). 청크 1만 개 미만이면 브루트포스가 수 ms.
   - Qdrant/Milvus/Weaviate 같은 전용 벡터DB는 **지금 규모(학생 <2,000, 청크 <10만)엔 오버킬** — 나중에 승격.
3. **세분화의 핵심 단위 = 세션이 아니라 "카테고리 청크".** 학습노트 1개, 설계문서 1개, 코드파일 1개, 다이어그램 1개가 각각 독립 검색·재사용 단위다. `knowledge_chunks` 한 테이블에 `chunk_type`으로 구분해 담는다.
4. **"포괄적으로 보여주기" = 멀티 카테고리 팬아웃 + 하이브리드 검색 + MMR 다양성.** 한 청크만 주지 말고, 한 질문에 대해 설계+학습노트+코드+다이어그램을 묶어 하나의 "포괄 카드"로 조립한다.

---

## 1. 현재 데이터 구조 → 청크 매핑

`session_*.json` 실측 필드를 재사용 단위로 분해한다.

| 세션 JSON 필드 | 카테고리 | `chunk_type` | 청크 분할 방식 |
|---|---|---|---|
| `title` + `description` | 프로젝트 요약 | `project_summary` | 세션당 1개 (대표 벡터) |
| `learning_notes[]` `{title,what,why,where}` | **학습노트** | `learning_note` | **노트 1개 = 청크 1개** |
| `design_doc` (features/pages/data_models/user_flows) | **설계문서** | `design_doc` | 세션당 1개 (크면 섹션별) |
| `blockly_code_langs{python,js,c}`, `generated_code`, `files`, `code_annotations` | **코딩결과** | `code` | 언어/파일별 청크 |
| `diagram`, `blockly_flowchart`, `blockly_xml` | 설계 시각화 | `diagram` | 세션당 1개 |
| `task_plan.tasks[]` | 결과값/계획 | `task_plan` | 세션당 1개 |
| `modi_modules` | 하드웨어 구성 | `hw_config` | 세션당 1개 |
| `messages[]` / `conversation` | 대화 원문 | (원천만) | 임베딩은 정규화 요청으로 별도 |

> **요청(request) 임베딩**은 상위 설계의 L3 원칙대로 `messages`의 사용자 발화를 **정규화 + intent 결합**해서 `request` 청크로 따로 만든다. "만들어줘/설명해줘" 같은 표현 변형을 같은 의도로 묶기 위함.

---

## 2. MySQL 스키마 (원천)

> 현재 compose엔 MySQL이 없다 → 서비스 추가 필요(§5). 회사 표준이 MySQL이므로 원천은 MySQL로 둔다.

```sql
-- 2.1 세션 원천 (기존 projects/<uid>/session_*.json 대체)
CREATE TABLE sessions (
  session_id   VARCHAR(64)  PRIMARY KEY,
  user_id      VARCHAR(64)  NOT NULL,
  title        VARCHAR(255),
  description  TEXT,
  coding_type  VARCHAR(32),          -- blockly | hardware | react | ...
  app_type     VARCHAR(64),
  phase        VARCHAR(16),          -- design | implement | verify
  raw          JSON NOT NULL,        -- 세션 전체 원본(무손실 보존)
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_user (user_id),
  INDEX idx_type (coding_type, app_type),
  INDEX idx_phase (phase)
);

-- 2.2 세분화된 지식 청크 (임베딩·재사용의 최소 단위) ★핵심★
CREATE TABLE knowledge_chunks (
  chunk_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id   VARCHAR(64) NOT NULL,
  chunk_type   VARCHAR(32) NOT NULL,   -- project_summary|learning_note|design_doc|code|diagram|task_plan|hw_config|request
  seq          INT DEFAULT 0,          -- 같은 타입 내 순번(학습노트 3번째 등)

  -- 통제 어휘(taxonomy) — 필터용. 상위 설계 §3의 enum과 일치.
  intent       VARCHAR(32),            -- create_component|fix_bug|explain_concept|design_review|...
  domain       VARCHAR(32),            -- react_state|blockly|algorithm|... (교육 커리큘럼 축)
  actor_role   VARCHAR(16),            -- student|teacher|anon
  difficulty   VARCHAR(8),             -- easy|medium|hard
  code_lang    VARCHAR(16),            -- python|javascript|c (code 청크 전용)
  modi_keys    JSON,                   -- hw_config/hardware일 때 모듈 키 배열 (["network","dial",...])

  -- 검색 대상 본문
  title        VARCHAR(255),
  content      MEDIUMTEXT NOT NULL,    -- 임베딩·BM25 대상 텍스트(카테고리별 직렬화)
  payload      JSON,                   -- 렌더링용 구조화 원본(코드 블록, mermaid 등)

  -- 임베딩 (BGE-m3, 1024차원, 로컬)
  embedding    JSON,                   -- MySQL 8.0: float 배열 JSON / MySQL 9.x면 VECTOR(1024)로
  embed_model  VARCHAR(32) DEFAULT 'bge-m3',
  schema_version INT DEFAULT 1,

  -- 품질 게이트 (재사용 오염 방지 — 상위 설계 R2/R3)
  outcome            VARCHAR(16),      -- success|partial|failed|blocked
  build_success      TINYINT(1),
  human_feedback     TINYINT,          -- +1 / 0 / -1 (G5, 아직 없음 → Phase 2)
  reusability_score  FLOAT DEFAULT 0,  -- 파생값; τ 이상만 검색 노출

  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  INDEX idx_filter (chunk_type, intent, domain, coding_type_dummy(0)) /* 아래 참고 */,
  INDEX idx_gate (reusability_score),
  INDEX idx_session (session_id)
);
```

> `coding_type`은 sessions에 있으므로 청크 필터 시 조인하거나, 조회 성능 위해 `knowledge_chunks`에 비정규화 복제(권장). 위 `coding_type_dummy`는 자리표시 — 실제론 `coding_type VARCHAR(32)` 컬럼을 청크에도 두고 `INDEX idx_filter (chunk_type, intent, domain, coding_type)`.

**임베딩 저장 (확정: AWS Aurora MySQL 8.0):**
- Aurora MySQL 8.0엔 **네이티브 `VECTOR` 없음**(그건 MySQL 9.x/HeatWave, 또는 Aurora *PostgreSQL* pgvector). → `embedding JSON`(float 배열)로 Aurora에 저장(원천·재구성용).
- 벡터 검색은 **반드시 외부 엔진**: PoC는 앱 코사인(청크 <1만 수 ms), 운영은 Redis Stack(§3). 네이티브 SQL 벡터 옵션은 이 프로젝트에선 폐기.

---

## 3. 검색 엔진 결정 (mysql / redis / 그 외)

> **인프라 확정: AWS Aurora MySQL 8.0 (관리형 원격).** → 원천은 Aurora. Aurora는 네이티브 벡터 없음 → 벡터는 외부 엔진 필수. compose엔 `mysql` 컨테이너 불필요(엔드포인트만 연결).

| 후보 | 지금 도입 비용 | 벡터 | 필터 | 키워드(BM25) | 판단 |
|---|---|---|---|---|---|
| **앱 코사인 (Aurora JSON 직접)** | **0** (원천 그대로) | 브루트포스 | SQL WHERE ✅ | FULLTEXT(한국어 약함) | **권장 (PoC/초기)** |
| **self-host Redis Stack (RediSearch)** | **낮음** (이미 Redis 운영 중, 이미지만 교체) | HNSW ✅ | 태그/숫자 ✅ | ✅ | **권장 (운영)** |
| Amazon MemoryDB 벡터 | 중(관리형) | ✅ | ✅ | 일부 | 관리형 Redis 원하면 대안 |
| Amazon OpenSearch (k-NN) | 중~높(관리형) | ✅ | ✅ | ✅ 최강 | 청크 >10만/학생 2,000+ 때 승격 |
| ~~ElastiCache Redis~~ | — | ❌ RediSearch 미지원 | — | — | **벡터 불가 — 쓰지 말 것** |
| Aurora 네이티브 VECTOR | — | ❌ 8.0엔 없음(9.x/HeatWave) | — | — | 폐기 |

**결론:**
- **초기(콜드~PoC):** Aurora + 앱 코사인. 신규 인프라 0. 상위 설계 Phase 4의 "shadow 모드"와 일치.
- **운영 전환:** compose의 `redis:7-alpine`을 `redis/redis-stack-server`로 교체 → 벡터+필터+키워드 한 곳. 세션락용 기존 Redis와는 **DB 인덱스 분리**(상위 설계 5.1).
- 규모 확장: 관리형 원하면 **MemoryDB(벡터)** 또는 **OpenSearch(k-NN)**. ⚠ **ElastiCache Redis는 RediSearch 미지원**이라 벡터 불가 — 함정 주의.
- 원천은 항상 Aurora. 벡터 인덱스는 언제든 Aurora에서 재구성.

**임베딩 모델:** BGE-m3 로컬 (한국어·$0·사내유지, 1024차원) — 상위 설계 확정. `sentence-transformers` or `FlagEmbedding`으로 앱 내 임베딩, 온프렘 CPU도 배치로 감당.

---

## 4. "포괄적으로 보여주기" — 검색·조립 전략

단일 top-1 청크를 주면 안 된다. 아래를 조합한다.

1. **멀티 카테고리 팬아웃(★):** 한 질문에 대해 `chunk_type`별로 각각 top-k 검색 → 설계 1 + 학습노트 2 + 코드 1 + 다이어그램 1을 **하나의 "포괄 카드"로 조립**. 사용자가 개념·설계·구현을 한 번에 본다.
2. **하이브리드 검색:** 벡터 유사도 + BM25 키워드 + 메타 필터(intent/domain/coding_type)를 **RRF(Reciprocal Rank Fusion)**로 병합. 벡터가 놓치는 정확 용어(모듈명 "dial" 등)를 키워드가 잡는다.
3. **MMR 다양성:** top-k가 서로 거의 같은 답이면 무의미 → Maximal Marginal Relevance로 중복 억제, 관점 다양화.
4. **의도 조건부 라우팅:** `router.classify()`/guardrail category 재활용. "설명해줘"→ `learning_note` 부스트, "만들어줘"→ `code`+`design_doc` 부스트, "고쳐줘"→ 같은 domain의 `fix_bug` 이력.
5. **부모 하이드레이션(parent-child):** 청크가 매칭되면 `session_id`로 원 세션을 끌어와 전체 맥락(다이어그램+코드+노트)을 함께 제시.
6. **품질/신선도 게이팅:** `reusability_score >= τ AND outcome=success` 만 노출. 교육 오류 전파 방지(상위 설계 R3).

**페르소나 필터와 결합:** 이전 세션의 페르소나(학년/난이도/coding_type) 축을 `knowledge_chunks`의 `difficulty`/`domain`/`coding_type` 필터로 그대로 쓴다 → "초등 2학년·블록코딩" 청크만 검색.

---

## 5. docker-compose 델타 (최소 변경)

> Aurora MySQL은 **AWS 관리형 원격** → compose에 `mysql` 컨테이너 없음. 앱은 Aurora 엔드포인트에 연결만.

```yaml
services:
  edu-agent:
    depends_on:
      redis: { condition: service_healthy }
    environment:
      # Aurora MySQL 8.0 (writer 엔드포인트). 자격증명은 .env/시크릿으로.
      DATABASE_URL: "mysql+pymysql://${DB_USER}:${DB_PASS}@${AURORA_HOST}:3306/edu_agent"
      # 벡터 인덱스용 Redis Stack (세션락 Redis와 논리 분리: DB 1)
      VECTOR_REDIS_URL: "redis://redis:6379/1"

  # 기존 세션락 redis를 Stack으로 승격 → 벡터 인덱스 겸용
  redis:
    image: redis/redis-stack-server:latest   # was redis:7-alpine
    # RediSearch/RedisJSON 포함. 세션락(DB0) + 벡터인덱스(DB1) 분리.
```

> - PoC 단계: Redis Stack 없이 Aurora + 앱 코사인만으로 시작 가능(위 `redis`는 기존 세션락 그대로 두고, 벡터는 앱 메모리 코사인).
> - 관리형 벡터로 갈 땐 `VECTOR_REDIS_URL`을 MemoryDB 엔드포인트로 교체하거나 OpenSearch 클라이언트로 전환.
> - 마이그레이션: 기존 `projects/*/session_*.json` 125건 → Aurora `sessions.raw` 백필 스크립트 + 카테고리 분해해 `knowledge_chunks` 적재.

---

## 6. 단계 실행 (상위 설계 로드맵과 정합)

| 단계 | 내용 | 산출 |
|---|---|---|
| **S1** | MySQL 서비스 추가 + `sessions`/`knowledge_chunks` DDL + `session_store.py`를 파일→DB 이중쓰기 | 원천 DB화 |
| **S2** | 기존 125 세션 백필 + 카테고리 분해기(`json → chunks`) | 초기 코퍼스 |
| **S3** | BGE-m3 로컬 임베딩 배치 → `embedding` 채움 | 벡터 확보 |
| **S4** | 앱 코사인 검색 API(shadow 모드, MySQL만) | "재사용했다면?" 시뮬 |
| **S5** | Redis Stack 인덱스 + 하이브리드/팬아웃/MMR | 포괄 검색 |
| **S6** | 의도 라우팅 + 품질 게이트 on + 폐루프 측정 | 실제 재사용 |

> 품질 게이트의 정답 신호(`human_feedback`, G5)는 아직 없다 → 상위 설계 Phase 2(피드백 UI)가 선행되어야 재사용 정확도가 산다.

---

## 7. GraphRAG / 온톨로지 판단

**결론: 풀 GraphRAG(Microsoft식 LLM 자동추출+커뮤니티요약)는 채택하지 않는다. "벡터 RAG + 경량 큐레이션 온톨로지" 하이브리드를 쓴다.**

- **풀 GraphRAG 제외 이유:** ① 구축 단계에서 청크마다 LLM 엔티티/관계 추출 → 비용 절감 목적과 정면 충돌(R1 악화). ② 도메인이 이미 구조화(MODI 유한 셀 + 커리큘럼 축)라 자동 테마 발견 불필요. ③ taxonomy(§3)가 이미 온톨로지 초안.
- **경량 온톨로지 채택 이유(교육 도메인 특수성):** 벡터 유사도로 못 하는 ①선수학습(prerequisite) ②페르소나→커리큘럼 경로 ③커버리지 맵(사전제작 우선순위)을 엣지로 표현. 그래프는 **LLM 추출이 아니라 커리큘럼에서 사람이 큐레이션**(비용 ≈ $0, 상위 설계 6-C와 정합).

### 7.1 온톨로지 스키마

노드: `curriculum`(학년/과목) · `concept`(변수·반복·조건·센서값매핑...) · `modi_module` · `persona`(학년·나이·난이도·MBTI).
엣지: `prerequisite`(선수학습·방향) · `contains` · `realized_by`(개념→청크) · `uses`(청크→모듈) · `targets`(페르소나→개념) · `relates_to`(연관).

```sql
CREATE TABLE ontology_nodes (
  node_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
  node_type VARCHAR(24) NOT NULL,   -- curriculum|concept|modi_module|persona
  key_name  VARCHAR(96) NOT NULL,   -- 'loop','sensor_range_map','dial'...
  label     VARCHAR(255),
  meta      JSON,
  UNIQUE KEY uq_node (node_type, key_name)
);
CREATE TABLE ontology_edges (
  src_id BIGINT NOT NULL,
  dst_id BIGINT NOT NULL,
  rel    VARCHAR(24) NOT NULL,      -- prerequisite|contains|realized_by|uses|targets|relates_to
  weight FLOAT DEFAULT 1.0,
  PRIMARY KEY (src_id, dst_id, rel),
  FOREIGN KEY (src_id) REFERENCES ontology_nodes(node_id) ON DELETE CASCADE,
  FOREIGN KEY (dst_id) REFERENCES ontology_nodes(node_id) ON DELETE CASCADE
);
-- 연결: knowledge_chunks 에 concept_id BIGINT 컬럼 추가 → concept 노드와 결합
ALTER TABLE knowledge_chunks ADD COLUMN concept_id BIGINT NULL, ADD INDEX idx_concept (concept_id);
```

저장은 이 규모에선 **MySQL 엣지 테이블 + 재귀 CTE**로 충분. Neo4j 등 전용 그래프DB는 트래픽/그래프가 커지면 승격.

### 7.2 검색 = 벡터 후보 → 그래프 확장 (2단)

```
질문 → intent 분류 + 페르소나 필터
  → (벡터) knowledge_chunks top-k 후보
  → (그래프) 후보 청크의 concept 노드로 점프
       ├ prerequisite 역방향: 필요한 선행개념 노출
       ├ realized_by: 같은 개념의 다른 카테고리 청크 팬아웃(설계+노트+코드)
       └ relates_to: 연관 개념 추천
  → MMR 다양화 → "포괄 카드" 조립
```

§4의 "멀티 카테고리 팬아웃"이 그래프의 `realized_by`/`relates_to`로 정당화된다 — 유사도 우연이 아니라 개념 구조 기반.

### 7.3 커버리지 맵 (사전제작 우선순위)

`(concept × difficulty)` 셀별로 연결된 고품질 청크 수를 집계 → **고트래픽·저커버리지 셀**을 상위 설계 6-D의 Opus 배치 저술 대상으로. 온톨로지가 "무엇을 미리 만들지"를 데이터로 답한다.
```
