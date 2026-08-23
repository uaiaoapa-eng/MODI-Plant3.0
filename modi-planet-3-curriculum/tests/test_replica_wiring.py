"""레플리카 배선 정합성 — 세 곳이 어긋나면 트래픽이 새거나 배포가 깨진다.

레플리카 목록이 세 파일에 나뉘어 있다:

    docker-compose.yml          서비스 정의 + nginx depends_on
    deploy/nginx/edu-agent.conf upstream 목록
    .github/workflows/deploy.yml  EDU_REPLICAS (롤링 교체·헬스 검증 대상)

2026-08-21 에 실제로 어긋났다: 컨테이너는 3대 healthy 였는데 nginx 는 옛 설정
(`server edu-agent:8000` 한 줄)을 물고 있어 **새 레플리카가 트래픽을 0 으로 받았고**,
healthy 체크만으로는 못 잡아 배포가 초록으로 통과했다.

여기서 세 목록이 정확히 같은지 고정한다.
"""
import pathlib
import re

import yaml

# ⚠ 예전엔 pytest.importorskip("yaml") 이었다. 그러면 PyYAML 이 없는 환경(CI)에서
#   이 파일이 **통째로 건너뛰어진다** — 레플리카 목록이 어긋나도 CI 가 초록으로
#   통과한다. 이 검사는 건너뛰면 안 되는 종류라 pyyaml 을 test 의존성에 넣고
#   평범하게 임포트한다(pyproject [project.optional-dependencies] test).

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
NGINX = ROOT / "deploy" / "nginx" / "edu-agent.conf"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _replicas_from_compose() -> list[str]:
    return sorted((k for k in _compose()["services"] if re.fullmatch(r"edu-agent-\d+", k)),
                  key=lambda s: int(s.rsplit("-", 1)[1]))


def _replicas_from_nginx() -> list[str]:
    text = NGINX.read_text(encoding="utf-8")
    return re.findall(r"^\s*server (edu-agent-\d+):8000", text, re.M)


def _replicas_from_workflow() -> list[str]:
    wf = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    env = next(j["env"] for j in wf["jobs"].values() if "env" in j)
    return str(env["EDU_REPLICAS"]).split()


def test_all_three_lists_match():
    """★ 셋 중 하나만 빠져도 그 레플리카는 트래픽을 못 받거나 교체가 안 된다."""
    c, n, w = _replicas_from_compose(), _replicas_from_nginx(), _replicas_from_workflow()
    assert sorted(c) == sorted(n), f"compose 와 nginx upstream 불일치\n  compose={c}\n  nginx={n}"
    assert sorted(c) == sorted(w), f"compose 와 배포 목록 불일치\n  compose={c}\n  deploy={w}"


def test_nginx_waits_for_every_replica():
    """upstream 호스트명이 안 풀린 상태로 nginx 가 뜨면 **설정 로드 자체가 실패**한다."""
    dep = _compose()["services"]["edu-nginx"]["depends_on"]
    # 집합으로 비교한다 — 사전순 정렬은 edu-agent-10 이 -2 앞에 와서 순서가 안 맞는다.
    assert set(dep) == set(_replicas_from_compose()), (
        f"nginx depends_on 누락: {sorted(set(_replicas_from_compose()) - set(dep))}")
    assert all(v.get("condition") == "service_healthy" for v in dep.values())


def test_every_replica_declares_its_own_name():
    """container_name 은 컨테이너 호스트명이 아니다(도커는 ID 해시를 준다).

    REPLICA_NAME 이 빠지면 리포트의 '서버별' 표가 읽을 수 없는 해시로 채워지고
    재배포마다 값이 바뀌어 어제와 오늘을 대조할 수 없다.
    """
    svcs = _compose()["services"]
    for name in _replicas_from_compose():
        env = svcs[name].get("environment") or {}
        assert env.get("REPLICA_NAME") == name, f"{name} 의 REPLICA_NAME 이 어긋났다: {env}"


def test_replica_environment_keeps_base_keys():
    """서비스에 environment 를 다시 쓰면 베이스 블록을 **통째로 덮어쓴다**(YAML 병합 아님).

    앵커 머지를 빠뜨리면 USE_LOCAL_CLAUDE·REDIS_URL 이 사라져 조용히 다르게 동작한다.
    """
    svcs = _compose()["services"]
    base = set((_compose()["x-edu-agent-base"].get("environment") or {}))
    for name in _replicas_from_compose():
        env = set(svcs[name].get("environment") or {})
        missing = base - env
        assert not missing, f"{name} 에 베이스 환경변수 누락: {sorted(missing)}"


def test_only_one_service_builds_the_image():
    """레플리카마다 build 를 선언하면 배포가 N 번 빌드한다."""
    svcs = _compose()["services"]
    builders = [n for n in _replicas_from_compose() if svcs[n].get("build")]
    assert builders == ["edu-agent-1"], f"빌드 선언이 하나가 아니다: {builders}"


def test_deploy_verifies_upstream_count_dynamically():
    """검증이 대수를 하드코딩하면 증설할 때마다 배포가 거짓 실패/거짓 성공한다."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "edu-agent-[0-9]+:8000" in text, "upstream 검사가 특정 대수에 묶여 있다"
    assert 'wc -w' in text, "기대 대수를 목록에서 세지 않는다"


def test_replicas_expose_no_host_ports():
    """호스트 포트는 edu-nginx 만 노출한다 — 레플리카가 직접 열리면 sticky 를 우회한다."""
    svcs = _compose()["services"]
    for name in _replicas_from_compose():
        assert not svcs[name].get("ports"), f"{name} 이 호스트 포트를 노출한다"
