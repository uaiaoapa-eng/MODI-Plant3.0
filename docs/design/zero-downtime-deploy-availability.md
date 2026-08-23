# 배포·장애 시 클라이언트 에러 제거 (무중단 배포 + 프록시 리트라이) 설계문서

> 작성: claude-opus-4-8 + walter | 날짜: 2026-07-14 | 상태: Draft
> 규칙: 이 문서를 쓰기 **전에** 관련 코드를 실제로 읽고 검증할 것.

## 1. 배경과 목표

배포 중이거나 서버가 죽었을 때 클라이언트(외부 챗 프론트)에 "서버가 정상 운영되지 않는다"는 에러가 뜬다. 원인은 **단일 컨테이너를 배포 때마다 먼저 죽이고(SIGKILL) → 그 다음에 몇 분짜리 이미지 빌드를 하는** 배포 절차라, 빌드가 끝날 때까지 앞단 프록시(NPM, .102)가 연결 거부/502를 받기 때문이다. 서버 크래시 시에도 재시작 창(start_period 20s) 동안 같은 증상이 난다.

이 변경 후:
- **배포 시**: 새 이미지를 먼저 빌드해 두고(구 컨테이너는 계속 서빙) 헬스 통과를 확인한 뒤에만 교체 → 다운타임이 "빌드 전체(분)" → "컨테이너 재생성 후 헬스까지(초)"로 축소.
- **교체 순간의 초 단위 창 + 크래시 재시작 창**: .95 로컬에 경량 리버스 프록시(nginx)를 앞단에 두고 백엔드 무응답 시 리트라이 + 깔끔한 점검 페이지를 반환 → 클라이언트에 raw 연결에러가 노출되지 않음.
- **진행 중 SSE 스트림**: graceful shutdown 으로 드레인 → 배포가 활성 채팅을 강제 절단하지 않음.

## 2. 현재 상태 (검증됨)

| 확인한 사실 | 근거 (파일:라인) |
|---|---|
| 배포는 구 컨테이너를 **먼저 강제 삭제(SIGKILL)** 후 이미지를 빌드·기동 → 빌드 시간 내내 컨테이너 부재 | `.github/workflows/deploy.yml:97-98` (`docker rm -f edu-agent` → `docker compose up -d --build`) |
| 이미지 빌드는 다분(npm claude-code 전역설치·pip 설치·build_template npm install 포함) | `Dockerfile:17,24,36` |
| 메인 스택은 **단일 컨테이너·단일 레플리카**, `restart: unless-stopped` | `docker-compose.yml:15-21` |
| 컨테이너 헬스체크 `/health` 존재(LLM 미소모), start_period 20s | `docker-compose.yml:72-77`, `server.py:504-511` |
| 배포 헬스체크는 `up -d` **이후** curl 루프 — 이미 구 컨테이너는 삭제된 뒤라 그 사이는 다운 | `.github/workflows/deploy.yml:100-116` |
| `stop_grace_period`/graceful-stop 설정 없음. `docker rm -f`는 SIGKILL이라 진행 중 SSE 즉시 절단 | `docker-compose.yml` 전체(해당 키 부재), `.github/workflows/deploy.yml:97` |
| **세션 상태는 디스크(`projects/<uid>.json`)에서 요청 시 재수화**됨. 캐시 미스/stale 시 재로딩(멀티워커 mtime 검사) | `server.py:205-227` (`_restore_state_from_file`, `should_reload`, `_loaded_mtime`) |
| 매 턴 종료 시 `auto_save`로 디스크 영속화 → 완료된 턴 상태는 컨테이너 교체에도 보존 | `server.py:815-828`, 볼륨 `./projects`,`./data` (`docker-compose.yml:61-65`) |
| 분산 세션 락은 Redis 공유(`REDIS_URL`) — 멀티레플리카에서도 락 안전 | `docker-compose.yml:38-39,79-93` |
| `restart: unless-stopped` = 크래시 자동 재시작. 단 재시작+start_period 동안은 무응답 창 | `docker-compose.yml:21,77` |
| 앞단 프록시는 NPM(.102:81) — **이 저장소에 설정 없음(버전관리 밖)**. 502/503 리트라이 여부 **미확인** | 저장소 grep 결과 nginx/proxy 설정 파일 없음 |
| 챗 프론트(에러를 렌더하는 클라이언트)는 **이 저장소에 없음** — 서버는 simulate.html·rag_demo.html만 서빙 | `server.py:310,1309-1311` (그 외 HTML 없음) |

