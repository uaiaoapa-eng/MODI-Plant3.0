# 동시성 & 확장 설계 (edu-agent)

> 상태: **설계 검토용 (코드 미반영)** · 작성일 2026-07-01 · 브랜치 `feature/concurrency-guard-and-retry`
>
> 목적: "외부에서 가끔 *서버와 연결할 수 없어요*", "여러 명 동시 요청 시 문제 없는가", "최대 몇 명까지", "Docker로 확장 가능한가"에 대한 구조적 해결안을 **단계별로** 정리한다. 이 문서가 승인되면 1단계(#1 세션 동시성 가드 + #2 재시도/백오프)부터 구현한다.

---

## 0. 요약 (TL;DR)

- 지금 구조는 **단일 uvicorn 워커 + 동기 스트림 + 로컬 Claude CLI 구독 1계정 공유**라, 동시 사용이 겹칠 때만 문제가 난다(혼자 쓰면 멀쩡).
- "연결 안 됨"의 주원인은 ① 스레드풀 포화로 신규 요청 지연, ② 구독 계정 레이트리밋(429)→`RuntimeError` 즉시 실패(재시도 없음), ③ OOM 재시작 순간.
- **1단계(이번 PR)**: 같은 세션 동시 턴 차단(#1) + CLI/스트림 재시도·백오프(#2). 둘 다 **순수 로직을 I/O에서 분리**해 단위테스트 가능하게 만든다. + 세션별 실제 LLM 응답을 검증하는 부하 스크립트 + `/health/llm` 엔드포인트.
- **다음 단계(문서로 예고)**: 캐싱(빌드 결과·프롬프트·세션), API 모드 전환, 세션 TTL/eviction, 멀티워커/스케일아웃.

---

## 1. 현재 아키텍처 — 요청 1건의 생애주기

```
[브라우저] --POST /chat (SSE)--> [FastAPI/uvicorn 워커 1개]
   server.py:83  chat()  (async 핸들러)
     └ get_orchestrator(session_id)         server.py:45  # sessions[session_id] 전역 dict
     └ StreamingResponse(event_stream())     server.py:105
          └ event_stream()  (★동기 제너레이터 → anyio 스레드풀에서 실행)
               └ orch.chat_stream(...)        orchestrator_stream.py:477  (★def, 동기)
                    └ _agent_loop_stream → _llm_call  orchestrator_stream.py:1190
                         └ self.client.messages.stream(...)
                              └ [CLI 모드] _CliStream → subprocess.Popen("claude")  claude_client.py:421
                                   (인증: 마운트된 ~/.claude 구독 1계정)  claude_client.py:237
                         └ 도구 실행: generate_code → build_check
                              └ subprocess npm/esbuild + 전역 _build_lock  builder.py:14,38,94
          └ finally: auto_save(...) → projects/<uid>/<sid>.json   server.py:99,261
```

핵심 사실(근거):

| 항목 | 사실 | 근거 |
|---|---|---|
| 워커 수 | 단일 워커(`--workers` 없음) | `Dockerfile` CMD `uvicorn server:app` |
| 스트림 | **동기** 제너레이터 → 스레드풀 1스레드를 턴 끝까지 점유 | `orchestrator_stream.py:477` |
| 세션 저장소 | 프로세스 메모리 `sessions` dict (복원만 디스크) | `server.py:34,45` |
| LLM 호출 | 기본 **CLI 구독** 방식(`USE_LOCAL_CLAUDE` 기본 true) | `claude_client.py:21` |
| 인증 | 전 사용자 **단일 `~/.claude` 구독** 공유 | `claude_client.py:237`, `docker-compose.yml` 볼륨 |
| CLI 호출 | 요청마다 `subprocess.Popen` 1개, cwd=공유 tempdir | `claude_client.py:421,270` |
| 빌드 | 전역 `threading.Lock` + 공유 `.build_cache` | `builder.py:14,16-18` |
| 메모리 | 메시지/코드 무한 누적(20턴부터 요약), 세션 자동제거 없음 | `context.py:32`, `server.py`(eviction은 삭제 API뿐) |

---

## 2. 동시성 시나리오별 문제 지점 (★ "어디를 고치나")

> 영향도: 🔴 데이터 손상/장애 · 🟠 성능/가용성 저하 · 🟡 비용/관측성

| ID | 시나리오 | 증상 | 근본 원인 (file:line) | 영향 | 수정 위치 | 이번 PR |
|---|---|---|---|---|---|---|
| **S1** | 같은 `session_id` 동시 요청(더블클릭·새 탭·프론트 중복 전송) | 응답 섞임/유실, 상태 깨짐 | 한 `StreamOrchestrator` 인스턴스의 가변 상태(`state._messages`, `generated_code_map`, `_cancelled`, `_step_count`)에 **락 없음** | 🔴 | **server.py /chat** 에 세션 동시성 가드(#1) | ✅ |
| **S2** | 다른 `session_id` 동시 요청(여러 사용자) | 느려짐/일부 실패 | 동기 스트림이 스레드풀 점유 + 단일 워커 → 스레드풀 포화 시 신규 요청 대기 | 🟠 | (1단계) 가드+재시도로 완화, (다음) 멀티워커/async | 부분 |
| **S3** | 동시 빌드(여러 사용자가 코드 생성) | 빌드 순번 대기(콜드 30~120s) | 전역 `_build_lock` + 공유 `.build_cache` 직렬화 | 🟠 | (다음) **builder.py** 빌드 결과 캐시(§6 L1) | ❌(설계만) |
| **S4** | 동시 호출이 구독 계정 한도 초과 | 429→`RuntimeError`→프론트 "연결 안 됨" | CLI가 **단일 구독 계정** 사용 + **재시도 없음** | 🔴 가용성 | **claude_client.py**(#2 재시도) + (다음) API 모드 | ✅(재시도) |
| **S5** | 세션 누적 | RAM 증가→OOM→재시작 순간 연결 불가 | `sessions` dict 자동 제거 없음, 메시지/코드 무한 누적 | 🟠 | (다음) 세션 TTL/eviction(§7) | ❌(설계만) |
| **S6** | 인증 토큰 갱신 경합 | 간헐 CLI 기동 실패 | 여러 subprocess가 `~/.claude.json`(rw) 동시 갱신 | 🟡🔴 | (다음) API 모드 전환으로 제거 | ❌(설계만) |
| **S7** | 중단(stop) 눌러도 안 멈춤 | 비용 낭비/응답 지연 | `cancel()`이 플래그뿐, in-flight subprocess/LLM 미중단 | 🟡 | (다음) subprocess kill 연동 | ❌(설계만) |

**1단계로 실제 체감 개선되는 것**: S1(데이터 손상 제거), S4(연결 끊김 대폭 감소), S2(부분 완화).
**1단계에서 "설계만" 하고 다음 PR로 미루는 것**: S3·S5·S6·S7 — §6/§7에 로드맵.

---

## 3. 1단계 설계 — #1 세션 동시성 가드 + #2 재시도/백오프

### 3.0 설계 원칙 (테스트 용이성)
> I/O(서버·서브프로세스·네트워크)에서 **순수 로직을 별도 모듈로 분리**한다. 그래야 서버 기동/Claude 인증/실제 대기 없이 단위테스트가 가능하다.

신규 모듈 2개:
- `agent/concurrency.py` — `SessionLockRegistry` (인메모리 + `threading.Lock`)
- `agent/retry.py` — `is_retryable_error` / `backoff_delay` / `run_with_retry` (부수효과 없음, `sleep` 주입)

### 3.1 #1 세션 동시성 가드

**계약**: 같은 `session_id`에 대해 **한 번에 하나의 턴만** 진행. 진행 중에 같은 세션으로 또 오면 **즉시 거절**(블로킹 대기 금지 — 스레드 쌓임 방지)하고 프론트에 `error`+`done` 이벤트를 보낸다.

```python
# agent/concurrency.py (설계 스케치)
class SessionLockRegistry:
    def __init__(self): self._locks={}; self._guard=threading.Lock()
    def _lock_for(self, sid):                       # 락 dict 자체를 보호
        with self._guard: return self._locks.setdefault(sid, threading.Lock())
    def acquire(self, sid) -> bool:                 # 비차단
        return self._lock_for(sid).acquire(blocking=False)
    def release(self, sid) -> None:                 # acquire 성공한 세션만 호출
        try: self._lock_for(sid).release()
        except RuntimeError: pass                    # 방어적(중복 release)
    def is_busy(self, sid) -> bool:
        return self._lock_for(sid).locked()
```

**server.py 배선** (`server.py:83` /chat):
```python
session_locks = SessionLockRegistry()             # 모듈 전역

@app.post("/chat")
async def chat(req, user_id=Depends(get_user_id)):
    orch = get_orchestrator(req.session_id, user_id)
    if user_id: orch._user_id = user_id
    if not session_locks.acquire(req.session_id):  # ★ 이미 처리 중
        return _sse_once({"type":"error","message":"이 세션의 이전 요청을 처리 중이에요. 잠시 후 다시 시도해주세요."})
    def event_stream():
        try:
            for event in orch.chat_stream(...): yield ...
        finally:
            auto_save(req.session_id, orch)
            if _langfuse_enabled: get_client().flush()
            session_locks.release(req.session_id)  # ★ 짝 맞춰 해제
    return StreamingResponse(event_stream(), ...)
```

**엣지 포인트(테스트로 고정)**:
- `acquire`는 async 핸들러 스레드, `release`는 스트리밍 제너레이터 스레드 → **다른 스레드에서 release 가능해야 함**. `threading.Lock`은 소유자에 안 묶여 OK(`RLock`이면 불가 → Lock 사용).
- `delete_project`(`server.py:328`)에서 세션 제거 시 락도 같이 정리(누수 방지) — 선택.
- 거절 이벤트는 기존 컨벤션(`type: error` / `type: done`, `main.py` 핸들러 참고)에 맞춤.

### 3.2 #2 재시도/백오프

**문제**: CLI subprocess가 429/529/timeout/네트워크로 0이 아닌 코드로 죽으면 `RuntimeError`(`claude_client.py:295,303,491,501`)가 그대로 올라가 "연결 안 됨"으로 보임. **재시도 로직 없음**.
**참고**: SDK 스트림 경로는 이미 `_llm_call`에 재시도가 있으나(`orchestrator_stream.py:1210-1275`) **"529/Overloaded"만** 일시적으로 인정 → CLI의 일반 `RuntimeError`(429 등)는 재시도되지 않음. 이 분류를 **공용 분류기로 통일**한다.

```python
# agent/retry.py (설계 스케치)
RETRYABLE = ("rate limit","rate_limit","ratelimit","too many requests","429",
             "overloaded","529","timeout","timed out",
             "connection refused","connection reset","connection error",
             "econnreset","broken pipe","502","503","504","temporarily","try again")
NON_RETRYABLE = ("no such file","command not found","not found in path",
                 "authentication","unauthorized","401","403","invalid api key")
def is_retryable_error(msg:str)->bool:
    low=(msg or "").lower()
    if any(m in low for m in NON_RETRYABLE): return False   # 영구 실패 우선
    return any(m in low for m in RETRYABLE)
def backoff_delay(attempt,*,base=2.0,factor=2.0,cap=30.0)->float:
    return min(cap, base*(factor**max(0,attempt)))          # 2→4→8 … cap 30
def run_with_retry(fn,*,max_attempts=3,sleep=time.sleep,on_retry=None,is_retryable=is_retryable_error):
    for attempt in range(max_attempts):
        try: return fn()
        except Exception as e:
            if attempt>=max_attempts-1 or not is_retryable(str(e)): raise
            d=backoff_delay(attempt)
            if on_retry: on_retry(attempt,d,e)
            sleep(d)
```
> 주의: 마커는 `"rate limit"`처럼 **구문 단위**로 둔다. `"rate"` 단독은 `gene`**`rate`**`_code` 같은 평문을 오탐하므로 금지.

**적용 지점**:
1. `claude_client._call_cli`(비스트리밍·서브에이전트용, `claude_client.py:279`) → 본문을 `run_with_retry`로 감싼다. `subprocess.TimeoutExpired`("timed out")=재시도, `FileNotFoundError`("no such file")=즉시 포기.
2. `orchestrator_stream._llm_call`(`:1221`, `:1259`) → 임시 키워드 판정을 `is_retryable_error`로 교체, 대기시간은 `backoff_delay`로. CLI 스트림 실패가 **스트림 재생성**(`:1267`, CLI는 새 `_CliStream`) 경로로 재시도됨.

**스트림 재시도 안전성**: 토큰이 이미 방출된 뒤 재시도하면 중복 위험. 대부분의 연결/429 실패는 **첫 토큰 이전**에 발생 → 안전. (토큰 방출 이후 중복 방지는 다음 단계에서 "produced 플래그" 가드로 강화 — §7.)

### 3.3 스코프 컷 (이번 PR에서 **안 하는 것**)
- in-flight subprocess/LLM 강제 중단(S7), 멀티워커/async 재작성(S2 근본), 빌드 캐시(S3), 세션 TTL(S5), API 모드 전환(S4/S6 근본) → 모두 **다음 PR**. 본 PR은 "데이터 손상 제거 + 연결 끊김 완화 + 검증 수단 확보"에 집중.

---

## 4. 테스트 설계

### 4.1 단위 테스트 (순수 로직, 서버/인증 불필요) — `pytest`
`tests/test_concurrency.py`
- 같은 세션 2번째 `acquire` → `False`, `release` 후 다시 `True`
- 다른 세션은 서로 안 막음
- `is_busy` 상태 전이
- **다른 스레드에서 release** 후 재획득 가능 (엣지)
- 중복 release 안전(예외 없음)
- 20스레드 동시 `acquire("s")` → **정확히 1개만 성공** (`threading.Barrier`)

`tests/test_retry.py`
- `is_retryable_error`: 429/529/overloaded/timeout/connection/503 → True
- 영구 실패(no such file/401/auth/invalid api key) → False
- **오탐 방지**: `"...generate_code..."` → False (`rate` 부분문자열)
- `backoff_delay`: 2→4→8, cap 30, 음수 방어
- `run_with_retry`: 2회 실패 후 성공(=sleep 2회), max 도달 후 raise(호출 3회), 영구 실패 즉시 raise(호출 1회), `on_retry` 콜백 호출

### 4.2 대량/동시 부하 스크립트 — `scripts/load_test.py` (세션별 실제 LLM 응답 검증)
> Q2 요구: **세션별로 실제 LLM 응답(토큰 수신·완료)을 검증**. 표준 라이브러리만 사용(추가 의존 없음). 스레드/멀티프로세스 둘 다 지원("개별 프로세스로 제대로 동작" 검증).

CLI:
```
python scripts/load_test.py --url http://HOST:18080 \
    --sessions 20 --turns 3 --mode distinct --concurrency 20 [--proc]
```
모드:
- `distinct` — N개 **서로 다른** session_id 동시 → 다중 사용자 처리량/성공률
- `same-session` — 같은 session_id로 2개 동시 → **#1 가드 검증**(정확히 1개 진행, 1개 busy `error`)
- `sustained` — 각 세션이 `--turns`회 **이어서** 대화 → 세션 상태 유지 + 연속 LLM 동작 확인

세션별 판정(성공 조건):
- HTTP 200 + SSE에서 **`type:token` 1회 이상 수신** + 종료 **`type:done`** 도달, `type:error` 없음
- 실패 분류: `connection_refused` / `non_200` / `timeout` / `error_event(429·overload 등)` / `no_tokens`

집계 리포트:
```
sessions=20 turns=3  mode=distinct  proc=False
✅ success: 18/20 (90%)   busy-guard hits: 0
❌ fail: 2  {timeout:1, error_event:1}
latency first-token  p50=1.8s p95=6.2s
latency total        p50=14s  p95=41s
```
`--proc` 시 `multiprocessing`으로 세션을 **독립 프로세스**로 띄워 GIL/스레드풀 영향 없이 순수 동시성 확인.

> 이 스크립트는 라이브 서버 + Claude 인증이 필요하므로 CI가 아니라 **운영/스테이징에서 수동 실행**. 합격 기준은 §4.4.

### 4.3 세션별 LLM 헬스체크 엔드포인트 (Q2 — "둘 다")
서버에 경량 진단 엔드포인트 추가(부하 스크립트가 활용):
- `GET /health/llm` → `{ok, latency_ms, mode: "cli"|"api"}` : 짧은 핑 프롬프트로 LLM 왕복 확인
- `GET /health/llm?session_id=s1` → 해당 세션이 메모리에 살아있는지 + 가벼운 동작 여부
- 비용 최소화를 위해 최소 토큰/타임아웃 + 결과 캐시(짧은 TTL). 헬스체크가 구독 한도를 갉아먹지 않도록 호출 간격 제한.

### 4.4 합격 기준 (acceptance)
- 단위테스트 전부 green (`pytest -q`).
- `same-session` 부하: 동시 2요청 중 **정확히 1개만** 진행, 나머지는 busy `error` (데이터 손상 0).
- `distinct` 부하(동시 10): 성공률 ≥ 95%, "연결 안 됨"류(connection/timeout/error_event) 재시도 후 잔존 ≤ 5%.
- `sustained`(세션당 3턴): 세션 상태 유지로 모든 턴 LLM 응답 수신.

---

## 5. 최대 동시 인원 (현재 vs 1단계 후)

| 구간 | 현재 | 1단계 후 |
|---|---|---|
| 같은 세션 동시 | 🔴 상태 깨짐 | ✅ 1개만 진행(안전) |
| 다른 세션 동시(활성) | 실질 3~10명(구독 한도·subprocess 메모리·빌드락) | 재시도로 가용성↑, 상한은 비슷(근본은 다음 단계) |
| 한계 요인 | 구독 1계정 한도 > subprocess 메모리 > 빌드락 | 동일 — **§7에서 API 모드로 상향** |

> 결론: 1단계는 "안전성 + 끊김 완화"가 목표. **동시 인원 상한 자체를 크게 늘리려면 §6 캐싱 + §7 API 모드/스케일아웃이 필요**.

---

## 6. 캐싱 전략 로드맵 (다음 단계)

| 레벨 | 무엇 | 효과 | 적용 위치 |
|---|---|---|---|
| **L1 빌드 결과 캐시** | `code_map` 내용 해시 → 빌드 통과/오류 결과 캐시. 동일 코드면 npm/esbuild 스킵 | S3 전역 빌드락 경합·콜드빌드 대폭 감소 | `builder.py` (`build_check` 앞단, content-hash 키) |
| **L2 프롬프트/응답 캐시** | Anthropic **prompt caching**(시스템·도구·문맥 캐시). 이미 `usage.cache_read`로 추적 중(`usage.py`) | 토큰 비용·지연 감소 → 동시 처리량↑ | `prompts`/호출부에 `cache_control` |
| **L3 세션 상태 캐시·영속** | 메모리 `sessions`를 디스크 영속(이미 부분) + **TTL/LRU eviction**, 추후 Redis | S5 OOM 방지, 멀티워커/스케일아웃 전제 | `server.py` 세션 계층 |

> L1은 S3를 직접 푸는 가장 가성비 좋은 캐싱이라 캐싱 항목 중 **다음 PR 1순위 후보**. content-hash 캐시 패턴(경로 독립·자동 무효화) 적용.

---

## 7. 근본 확장 로드맵 (다음 단계, 요약)

1. **LLM API 모드 전환** (`USE_LOCAL_CLAUDE=false` + `ANTHROPIC_API_KEY`): 구독 단일계정 한도·`~/.claude.json` 경합(S4·S6) 제거, 표준 레이트리밋 티어로 동시성 상향. *코드 경로는 이미 존재*(`create_client`).
2. **세션 TTL/eviction**(S5): 비활성 N시간/턴 초과 시 메모리에서 제거.
3. **stop 실효화**(S7): `cancel()` → in-flight subprocess `kill` 연동, 스트림 재시도 시 produced-가드.
4. **멀티워커/스케일아웃**: async 전환 또는 워커 다중화. 단, 세션이 프로세스 메모리에 있으므로 **세션 상태 외부화(Redis) 또는 sticky routing 선행 필수**. Docker `--scale`는 `container_name` 고정·단일 포트·공유 인증 때문에 현재 그대로는 불가 → 리버스 프록시 + 상태 외부화 후 가능.

---

## 8. 단계별 실행 계획 (PR 분할)

- **PR1 (완료)**: `agent/concurrency.py`, `agent/retry.py`(+단위테스트), `server.py` 가드 배선 + `/health/llm`, `claude_client.py`/`orchestrator_stream.py` 재시도 통합, `scripts/load_test.py`, 본 문서. → S1 해결, S4 완화, 검증수단 확보.
- **PR2 (진행)**: 세션 TTL/eviction(S5) + 캐싱 3종(세션 상태/프롬프트/조회결과). **S3(빌드 캐시) 제외**(현재 빌드 동시성은 문제로 보지 않음). 내부 서버 우선 + 클라우드 seam(§9).
- **PR3 (진행)**: **stop 실효화(S7)** — cancel()이 진행 중인 claude 서브프로세스를 직접 종료.
  API 모드 전환(S4/S6)은 사용자 요청에 따라 **보류**(현재 CLI 구독 유지, 한계 측정 후 결정).
- **PR4 (진행)**: 내부 docker-compose 에 Redis 추가 → **분산 세션 락(멀티워커 S1)** + **mtime 캐시 무효화**(공유 볼륨 stale 방지) + 멀티워커(`WEB_CONCURRENCY`). Redis **상태 저장**은 디스크 미공유인 클라우드 멀티노드용이라 후속.

### PR4 상세 — 내부 멀티워커 안전화 (Redis)
- **왜 Redis 인가**: 단일 박스라 상태는 `projects/` 볼륨으로 이미 공유됨. 멀티워커에서 깨지는 건 ① 워커 간 세션 락 ② 워커별 인메모리 캐시 stale. → ①은 **Redis 분산 락**, ②는 **mtime 무효화**로 해결.
- **분산 락**: `RedisSessionLock`(SET NX PX + 토큰 비교 GET/DEL). `make_session_lock(REDIS_URL)` 가 URL 있으면 Redis, 없으면 인메모리. TTL(기본 900s>최대 턴) 로 워커 사망 시 자동 해제.
- **캐시 무효화**: `should_reload(cached_mtime, disk_mtime)` — 다른 워커가 더 최신 상태를 디스크에 저장했으면 캐시 버리고 재로딩. `get_orchestrator`/`auto_save`/`restore` 가 `_loaded_mtime` 추적.
- **compose**: `redis:7-alpine` 서비스 추가(**호스트 포트 미발행 — 내부 네트워크 전용, 충돌 없음**), `REDIS_URL`/`WEB_CONCURRENCY` env, `depends_on: redis(healthy)`. edu-agent 호스트 포트는 기존 그대로(`EDU_AGENT_PORT:-18080`).
- **현실 한계**: 워커를 늘려도 LLM 동시성 상한은 **여전히 구독 1계정 한도**. 멀티워커의 실효는 API 모드 전환(보류) 이후 큼. PR4 는 "그때 바로 스케일되도록 안전한 토대"를 까는 것.

### Redis 상태 저장(후속, 클라우드 트랙)
- 클라우드 멀티노드는 디스크 볼륨을 공유 못 하므로 세션 상태(JSON)도 Redis 로. `_write_session_json`/`list`/`load`/`delete` 를 `SessionStateStore`(Disk|Redis) 뒤로 빼는 리팩터가 필요 → 별도 PR.

---

## 9. 내부 서버 vs 클라우드 전략 (PR2 반영)

두 배포 트랙은 같은 코드에서 **저장/캐시 계층의 구현만 다르게** 가져간다. PR2 는
내부 서버용 인메모리 구현을 넣되, 인터페이스를 분리해 클라우드 전환 시 어댑터만 교체한다.

| 축 | 내부 서버(현재) | 클라우드(후속) | PR2 seam |
|---|---|---|---|
| 세션 저장 | `InMemorySessionStore`(TTL+LRU) | `RedisSessionStore` | `agent/session_store.py` 인터페이스 |
| 조회/응답 캐시 | `TTLCache`(인프로세스) | 분산 캐시(Redis) | `agent/cache.py` |
| 프롬프트 캐시 | (CLI는 CLI가 처리) | API `cache_control` 활성 | `agent/prompt_cache.py` (API 모드에서만 적용) |
| LLM 인증 | CLI 구독 1계정(유지) | API 키 + 티어 | `claude_client.create_client` 분기(기존) |
| 스케일 | 단일 워커(세션 메모리 공유 안정) | replica + LB + sticky/외부세션 | 세션 외부화 후 가능 |

**구현 원칙**: `server.py` 는 `sessions` 를 `InMemorySessionStore` 로만 다룬다(dict 유사
인터페이스). 클라우드에서는 동일 시그니처의 Redis 구현으로 바꾸면 끝 — 라우트 코드 무변경.

### PR2 캐싱 적용 요약
- **세션 상태(S5)**: 메모리 세션을 TTL(기본 24h)+LRU(기본 500)로 관리 → OOM/재시작 방지.
  디스크 영속(`projects/`)은 그대로라 evict 돼도 다음 접근 시 복원된다.
- **프롬프트/컨텍스트**: API 모드에서 system/tools 에 `cache_control` 부착(반복 호출 토큰 절감).
  CLI 구독 모드(현재)는 무변경 — 위험 없음, API 전환(PR3) 시 자동 활성.
- **조회 결과**: `/reference` 목록을 짧은 TTL 로 캐시(디스크 I/O 절감). `/health/llm` 핑도 TTL 캐시.
  > `/projects` 는 유저 저장/삭제로 자주 바뀌어 캐시하지 않는다(stale 위험 > 이득).

### 환경변수
- `SESSION_TTL_SECONDS`(기본 86400), `SESSION_MAX`(기본 500), `REFERENCE_CACHE_TTL`(기본 60).

---

### 검토 요청
이 설계대로 **PR1**을 구현해도 될까요? 특히 확인 부탁:
1. #1 거절 정책 = **즉시 거절(busy)** vs 짧은 대기 후 진행 — 본 설계는 *즉시 거절*.
2. `/health/llm`이 구독 토큰을 소량 소모함 — 호출 간격 제한 + 캐시로 최소화 예정. OK?
3. PR 분할(§8) 순서 동의 여부.
