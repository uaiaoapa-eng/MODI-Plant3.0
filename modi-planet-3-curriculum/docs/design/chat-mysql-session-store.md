# /chat ↔ 온톨로지 프라임 ↔ MySQL 세션 스토어 설계 (#27 P3)

이슈 #27("RAG 온톨로지·페르소나 도출 시스템")의 운영 이식 단계. `/chat` 흐름을
온톨로지 검색과 결합하고, 세션(대화/프로젝트) 저장을 파일에서 MySQL 원천으로 이행한다.

관련 문서: `rag-db-schema.md`(스키마), `langfuse-rag-architecture.md`(비용/관측).

---

## 1. 배경 / 문제

- `/chat` 은 온톨로지·페르소나 도출(`rag_demo_app.derive`)과 **분리**돼 있었다 — 개념/선수학습
  제안이 생성에 반영되지 않음.
- 세션(대화/프로젝트)은 **파일 전용**(`projects/<uid>/session_*.json`)이라 멀티박스/유실에
  취약하고, MySQL 원천(운영)과 이원화.
- 요구된 3가지: ① 유사 요청 → 맞는 학습노트·코드 **제안** ② **온톨로지 개념·선수학습 경로**
  함께 제안 ③ **매번 동일 제공 금지** — '복사'가 아니라 '각색'.

## 2. 배포 토폴로지 (전제)

```
[edu-agent 앱 :18080]  torch·MySQL 없음. RAG_UPSTREAM 로 프록시.
        │  RAG_UPSTREAM=http://host.docker.internal:8100
        ▼
[rag-search :8100]  RAG_BACKEND=mysql_redis, BGE-m3(torch)
        ├── MySQL 8.0   (원천: sessions / knowledge_chunks / ontology_*)
        └── Redis Stack (벡터 HNSW)
```
핵심 제약: **앱은 MySQL·torch 에 직접 닿지 않는다.** 모든 DB/시맨틱 작업은 rag-search 경유.

## 3. 온톨로지 제안형 프라임 (읽기)

`agent/reuse.py :: ontology_suggest(user_input, coding_type, user_id, seen, top)`
- **개념 식별**: `ontology_lib.match_concepts`(alias, 오프라인) → 핵심 개념
- **그래프 확장**: `prerequisites`/`related`(sqlite 시드, MySQL과 동일 큐레이션)
- **유사 결과물**: `_fetch_artifacts` — `RAG_UPSTREAM` 있으면 `/api/search`(MySQL/시맨틱),
  없으면 인프로세스 `search_lib`. reuse/review 티어만(콜드셀 제외).
- **anti-monotony**: 프라임 문구 "그대로 복사하지 말고 각색"; `seen` 으로 이미 제안한 항목
  다음 턴 회피. 개념 하나 반복 대신 선수학습 **경로**로 제시.

