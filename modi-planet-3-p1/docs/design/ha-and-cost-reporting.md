# 이중화 · LLM 모드 전환 · 비용 리포트 (2026-08-21)

40명 실시간 수업(2026-08-22)을 앞두고 하루 동안 진행한 인프라·관측 개편 기록이다.
**모든 수치는 프로덕션 실측**이고, 틀렸던 판단도 그대로 남겼다 — 같은 함정을 다시
밟지 않는 게 이 문서의 목적이다.

## 1. 출발점

동접 40 부하를 넣자 서비스가 무너졌다.

| 지표 | 개편 전 (CLI · 단일 컨테이너) |
|---|---|
| 성공률 | **0%** (40/40 절단) |
| 총 소요 p50 | 298.0s |
| `/health` | **19회 무응답(약 4분)** |
| load (16코어) | 161.97 — SSH 조차 불가 |

원인은 하나가 아니라 셋이 겹쳐 있었다.

1. **LLM 호출마다 node 서브프로세스** — CLI 모드는 호출당 `claude` 프로세스를 띄운다.
   동접 40이면 프로세스 40개가 같은 16코어를 물고 늘어진다.
2. **공유 구독 1계정** — 전 사용자가 같은 계정의 5시간 윈도를 태운다. 부하 테스트
   생성 턴 120여 건으로 실제 소진돼 "Claude 사용 한도에 도달했어요"가 전원에게 나갔다.
3. **Sentry 자기증폭** — 같은 박스에서 self-hosted Sentry가 돌고 있었고 traces/profiles가
   1.0이었다. 요청 40건이 트레이스 40 + 프로파일 40을 만들어 같은 코어가
   relay→kafka→snuba→clickhouse를 처리했다. `dmesg`에 증거가 남아 있다:
   `Out of memory: Killed process 58954 (sentry) anon-rss:11619828kB` (09:37:54).

> **처음 진단이 틀렸다.** "레플리카 3대 → 스레드풀 3배"로 해결된다고 봤는데,
> 병목은 스레드풀이 아니라 서브프로세스 자원 고갈이었다. 레플리카만 늘렸다면
> 같은 계정·같은 코어를 3배로 나눠 쓸 뿐 성공률은 0%에 머물렀을 것이다.

## 2. 개편 결과

| 지표 | 개편 전 | 개편 후 (API · 3레플리카) |
|---|---|---|
| 성공률 | 0% | **100%** (40/40) |
| 첫 토큰 p50 | 126.5s | 25.7s |
| 총 소요 p50 | 298.0s | **72.0s** |
| `/health` | 19회 무응답 | 35/35 정상, 전부 1초 미만 |
| 생성 턴 비용 | $0.1286 | **$0.0290** (프롬프트 캐시 실효) |

## 3. 이중화 설계

```
NPM(.102) → edu-nginx(:18080) → edu-agent-{1,2,3}:8000
                                  ├── ./projects  (공유 볼륨)
                                  └── redis       (분산 세션 락)
```

**왜 `replicas:`가 아니라 명시 서비스 3개인가**
`container_name`이 곧 컴포즈 네트워크의 안정적 호스트명이라 nginx upstream에 그대로
적을 수 있다(DNS 다중 A레코드 해석 타이밍에 의존하지 않음). 서비스별 헬스체크와
1→2→3 순차 롤링 교체도 가능해진다.

**왜 `user_id` sticky인가**
`session_id`는 POST 본문에 있어 nginx가 못 읽는다. `user_id`는 쿼리 파라미터라 읽힌다.
없으면 `X-User-Id` 헤더 → `$remote_addr` 순으로 폴백한다.

**`proxy_next_upstream`에 `non_idempotent`를 넣지 않은 이유**
`/chat`은 POST이고 LLM 과금이 붙는다. 응답이 시작된 뒤 재시도하면 **같은 턴을 두 번
과금**한다. 연결 자체가 실패한 경우(`error timeout http_502/503/504`)만 넘긴다.

### 세션 연속성

3대가 `./projects`를 공유하고, `agent/session_store.py`의 `should_reload(mtime)`가
"다른 레플리카가 더 최신을 저장했으면 재로딩"을 처리한다.