## 3. 설계

### 3.1 변경 개요

3계층으로 나눠 다운타임/에러 노출을 제거한다. 순서대로 효과가 크고 위험이 낮다.

```
[Phase 1] 무중단에 가까운 배포 (deploy.yml + compose)
  - build 를 먼저(구 컨테이너 서빙 유지) → 헬스 게이트(--wait)로 교체
  - docker rm -f 프리킬 제거, stop_grace_period 로 graceful drain
  → 다운타임: 빌드 전체(분) → 재생성~헬스(초)

[Phase 2] 로컬 리버스 프록시 리트라이 (신규 nginx 서비스)
  NPM(.102) → edu-nginx(.95, 신규) → edu-agent
  - proxy_next_upstream 리트라이 + 502/503 커스텀 점검 페이지
  → 교체 초단위 창 + 크래시 재시작 창을 클라이언트에 raw 에러로 노출 안 함

[Phase 3] 3 레플리카 + sticky (구현됨 2026-08-21 — 블루-그린 대신 채택)
  edu-agent-{1,2,3} 3 레플리카를 같은 박스·같은 ./projects 볼륨으로 띄우고
  edu-nginx 가 user_id consistent hash 로 sticky 라우팅 + 자동 페일오버
  배포는 1→2→3 순차 교체(나머지 2대가 계속 서빙) + stop_grace_period 180s 드레인
  → 배포·크래시 어느 쪽도 무응답 창 0, 진행 중 턴도 드레인으로 보존
```

### Phase 3 채택 결정 (2026-08-21)

블루-그린(2색 교체) 대신 **3 레플리카 + sticky** 를 택했다.

| 항목 | 블루-그린 | 3 레플리카 + sticky (채택) |
|---|---|---|
| 배포 중 용량 | 50% (한 색 내림) | 67% (1/3만 교체) |
| 상시 가용성 | 1대 죽으면 50% | 1대 죽으면 67% |
| 세션 이동 | 컷오버마다 전량 이동 | consistent hash — 죽은 1대의 키만 이동 |
| 부하 분산 | 색 단위 | user 단위 |

결정적 근거는 **실측**이다. 동접 40 생성 턴에서 성공률 0%, `/health` 조차 약 4분
무응답이었고 원인은 단일 워커의 anyio 스레드풀(기본 40) 고갈이었다. 레플리카를 3으로
늘리면 스레드풀이 3배가 되고 40 동접이 레플리카당 ~13 으로 쪼개진다 — 실측에서 동접
20 까지는 성공률 100% 였으므로 이 구간에 들어간다.

**sticky 키를 user_id 로 잡은 이유**: `session_id` 는 POST 본문(JSON)에 있어 nginx 가
lua 없이 읽을 수 없다. 반면 `user_id` 는 쿼리스트링(`/chat?user_id=`)이고 헤더
(`X-User-Id`)도 허용된다(`server.py` `get_user_id`). 세션 파일도
`projects/<user_id>/<session_id>.json` 로 사용자 단위로 묶이므로 sticky 키로는
session_id 보다 오히려 정확하다. 폴백은 `X-User-Id` → `$remote_addr` 순이며, IP 폴백은
한 교실이 같은 NAT IP 로 나오면 한 레플리카에 쏠리는 한계가 있어 최후 수단이다.

