# 온프렘 배포 — 하이브리드 검색 데모

질문 → **벡터(BGE-m3) + 부분일치 하이브리드**로 학습노트 청크를 랭킹하고,
유사도 임계값으로 **일부는 재사용(가져오기) · 일부는 신규 등록**으로 게이팅하는 데모.

- **검색 탭** — 질문 → 매칭 학습노트 청크 직접 노출(결정 배지 포함)
- **도출 탭** — 질문 + 페르소나 → 개념 매칭 → 선수학습·연관·MODI 그래프 → 학습노트
- **커버리지 탭** — 질문 묶음의 재사용/근접/등록 **비율(%)** = 목표 히트율 확인

## 두 가지 실행 모드

1. **통합(권장·운영)** — 메인 서버 `server.py`에 RAG가 내장됨. uuid(user_id) 인증을 채팅과 공유.
   - UI: `GET /rag` · 검색: `/api/search` · 등록: `POST /api/register` · 도출: `/api/query` · 커버리지: `/api/coverage`
   - `user_id`는 `?user_id=` 쿼리 또는 `X-User-Id` 헤더(채팅과 동일). 등록 소유자는 인증 user_id로 자동 지정.
   - 기동: 기존 메인 서버 그대로(`uvicorn server:app ...`). RAG 자산(`data/`)만 있으면 됨.
2. **독립 데모(경량)** — RAG만 단독 컨테이너로. 채팅/LLM 의존 없음.

## 독립 데모 빠른 시작

```bash
# 리포 루트에서
bash deploy/install.sh          # 풀(벡터) 이미지 — 권장
bash deploy/install.sh --lite   # 경량(부분일치 단독) — torch 불필요, 빠른 기동
```

기동 후: `http://<서버IP>:8100`

## 온프렘 운영 스택 (MySQL + Redis Stack) — 영속 등록 배선 런북

단일 컨테이너 데모를 넘어 **원천=MySQL 8.0 · 벡터검색=Redis Stack(HNSW)** 로 운영하면
등록물이 파일이 아닌 **MySQL+Redis 에 영속 저장**되어 재배포에도 누적된다.

**아키텍처(네트워크 분리 유지)**: 메인 앱(프로젝트 `edu-agent`)은 경량 프록시(torch 無)로,
검색·임베딩·등록은 별도 프로젝트 `edu-agent-rag`(rag-onprem 스택)의 `rag-search`(:8100)에
위임한다. 메인 앱은 `onprem.env` 의 `RAG_UPSTREAM` 으로만 이 스택과 통신하며, compose 는
**병합하지 않는다**(같은 redis 를 섞으면 메인 세션락을 오염시키는 사고 전례 — 실측 확인됨).

### 1) rag-onprem 스택 기동 (스키마는 최초 기동 시 자동 적용)

```bash
cp deploy/.env.example .env    # MySQL 자격증명·임계값(TAU_*) 설정
docker compose -f docker-compose.rag-onprem.yml config   # 렌더 검증(문법·변수 치환 확인)
docker compose -f docker-compose.rag-onprem.yml up --build -d
```

- `deploy/schema.sql` 은 mysql 컨테이너 **최초 기동 시** `docker-entrypoint-initdb.d` 로 자동
  적용된다(별도 수동 DDL 단계 불필요).
- `mysql`(원천) + `redis/redis-stack-server`(HNSW 벡터) + `rag-search`(`RAG_BACKEND=mysql_redis`).

### 1-1) 스키마 재적용 (기존 볼륨에 새 테이블 반영, #149)

