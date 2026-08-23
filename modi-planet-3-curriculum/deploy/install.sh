#!/usr/bin/env bash
# 온프렘 서버 설치 스크립트 — 하이브리드 검색 데모.
#
#   bash deploy/install.sh          # 벡터(풀) 이미지 빌드·기동
#   bash deploy/install.sh --lite   # 경량(부분일치 단독) 이미지 기동
#
# 리포지토리 루트에서 실행. Docker + Docker Compose v2 필요.
set -euo pipefail

cd "$(dirname "$0")/.."   # 리포 루트로
ROOT="$(pwd)"

MODE="full"
[[ "${1:-}" == "--lite" ]] && MODE="lite"

echo "==> edu-agent RAG 검색 온프렘 설치 (mode=$MODE, dir=$ROOT)"

# 1) 사전 점검
command -v docker >/dev/null 2>&1 || { echo "✗ docker 없음. 먼저 Docker 설치."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "✗ 'docker compose'(v2) 없음."; exit 1; }
[[ -d projects ]] || { echo "✗ projects/ (세션 데이터) 없음 — 온톨로지 빌드 불가."; exit 1; }

# 2) .env 준비
if [[ ! -f .env ]]; then
  cp deploy/.env.example .env
  echo "==> .env 생성(기본 임계값). 필요 시 TAU_REUSE/TAU_NEAR 조정 후 재기동."
fi

# 3) 빌드·기동
if [[ "$MODE" == "lite" ]]; then
  COMPOSE="docker-compose.rag-demo.yml"
  echo "==> 경량 이미지: 벡터 없이 부분일치 검색만 동작(vector_enabled=false)."
else
  COMPOSE="docker-compose.rag-search.yml"
  echo "==> 풀 이미지: BGE-m3 다운로드+임베딩을 빌드 시 굽습니다(수 분 소요, ~4-5GB)."
fi

docker compose -f "$COMPOSE" up --build -d

# 4) 헬스체크 대기
PORT="$(grep -E '^PORT=' .env | cut -d= -f2 || true)"; PORT="${PORT:-8100}"
echo -n "==> 헬스체크(http://localhost:${PORT}/health) "
for i in $(seq 1 60); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "OK"
    curl -fsS "http://localhost:${PORT}/health"; echo
    echo "==> 완료 → 브라우저에서 http://<서버IP>:${PORT}  (검색/도출/커버리지)"
    exit 0
  fi
  echo -n "."; sleep 3
done
echo; echo "✗ 헬스체크 실패. 로그: docker compose -f $COMPOSE logs --tail=50"
exit 1