주입: `orchestrator_stream._reuse_block` — 빌드 직전 시스템 프롬프트에 프라임 블록 추가
(기존 #44 코드 재사용 게이트와 병행, 시그니처 불변). 세션 `_suggested_keys` 누적.

## 4. 세션 MySQL 이행 — 이중쓰기 → 원천

무손실·무중단 전환: **이중쓰기(파일 + MySQL) + 읽기 union**.

| 동작 | 경로 |
|---|---|
| 저장 `auto_save` | 파일 write + `_session_writeback_upstream` → `/api/session/save`(MySQL raw) |
| 리스트 `GET /projects` | 파일 목록 ∪ `/api/session/list`(session_id 기준, MySQL 우선) |
| 열기 `GET /projects/{id}` | `/api/session/get` 프록시(실패/부재 시 파일 폴백) |
| **`/chat` 복원** `get_orchestrator` | 파일 있으면 파일; 없고 프록시면 `_hydrate_from_upstream`(MySQL→파일 materialize→복원) |
| 삭제 `DELETE /projects/{id}` | 파일 + `/api/session/delete` |

rag-search 신규 엔드포인트(`scripts/rag_demo_app.py`): `POST /api/session/save`,
`GET /api/session/list|get`, `DELETE /api/session/delete`.
접근 계층(`scripts/store_mysql.py`): `list_sessions`/`get_session`/`delete_session`
(`upsert_session` 기존, raw JSON 무손실). 스키마: 기존 `sessions` 테이블로 충분 — **DDL 추가 없음**.

## 5. 프로세스 과정 (end-to-end)

**A. 사용자가 `/chat` 으로 새/이어가기 요청**
1. `POST /chat` → `get_orchestrator(session_id, user_id)`
2. 파일 있음 → `_restore_state_from_file` / 파일 없고 프록시 → `_hydrate_from_upstream`
   (`GET /api/session/get` → 받은 raw 를 파일로 내려받아 복원)
3. 빌드 직전 `_reuse_block`:
   - `ontology_suggest` → 개념·선수학습 + `_fetch_artifacts`(`GET /api/search`) → 프라임 주입
4. LLM 생성(각색) → 코드/설계/노트 산출

**B. 턴 종료 후 `auto_save`**
5. `_build_save_data` → 파일 write (`projects/<uid>/<sid>.json`)
6. `_rag_feedback` → `POST /api/writeback` (파생 청크 등록)
7. `_session_writeback_upstream` → `POST /api/session/save` (세션 raw 이중쓰기)

**C. 리스트/열기/삭제** — §4 표 경로. 배포 시 `backfill_sessions.py` 로 기존 파일 일괄 이관.

## 6. 데이터 DB 흐름 (테이블 레벨)

```
쓰기 (chat 1턴):
  파일:   projects/<uid>/<sid>.json                     (전문, 소스오브트루스 겸 캐시)
  MySQL:  sessions(session_id, user_id, title, desc,    ← /api/session/save
                   coding_type, app_type, phase, raw=전문 JSON, updated_at)
  MySQL:  knowledge_chunks(chunk_type, concept_key,     ← /api/writeback (파생)
                   intent, domain, difficulty, modi_keys,
                   title, content, payload, embedding, source='registered')
  Redis:  HNSW 벡터(BGE-m3 임베딩) + 개념 centroid       ← writeback 시 upsert

읽기:
  대화 리스트  → SELECT session_id,title,...,raw,UNIX_TIMESTAMP(updated_at)
                 FROM sessions WHERE user_id=?  (∪ 파일 목록)
  프로젝트 열기 → SELECT raw FROM sessions WHERE session_id=? [AND user_id=?]
  /chat 복원   → 위 열기와 동일(hydrate) → 파일 materialize → 상태 복원
  유사 결과물  → /api/search: knowledge_chunks + Redis 벡터(하이브리드 시맨틱)
  개념/선수학습 → ontology_nodes/ontology_edges (프라임의 그래프 부분)
```

폐루프: `chat 생성 → sessions/knowledge_chunks/Redis 적재 → 다음 chat 의 프라임이
/api/search 로 재조회하여 각색 제안 / 세션은 /api/session/get 으로 hydrate`.

## 7. 배포

`.github/workflows/deploy.yml`(self-hosted, master push — `server.py`·`agent/**`·`scripts/**` 포함):
① 메인 앱 재빌드 → ② rag-search 재빌드 → ③ mysql/redis 기동 → ④ RAG 청크 백필 →
⑤ rag-search 재기동 → ⑥ **세션 백필**(`backfill_sessions.py`, 메인 앱 컨테이너 프록시 POST, 멱등) →
⑦ 헬스/gold 검증. 초기 이관 스크립트는 `RAG_UPSTREAM` 있으면 프록시, 없으면 `store_mysql` 직결.

## 8. 검증 / 남은 작업

- 단위: `tests/test_ontology_prime.py` (A/B/C + 프록시 라우팅). 전체 289 passed.
- ⚠️ MySQL 왕복은 **라이브 DB(온프렘)에서 검증** 필요(로컬 DB 부재).
- 남음: 파일 쓰기 제거(완전 단일화), `ChatRequest.grade` 학년 게이팅(#27 페르소나),
  `uses` 엣지 정제·시드 검수(#27 P1).