**검증 (실 nginx 1.29 + 스텁 3대)**
```
같은 user_id 10회        → 10/10 동일 레플리카      (sticky 성립)
서로 다른 user_id 60명    → 17 / 25 / 18            (3대 분산)
담당 레플리카 강제 종료    → 다른 레플리카로 즉시 이동  (자동 페일오버)
```

**처리량에 대한 정직한 한계**: 레플리카를 늘려도 LLM 동시성 상한은 여전히 공유 구독
1계정이다. 3 레플리카의 실효는 ① 가용성 ② 스레드풀 3배까지이고, 토큰 처리량 자체를
올리려면 API 모드 전환이 필요하다.

**전제 — 반드시 같은 박스**: 세션 연속성은 `./projects` 공유 볼륨 + `should_reload`
(mtime 비교, `agent/session_store.py:23`) 에 의존한다. 이 mtime 계약은 주석대로 "단일
박스 멀티워커는 볼륨을 공유하므로" 를 전제하므로, 레플리카를 **서로 다른 박스로 흩으면
stale 세션 서빙이 발생한다**. 멀티박스로 가려면 `_hydrate_from_upstream`(현재 '파일
부재 시에만' 발동, `server.py:203`)을 MySQL `updated_at` 비교로 바꾸는 선행 작업이
필요하다. 회귀 방지 테스트: `tests/test_session_continuity_ha.py`.

### 3.2 인터페이스 계약

**Phase 1 — deploy.yml 메인 스택 스텝 (교체)**
```yaml
# 기존: docker rm -f edu-agent  → docker compose up -d --build  (빌드 내내 다운)
# 변경: 빌드 먼저(구 컨테이너 서빙 유지) → 헬스 게이트 교체
- name: Build image (구 컨테이너는 계속 서빙)
  run: sudo -E EDU_AGENT_PORT="$EDU_AGENT_PORT" docker compose build edu-agent
- name: Swap to new (health-gated)
  run: sudo -E EDU_AGENT_PORT="$EDU_AGENT_PORT" docker compose up -d --no-build --wait edu-agent
# docker rm -f 프리킬 제거. 이름 충돌 정리는 --wait 실패 시 폴백에서만.
```

**Phase 1 — docker-compose.yml (edu-agent 서비스에 추가)**
```yaml
    stop_grace_period: 30s   # uvicorn SIGTERM 드레인(진행 중 SSE 완료) 여유
```

**Phase 2 — 신규 nginx 서비스 (docker-compose.yml 또는 별도 compose)**
```nginx
# deploy/nginx/edu-agent.conf
upstream edu_backend { server edu-agent:8000 max_fails=3 fail_timeout=10s; }
server {
  listen 8080;
  location / {
    proxy_pass http://edu_backend;
    proxy_next_upstream error timeout http_502 http_503 http_504;
    proxy_next_upstream_tries 3;
    proxy_connect_timeout 2s;
    # SSE(/chat) 스트리밍 보존
    proxy_buffering off; proxy_read_timeout 3600s;
    error_page 502 503 504 /maintenance.html;
  }
  location = /maintenance.html { root /usr/share/nginx/html; internal; }
}
```
> NPM(.102)의 프록시 대상을 `.95:18080`(앱 직결)에서 `.95:<nginx포트>`로 변경 필요 — **인프라 작업(walter), 코드 밖.**

**Phase 3 — 블루-그린**
```yaml
# edu-agent 를 edu-agent-blue / edu-agent-green 두 서비스로, nginx upstream 에 둘 다 등록
# deploy.yml: build → green up --wait(헬스) → nginx reload(트래픽 이동) → blue 중지
```

### 3.3 데이터 변경

없음. 세션은 이미 `./projects` 공유 볼륨에 영속·재수화되고 락은 Redis 공유라, 컨테이너 교체·멀티레플리카에서 스키마 변경 불필요.

## 4. 하지 않는 것 (Non-goals)

- **챗 프론트(클라이언트) 코드 수정** — 이 저장소에 없음. 에러 렌더/재연결 로직은 프론트 인계 범위(별도).
- **NPM(.102) 설정 자체를 이 저장소에서 관리** — 버전관리 밖. Phase 2에서 프록시 대상 변경은 인프라 작업으로만 문서화하고 자동화하지 않는다.
- **세션 상태 저장 방식 변경**(파일→DB 전면 이전 등) — 현행 재수화 구조로 충분. 건드리지 않는다.
- **RAG 온프렘 스택(edu-agent-rag)의 배포 절차 변경** — 메인 챗 서버 가용성과 분리. deploy.yml의 RAG 스텝은 그대로 둔다.
- **WEB_CONCURRENCY(워커 수)·오토스케일·쿠버네티스 도입** — 범위 밖. 단일 박스 compose 유지.
- **헬스체크를 LLM 왕복(`/health/llm?ping=1`)으로 바꾸기** — 구독 쿼터 소모. 경량 `/health` 유지.

## 5. 엣지 케이스와 결정 사항

| 상황 | 결정 |
|---|---|
| Phase 1 빌드 성공했으나 새 컨테이너 헬스 실패 | `--wait` 가 비0 종료 → 배포 job 실패로 표시. 폴백 스텝에서 로그 덤프 후 실패. (구 이미지 자동 롤백은 Phase 1 범위 밖, 로그로 수동 대응) |
| 진행 중 SSE 스트림이 30s 그레이스 안에 안 끝남 | 30s 후 SIGKILL. 클라이언트는 스트림 종료를 받고, 완료된 턴까지는 디스크 보존 → 재요청 시 재수화. |
| Phase 2 nginx 도입 전까지 NPM 직결 유지 | Phase 1만으로도 다운타임 초 단위. Phase 2는 그 초 단위 + 크래시 창을 덮는 추가 방어. 단계적 적용 가능. |
| 크래시로 앱이 20s 이상 다운 | 단일 레플리카에선 nginx 리트라이(수 초)로 못 덮음 → 점검 페이지(503) 반환(raw 연결에러 대신). 완전 무응답 0은 Phase 3(2레플리카) 필요. |
| 멀티레플리카 시 in-memory `sessions` 캐시 불일치 | 이미 mtime 기반 stale 재로딩(`should_reload`)이 있어 최신 디스크 상태로 수렴. 진행 중(미저장) 턴만 레플리카 간 비공유 — 컷오버는 색 하나씩이라 활성 턴은 해당 색에서 완료. |

## 6. 구현 이슈 분해

| # | 이슈 제목 | 의존 | 검증 명령어 |
|---|---|---|---|
| 1 | 무중단에 가까운 배포: 빌드-선행 + 헬스게이트 교체 + graceful stop | 없음 | `docker compose config -q` / `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"` |
| 2 | 로컬 리버스 프록시(nginx) 리트라이 + 점검 페이지 | #1 | `docker compose config -q` / nginx `-t` |
| 3 | 블루-그린 2레플리카 무중단 컷오버 (선택/HA) | #2 | `docker compose config -q` / deploy.yml yaml 파싱 |

- **#1 (선행 없음)**: `.github/workflows/deploy.yml` 메인 스택 스텝 재구성(빌드 먼저 → `up -d --no-build --wait`), `docker rm -f` 프리킬 제거·이름충돌 폴백만 유지, `docker-compose.yml`에 `stop_grace_period: 30s` 추가. 파일 2개, ~60줄. 위험 낮음, 효과 최대.
- **#2 (#1 후)**: 신규 nginx 서비스 + `deploy/nginx/edu-agent.conf` + `maintenance.html`, compose 에 서비스 추가, deploy.yml 에 nginx 기동 스텝. NPM 대상 변경은 문서로만(인프라 작업). 파일 3~4개, ~120줄.
- **#3 (#2 후, 선택)**: edu-agent 를 blue/green 2서비스로, nginx upstream 2백엔드, deploy.yml 색 단위 컷오버. 파일 2~3개, ~150줄. 진짜 무중단·크래시 HA가 필요할 때만.
