PYTHON ?= python3

.DEFAULT_GOAL := help
.PHONY: help run test load-test sim nginx-register

help:  ## 사용 가능한 명령 목록
	@echo "사용법: make <target>"
	@echo ""
	@echo "  run              개발 서버 실행 (uvicorn, --reload, :8000)"
	@echo "  test             단위 테스트 실행 (pytest)"
	@echo "  load-test        동시성/부하 검증 스크립트 (LOAD_ARGS 로 인자 전달)"
	@echo "  nginx-register   NPM에 edu-agent.luxrobo.net 프록시 자동 등록 (env 필요)"
	@echo "  help             이 도움말"

run:  ## 개발 서버 실행 (uvicorn, --reload, :8000)
	.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --reload

test:  ## 단위 테스트 실행 (pytest). 최초 1회: .venv/bin/pip install -e ".[test]"
	.venv/bin/python -m pytest

load-test:  ## 동시성/부하 검증. 예: make load-test LOAD_ARGS="--mode same-session --concurrency 5"
	$(PYTHON) scripts/load_test.py $(LOAD_ARGS)

sim:  ## chat 시뮬레이션(전체/과정 두 모드 자동 검증·재사용). 예: make sim SIM_ARGS="--mode quick --runs 3"
	PYTHONPATH=scripts $(PYTHON) scripts/sim_chat_flow.py $(SIM_ARGS)

# Nginx Proxy Manager(192.168.0.102:81)에 edu-agent.luxrobo.net 프록시 자동 등록
# 실행 전 환경변수 필요:
#   export NPM_EMAIL=... NPM_PASS=...      # NPM 관리자 로그인
#   export AUTH_USER=... AUTH_PASS=...     # 도메인 기본인증 계정
# 선택: LE_EMAIL, DOMAIN, FWD_HOST, FWD_PORT, ENABLE_SSL (기본 SSL 발급)
nginx-register:  ## NPM에 edu-agent.luxrobo.net 프록시 자동 등록 (env 필요)
	$(PYTHON) scripts/npm_register.py
