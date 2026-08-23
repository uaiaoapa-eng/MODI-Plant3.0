# 교육용 바이브코딩 에이전트 (edu-agent) — 사내 서버 배포용 이미지
#
# 베이스: Python 3.11 (pyproject 요구사항: requires-python >= 3.11)
# 추가: Node.js — agent/builder.py 가 React 코드 빌드체크에 npm/esbuild 를 호출하기 때문.
#       (Node 가 없으면 빌드체크만 graceful 하게 스킵되지만, 기능 유지를 위해 포함)
FROM python:3.11-slim-bookworm

# --- Node.js 20 런타임 추가 (공식 node 이미지에서 복사) ---
# python/node 둘 다 debian bookworm 기반이라 바이너리 호환됨.
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# --- Claude Code CLI 설치 (CLI 방식: USE_LOCAL_CLAUDE=true 일 때 사용) ---
# 인증(.credentials.json)은 compose 에서 호스트 ~/.claude 를 마운트해 주입한다.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# --- 파이썬 의존성 설치 (레이어 캐시를 위해 메타데이터 먼저 복사) ---
COPY pyproject.toml ./
COPY agent ./agent
COPY curriculum ./curriculum
RUN pip install --no-cache-dir -e .

# --- 앱 소스 복사 ---
COPY server.py main.py ./
COPY web ./web
# RAG 통합 모듈(search_lib/registry_lib/rag_demo_app 등). 없으면 server.py 가
# RAG 라우트만 비활성화하고 코어는 뜨지만, 포함해야 /api/search·/rag 가 동작한다.
# (벡터는 torch 미포함이라 lexical 모드; 완전 벡터 RAG 는 docker-compose.rag-onprem.yml)
COPY scripts ./scripts
COPY build_template ./build_template
COPY docs ./docs

# --- React 빌드체크용 node_modules 미리 설치 (런타임 첫 요청 지연/오프라인 대비) ---
RUN cd build_template && npm install --prefer-offline --no-audit --no-fund || true

# 추천(예시) 프로젝트 템플릿 — /reference 엔드포인트가 런타임에 읽는다.
# (빠지면 컨테이너의 /app/reference 가 빈 폴더라 예시 프로젝트가 안 보임)
# 자주 바뀌므로 npm install 뒤에 둬서 변경 시 위 레이어 캐시를 깨지 않게 한다.
COPY reference ./reference

# 세션 저장 폴더 (compose 에서 볼륨으로 덮어씀)
RUN mkdir -p projects

ENV PYTHONUNBUFFERED=1

# 컨테이너 내부는 항상 8000 으로 고정. 외부 노출 포트는 docker-compose 에서 매핑한다.
EXPOSE 8000

# 운영 실행: --reload 는 개발 전용이라 제거.
# 워커 수는 WEB_CONCURRENCY(기본 1). 멀티워커 시 세션 락은 REDIS_URL 로 공유된다.
#
# ⚠ `exec` 가 핵심이다. CMD 를 shell 형식으로 쓰면 도커가 `/bin/sh -c "..."` 로 감싸
#   **PID 1 이 sh 가 되고 uvicorn 은 그 자식**이 된다. 그러면 두 가지가 깨진다:
#
#   ① graceful shutdown 무력화 — `docker stop` 의 SIGTERM 은 PID 1(sh)에게 가는데
#      sh 는 자식에게 전달하지 않는다. uvicorn 이 신호를 못 받아 진행 중 SSE 를
#      드레인하지 못하고, stop_grace_period(180s) 를 넘겨 SIGKILL 로 강제 종료된다
#      → 그 턴 대화가 유실된다. 180s 를 준 이유 자체가 무의미해진다.
#   ② 크래시 감지 불가 — PID 1(sh)이 죽어도 uvicorn 은 고아로 살아남아 컨테이너가
#      계속 running 으로 보인다. restart 정책이 발동하지 않는다.
#      (2026-08-21 실측: `docker exec ... kill -9 1` 후 restarts=0 status=running)
#
#   `exec` 를 붙이면 uvicorn 이 sh 를 **대체**해 PID 1 이 된다. env 확장(${...})은
#   sh 가 exec 전에 이미 처리하므로 shell 형식의 이점은 그대로 유지된다.
CMD exec uvicorn server:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1}