> **⚠️ 이 설계는 '같은 박스' 전제 위에 있다.** 레플리카를 다른 서버로 흩으면 파일
> mtime 비교가 성립하지 않아 stale 세션을 읽는다.

턴 도중 죽으면 `finally`의 `auto_save`가 실행되지 않아 그 턴 산출물이 통째로 유실된다.
그래서 산출물 확정 신호(`blockly_ready`/`code_validated`)에서 즉시 디스크에 확정하는
중간 체크포인트를 넣었다(#181). **재개가 아니라 보존**이다 — 학생은 복원된 코드를
보고 이어가야 한다.

## 4. 하루 동안 밟은 함정

문서로 남길 가치가 있는 건 성공보다 이쪽이다.

### ① Docker 단일 **파일** 바인드 마운트는 inode를 묶는다

nginx 설정을 고쳐 배포했는데 컨테이너 안은 옛 내용 그대로였다. `rsync`가 파일을
원자적으로 교체하면서 **새 inode**를 만들었고, 마운트는 옛 inode를 계속 보고 있었다.
→ **디렉터리 마운트로 변경**하고, 배포에 `nginx -T` 파싱 검증을 넣어 upstream이 실제로
3줄이 아니면 배포를 실패시킨다.

### ② shell 형식 `CMD` → PID 1이 `sh`

```dockerfile
CMD uvicorn ...          # PID 1 = sh, uvicorn 은 그 자식
CMD exec uvicorn ...     # uvicorn 이 sh 를 대체해 PID 1
```

`exec`가 없으면 `docker stop`의 SIGTERM이 `sh`에서 멈춘다. 즉 **`stop_grace_period: 180s`를
준 의미가 통째로 사라진다**(진행 중 SSE를 드레인 못 하고 SIGKILL).

### ③ `docker exec ... kill -9 1`은 크래시 테스트가 아니다

리눅스는 PID 네임스페이스 init(PID 1)에게 **같은 네임스페이스 안에서** 보낸 SIGKILL을
커널이 폐기한다. 이 명령으로는 컨테이너를 죽일 수 없고, 결과는 항상
`restarts=0 status=running`이다.

> 이 결과를 보고 "②가 원인"이라고 진단했는데 **틀렸다.** ②는 실재하는 별개 결함이지만,
> 크래시 테스트가 0을 준 이유는 아니었다. 호스트에서 실제 PID를 죽이니 정상 동작했다:
> `before=0 → after=1 status=running`, 그동안 `/health`는 200.

```bash
sudo kill -9 $(sudo docker inspect -f '{{.State.Pid}}' edu-agent-2)   # 올바른 방법
```

### ④ `schema.sql`은 최초 init에서만 적용된다

`docker-entrypoint-initdb.d`는 **빈 데이터 디렉터리로 처음 뜰 때만** 실행된다. #133에서
추가한 `usage_turns`가 운영 DB에 영원히 생기지 않았고, 사용량 적재가 fail-open으로
조용히 실패해 **비용 데이터가 0건**이었다. 진단 함수(`counts()`)에 이 테이블을 추가한
순간에야 드러났다.

→ 배포에 `apply_schema.py` 실행 스텝을 넣었다(전 문이 `IF NOT EXISTS`라 멱등).
**새 테이블을 추가하면 반드시 `deploy/schema.sql`에 넣어야 한다.**

### ⑤ MySQL `SUM()`은 BIGINT도 DECIMAL로 준다

원장이 살아나자마자 리포트가 터졌다.

```
{"ok": false, "error": "unsupported operand type(s) for *: 'decimal.Decimal' and 'float'"}
```

총계는 `int()`로 감쌌지만 행 단위 집계는 raw를 그대로 넘겨서, **표가 한 줄이라도 있으면**
전체가 실패했다. 기존 테스트가 못 잡은 이유는 가짜 커서에 int를 넣었기 때문이다.
→ `_num()`으로 통일하고, 실제 드라이버 타입으로 도는 회귀 테스트를 추가했다.

## 5. API 인증 실패 시 CLI 폴백

API 모드로 전환하면 새 단일 실패점이 생긴다 — 키 만료·크레딧 소진이면 **전 서비스가
멈춘다.** CLI 구독 경로는 그대로 살아 있으므로 자동으로 넘긴다(#179).

```
① 키가 비었거나 공백           → 네트워크 왕복 없이 즉시 CLI
② 호출 중 401/403/크레딧 소진   → 같은 호출을 CLI 로 재시도
③ 실패 래치 + 쿨다운(600s)      → 죽은 API 를 계속 두드리지 않음. 지나면 재시도
```

**인증과 무관한 에러(레이트리밋·타임아웃·도구 실패)는 폴백하지 않는다.** 진짜 버그를
CLI로 숨기면 원인을 영영 못 찾는다. 실제 무효 키로 401을 유발해 검증했다.

## 6. 비용 리포트

`weighted_tokens = input×1 + output×5 + cache_read×0.1 + cache_creation×1.25`.
이 비율이 Haiku 4.5 단가와 정확히 일치하므로 **`weighted / 1e6 = USD`**가 성립한다.

**비용 구성 실측(Langfuse)**: `output×5 = 77.3%`, `cache_creation×1.25 = 21.7%`,
`input = 0.9%`, `cache_read = 0.1%`.

> **"비용의 80%가 캐시 안 된 입력"이라던 초기 판단은 틀렸다.** 지배적인 건 출력이다.
> 따라서 절감 레버는 입력 축소가 아니라 **출력 축소(후처리 게이팅)와 재사용**이다.

읽는 경로는 셋이고 **렌더러는 하나**다 — 라이브 페이지와 밤 스냅샷이 갈라지면
"웹에서 본 값"과 "보관된 값"이 달라져 청구 근거가 무너진다.

| 경로 | 용도 |
|---|---|
| `GET /report?token=…` | 웹 페이지. 열 때마다 재집계 |
| `data/reports/YYYY-MM-DD.html` | 밤 크론(00:10 KST) 확정본. **배포 rsync 제외 대상** |
| `scripts/usage_report.py` | 터미널 표 |

접근 통제는 **fail-closed**다. `REPORT_TOKEN`이 없으면 라우트가 404다 — 학생이 쓰는
것과 같은 도메인이고 내용은 사용자별 비용이라, "설정을 깜빡해서 공개"가 기본이 되면
안 된다. 토큰이 틀려도 403이 아니라 404를 준다(403은 존재를 알려 준다).

## 7. 남은 위험

| 항목 | 내용 |
|---|---|
| **크로스 유저 세션 노출** | 세션이 `session_id`만으로 식별된다. 같은 값을 쓰면 남의 대화가 보인다(프로덕션 재현 완료). 별도 수정 필요 |
| **healthcheck 미재시작** | Docker healthcheck는 `unhealthy` 표시만 한다. 살아 있으나 응답 못 하는 레플리카는 방치된다 |
| **RAG 재사용 게이트 미발동** | `/api/search`는 `reuse @ 0.727`을 주는데 실트래픽 `/chat`은 `decision: none, top1: 0.0`. 데이터가 아니라 **배선** 문제. 비용의 77%를 좌우하는 레버가 잠겨 있다 |
| **캐시 쓰기만 하고 못 읽음** | `cache_creation` 102,822 vs `cache_read` 5,985 |
| **과거 사용량 복구 불가** | ④ 때문에 #133 이후 데이터가 없다. Langfuse에만 남아 있다 |
| **배포 소요 증가** | `stop_grace_period 180s` × 3대 → 최대 9분. 긴급 시 `--timeout 10` |

## 관련 문서

- [`token-quota-and-error-structure.md`](token-quota-and-error-structure.md) — 쿼터 3계층 설계
- [`concurrency-and-scaling.md`](concurrency-and-scaling.md) — 동시성 가드·재시도
- [`chat-error-surfacing-and-usage-turns-fix.md`](chat-error-surfacing-and-usage-turns-fix.md) — `usage_turns` 초기 이슈
- [`../observability-sentry.md`](../observability-sentry.md) — Sentry 연동
