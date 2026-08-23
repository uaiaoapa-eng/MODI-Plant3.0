"""Dockerfile CMD 의 신호 전달 회귀 테스트 — 도커 실행 없이 정적 검증.

배경 (2026-08-21 실측):
    CMD 가 shell 형식이면 도커가 `/bin/sh -c "..."` 로 감싸 **PID 1 이 sh** 가 되고
    uvicorn 은 그 자식이 된다. 그러면 두 가지가 조용히 깨진다.

    ① graceful shutdown 무력화
       `docker stop` 의 SIGTERM 은 PID 1(sh)에게 간다. sh 는 자식에게 전달하지 않으므로
       uvicorn 이 신호를 못 받고, 진행 중 SSE 를 드레인하지 못한 채 stop_grace_period
       (180s) 를 넘겨 SIGKILL 된다 → 그 턴 대화가 유실된다.
       **180s 를 준 이유(PR #174) 자체가 무의미해진다.**

    ② 크래시 감지 불가
       PID 1(sh)이 죽어도 uvicorn 은 고아로 살아남아 컨테이너가 계속 running 으로
       보인다. restart 정책이 발동하지 않는다. 실측:
           docker exec edu-agent-2 sh -c 'kill -9 1'
           → restarts=0 status=running   (죽지 않음)

    `exec` 를 붙이면 uvicorn 이 sh 를 대체해 PID 1 이 된다. env 확장(${...})은 sh 가
    exec 전에 처리하므로 shell 형식의 이점은 유지된다.

이 테스트는 그 한 글자가 사라지는 회귀를 막는다.
"""
import pathlib
import re

import pytest

DOCKERFILE = pathlib.Path(__file__).resolve().parent.parent / "Dockerfile"


@pytest.fixture(scope="module")
def cmd_line() -> str:
    """Dockerfile 의 CMD 지시문 한 줄(주석 제외)."""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("CMD "):
            return line
    pytest.fail("Dockerfile 에 CMD 지시문이 없다")


def test_cmd_uses_exec_form_semantics(cmd_line):
    """★ 핵심: shell 형식 CMD 는 반드시 `exec` 로 시작해야 한다.

    이게 없으면 SIGTERM 이 uvicorn 에 도달하지 않아 graceful drain 이 죽고
    (진행 중 대화 유실), 크래시 시 restart 정책도 발동하지 않는다.
    """
    body = cmd_line[len("CMD "):].strip()
    if body.startswith("["):
        return  # JSON(exec) 형식이면 애초에 sh 를 거치지 않는다 — OK
    assert body.startswith("exec "), (
        "shell 형식 CMD 가 exec 로 시작하지 않는다 — PID 1 이 sh 가 되어 "
        "SIGTERM 이 uvicorn 에 전달되지 않고(graceful drain 무력화), "
        "PID 1 사망 시에도 컨테이너가 running 으로 남는다(restart 미발동)"
    )


def test_cmd_still_expands_web_concurrency(cmd_line):
    """워커 수 env 확장이 유지돼야 한다 — exec 를 붙여도 sh 가 먼저 확장한다."""
    assert "${WEB_CONCURRENCY" in cmd_line, "WEB_CONCURRENCY 확장이 사라졌다"


def test_cmd_binds_all_interfaces_and_fixed_port(cmd_line):
    """컨테이너 내부 포트는 8000 고정(compose 가 매핑), 0.0.0.0 바인딩."""
    assert "--host 0.0.0.0" in cmd_line
    assert "--port 8000" in cmd_line


def test_cmd_has_no_reload_in_production(cmd_line):
    """--reload 는 개발 전용 — 운영에 들어가면 파일 감시로 워커가 계속 재기동된다."""
    assert "--reload" not in cmd_line


def test_stop_grace_period_is_long_enough_for_a_turn():
    """graceful drain 시간이 실측 생성 턴(94~160초)보다 길어야 의미가 있다.

    exec 수정과 짝이다 — 신호가 도달해도 시간이 짧으면 여전히 턴이 잘린다.
    """
    compose = (DOCKERFILE.parent / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"stop_grace_period:\s*(\d+)s", compose)
    assert m, "docker-compose.yml 에 stop_grace_period 가 없다"
    assert int(m.group(1)) >= 160, (
        f"stop_grace_period={m.group(1)}s — 실측 생성 턴이 94~160초라 "
        "이보다 짧으면 배포 교체 때 진행 중 대화가 잘린다"
    )