`deploy/schema.sql`에 테이블이 추가된 뒤(예: 사용량 기록 `usage_turns`, #133) **이미 데이터가
쌓인** `mysql_data` 볼륨에는 `docker-entrypoint-initdb.d`가 재실행되지 않아 새 테이블이
반영되지 않는다 — 그 상태에서 해당 테이블에 쓰려는 INSERT는 500으로 실패한다(Sentry #101).
데이터를 지우는 `down -v` **없이** 스키마만 재적용하려면:

```bash
# rag-search 컨테이너 안(또는 리포·pymysql 이 있는 호스트)에서, 실행 중 MySQL을 가리켜 실행.
# CREATE DATABASE IF NOT EXISTS 문도 함께 실행되므로 root 자격증명 권장(docker-entrypoint-initdb.d 와 동일 전제).
DATABASE_URL=mysql+pymysql://root:${MYSQL_ROOT_PASSWORD:-rootpw}@localhost:3306/edu_agent \
  python scripts/apply_schema.py
```

- `deploy/schema.sql` 전체를 위에서부터 순차 실행한다. 전 문이 `CREATE DATABASE/TABLE
  IF NOT EXISTS`뿐이라 몇 번을 재실행해도 안전(멱등) — 기존 테이블·데이터는 그대로 두고
  없는 테이블만 새로 생긴다. 컬럼을 바꾸는 `ALTER` 마이그레이션은 다루지 않는다(별도 설계
  필요, Non-goals).
- 반영 후 확인: `curl -s -X POST http://localhost:8100/api/usage/add -H 'content-type:
  application/json' -d '{"ts":"2026-07-14T00:00:00+09:00","subject":"u:test"}'` 가 200을
  반환하면 성공. Sentry #101(usage_persist 500)이 더 이상 발생하지 않는지도 함께 확인한다.
- 설계 상세: `docs/design/chat-error-surfacing-and-usage-turns-fix.md` §3.1-C.

### 2) 씨앗 백필 (최초 1회 또는 데이터 갱신 시)

```bash
# 로컬 base 자산(ontology.db + chunk_emb.npy) → MySQL + Redis 적재. base 821건.
docker compose -f docker-compose.rag-onprem.yml run --rm backfill
curl -s http://localhost:8100/health    # rag-search 확인(vector_enabled·registered 노출)
```

- `backfill_onprem.py` 는 이미지 빌드 때 구운 `build_ontology`/`build_embeddings` 산출물을 읽어
  MySQL `sessions`/`knowledge_chunks`/`ontology_*` + Redis HNSW 인덱스로 적재한다.
- 멱등(중복 등록 방지 내장) — 재실행 안전. 검증은 전후 count 비교로 한다.

### 3) 메인 앱 배선 반영 후 재기동

`deploy/onprem.env` 에 `RAG_UPSTREAM=http://host.docker.internal:8100` 이 있는지 확인 후:

```bash
docker compose up -d                                    # onprem.env 오버레이 자동 적용
curl -s http://localhost:18080/api/registry/stats       # upstream=true 확인
```

- `host.docker.internal` 은 `docker-compose.yml` 의 `extra_hosts: host.docker.internal:host-gateway`
  로 **리눅스에서도 해석**된다(별도 조치 불필요). 그래도 안 잡히면 `RAG_UPSTREAM` 을 호스트
  IP(예: `http://192.168.0.95:8100`)로 바꾼다.
- `/api/registry/stats` 가 `upstream=true`(또는 rag-search 위임 stats: `backend=mysql_redis`,
  `count>0`)를 반환하면 배선 성공.

### 롤백 (프록시 → 로컬 폴백)

`RAG_UPSTREAM` 만 제거하면 메인 앱은 코드 기본값(로컬 sqlite+npy)으로 되돌아간다:

```bash
# deploy/onprem.env 에서 RAG_UPSTREAM 줄을 주석 처리하거나 삭제한 뒤
docker compose up -d
curl -s http://localhost:18080/api/registry/stats       # upstream=false 확인
```

⚠️ 로컬 폴백 모드에선 등록물이 컨테이너 `./data`(볼륨 마운트, #103) 파일에만 남고 MySQL+Redis
에는 쌓이지 않는다. 롤백은 rag-onprem 장애 시 **가용성 우선** 임시 조치로만 쓰고, 복구 후
`RAG_UPSTREAM` 을 되돌린다.

### 장애 시나리오

- **rag-search(:8100) 다운**: 메인 앱은 `agent/reuse.py` 기존 동작대로 검색을 로컬로 자동
  폴백한다(가용성 우선). 등록 write-back 은 예외를 삼키되 Langfuse `register_ok=0` +
  `register_skip_reason`(#104)으로 가시화되므로, 스택 복구 후 재백필 여부를 판단한다.
- **Redis 장애(rag-search 는 살아 있음)**: rag-search 내부에서 로컬 백엔드로 폴백.
- **재기동 후 count 리셋**: MySQL+Redis 는 named 볼륨(`mysql_data`/`redis_data`)에 영속하므로
  스택을 `down`(‑v 없이) 후 `up` 해도 데이터가 유지된다. `down -v` 는 데이터를 지우니 주의.

> 설계 상세: `docs/design/reuse-corpus-persistence.md`(영속화 배선) · `docs/design/rag-db-schema.md`(스키마·마이그레이션 S1~S6).
> ⚠️ 이 스택은 실제 MySQL/Redis 기동 환경(온프렘 서버)에서 검증하세요 — DB 연동은 서비스가 떠 있어야 동작합니다.

## 두 가지 이미지

| | 풀 (`Dockerfile.rag-search`) | 경량 (`Dockerfile.rag-demo`) |
|---|---|---|
| 검색 엔진 | 벡터+부분일치 하이브리드 | 부분일치 단독(폴백) |
| 의존성 | torch(CPU)+transformers+BGE-m3 | fastapi+uvicorn |
| 이미지 크기 | ~4–5GB | ~150MB |
| 서버 RAM | ≥4GB 권장 | ~256MB |
| 인터넷 | 빌드 시만(모델 baked → 런타임 오프라인) | 불필요 |

풀 이미지는 **빌드 시점**에 온톨로지 DB → BGE-m3 다운로드 → 청크 임베딩을 모두
구워 넣으므로, 런타임은 오프라인(에어갭)에서도 동작한다.

## 임계값 튜닝 (등록 vs 가져오기 %)

`.env` (없으면 install.sh가 `deploy/.env.example`에서 생성):

```
TAU_REUSE=0.62    # ≥ → 재사용(가져오기)
TAU_NEAR=0.48     # ≥ → 근접(검수), 미만 → 등록 후보
W_VEC=0.4         # 하이브리드 가중치(청크 벡터)
W_LEX=0.15        # 하이브리드 가중치(어휘)
W_CONCEPT=0.45    # 하이브리드 가중치(개념 centroid — 패러프레이즈 정확도 핵심)
```

> 검색 매칭 품질: 개념 centroid(개념별 청크 임베딩 평균) 신호를 blend해 패러프레이즈에
> 강건하다. gold 8건 primary_concept 정확도 6/8(부분일치 0/8·청크 top1 대비 향상).
> `PYTHONPATH=scripts python scripts/eval_search.py`로 재현·계측.

- **재사용률↑ (신규 생성 비용↓)**: `TAU_REUSE`·`TAU_NEAR` ↓
- **품질↑ (오매칭↓)**: 임계값 ↑
- 변경 후: `docker compose -f docker-compose.rag-search.yml up -d` (재빌드 불필요, env만 재적용)

커버리지 탭 또는 `GET /api/coverage`로 조정 결과의 재사용/등록 비율을 즉시 확인.

## 엔드포인트

| | |
|---|---|
| `GET /api/search?q=&coding_type=&top=&user_id=` | 하이브리드 검색 → 결정 태그 붙은 청크 랭킹. `user_id` 지정 시 base(전역)+내 등록물로 한정 |
| `POST /api/register` | 결과물 등록 → 저장+임베딩 → 즉시 검색 반영(RAG 되먹임). body: `{question,title,content,coding_type,concept_key,user_id,session_id}` |
| `GET /api/query?question=&grade=&coding_type=` | 페르소나 도출(개념·선수학습·학습노트) |
| `GET /api/coverage?coding_type=` | 질문 묶음 재사용/근접/등록 비율(%) |
| `GET /health` | 상태 + `vector_enabled` + `registered`(등록물 수) |

### RAG 되먹임(등록) 루프

검색이 `register`(콜드셀)로 판정한 질문의 결과물을 `POST /api/register` 하면,
그 즉시 임베딩되어 다음 검색부터 재사용 히트로 잡힌다. 저장 위치:

```
data/registered.jsonl     append-only 등록 행(질문·제목·내용·user_id·session_id·ts)
data/registered_emb.npy   행별 임베딩(정합)
```

이 스토어는 `build_ontology.py`가 `ontology.db`를 재생성해도 **건드리지 않으므로**
등록물이 유실되지 않는다. compose가 `./data`를 볼륨 마운트하므로 컨테이너 재시작에도 영속.
`user_id`(uuid)로 등록하면 검색에서 `user_id` 필터로 '내 것'만 좁혀볼 수 있다(기본은 전체 통합 재사용).

## 데이터 갱신

`projects/`(세션 데이터)가 바뀌면 그래프·임베딩을 재생성해야 한다:

```bash
# 호스트에 python 환경이 있으면
PYTHONPATH=scripts python scripts/build_ontology.py
PYTHONPATH=scripts python scripts/build_embeddings.py --force
# 또는 이미지 재빌드
docker compose -f docker-compose.rag-search.yml up --build -d
```

## 운영 이관 (참고)

현재는 SQLite + 로컬 `.npy`로 단일 컨테이너 데모. 운영 전환 시:
- 벡터 저장: `.npy` → Redis Stack(HNSW) 또는 Aurora MySQL 8.0 벡터
- 임베딩 서버 분리(다중 워커 공유), 질문 임베딩 캐시(현재 `embed_bge.embed_one` LRU)
- 이슈 #27 P2/P3 참고.
