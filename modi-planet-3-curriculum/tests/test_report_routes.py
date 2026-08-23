"""리포트 웹 라우트 접근 통제 회귀 테스트 — 네트워크 미사용.

이 페이지는 학생이 쓰는 것과 **같은 도메인**(`ai.modiplanet.com`)에 붙고,
내용은 사용자별 사용량·비용이다. 그래서 렌더보다 먼저 지켜야 하는 건 접근 통제다.

설계 결정 두 가지를 여기서 고정한다.

    ① fail-closed — REPORT_TOKEN 이 없으면 라우트가 아예 404 다.
       "설정을 깜빡해서 전 세계에 공개" 가 기본값이 되면 안 된다.
    ② 404 (403 아님) — 토큰이 틀렸을 때 403 을 주면 "여기 뭔가 있다" 를 알려 준다.
       존재 자체를 숨긴다.
"""
import pytest

try:
    import server
    from fastapi.testclient import TestClient
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"server import 불가: {e}", allow_module_level=True)

TOKEN = "test-report-token"
PAGES = ["/report", "/api/usage/report", "/report/archive/2026-08-22"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("REPORT_TOKEN", TOKEN)
    return TestClient(server.app)


@pytest.fixture
def client_no_token(monkeypatch):
    monkeypatch.delenv("REPORT_TOKEN", raising=False)
    return TestClient(server.app)


# ──────────────────────────────────────────────────────────────────────────────
# 접근 통제
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PAGES)
def test_disabled_when_token_unset(client_no_token, path):
    """★ 기본은 꺼짐. 설정을 깜빡해도 공개되지 않는다."""
    assert client_no_token.get(path).status_code == 404


@pytest.mark.parametrize("path", PAGES)
def test_no_token_is_404(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("bad", ["", "nope", TOKEN + "x", TOKEN[:-1], TOKEN.upper()])
def test_wrong_token_is_404(client, bad):
    """403 이 아니라 404 — 존재를 알려 주지 않는다."""
    assert client.get(f"/report?token={bad}").status_code == 404


def test_blank_token_env_does_not_open_everything(monkeypatch):
    """REPORT_TOKEN 이 공백만 있어도 '설정됨'으로 오인하면 안 된다."""
    monkeypatch.setenv("REPORT_TOKEN", "   ")
    c = TestClient(server.app)
    assert c.get("/report").status_code == 404
    assert c.get("/report?token=   ").status_code == 404


def test_header_token_accepted(client):
    """URL 에 토큰을 남기고 싶지 않은 자동화용 — 헤더로도 받는다."""
    r = client.get("/api/usage/report", headers={"X-Report-Token": TOKEN})
    assert r.status_code == 200


def test_valid_token_renders_html(client):
    r = client.get(f"/report?token={TOKEN}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text.lstrip().startswith("<!doctype html>")


# ──────────────────────────────────────────────────────────────────────────────
# 아카이브 — 경로 조작
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("day", [
    "../../etc/passwd", "..%2f..%2fetc%2fpasswd", "2026-08-22/../../../etc/passwd",
    "....//....//etc/passwd", "2026-8-2", "abcd-ef-gh", "",
])
def test_archive_rejects_path_traversal(client, day):
    """★ 날짜 형식만 통과시킨다 — 파일 경로를 사용자 입력으로 만들면 안 된다."""
    r = client.get(f"/report/archive/{day}?token={TOKEN}")
    assert r.status_code == 404
    assert "passwd" not in r.text


def test_archive_missing_day_is_404(client):
    assert client.get(f"/report/archive/2020-01-01?token={TOKEN}").status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# 업스트림 장애 — 페이지는 떠야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_page_renders_when_upstream_missing(client):
    """RAG_UPSTREAM 미설정/불통이어도 500 이 아니라 원인을 보여준다.

    운영자가 이 페이지를 여는 시점은 대개 뭔가 이상할 때다. 그때 500 백지가 뜨면
    아무 도움이 안 된다.
    """
    r = client.get(f"/report?token={TOKEN}")
    assert r.status_code == 200
    assert "불러오지 못했습니다" in r.text


def test_json_endpoint_reports_error_not_crash(client):
    r = client.get(f"/api/usage/report?token={TOKEN}")
    assert r.status_code == 200
    assert r.json().get("ok") is False


# ──────────────────────────────────────────────────────────────────────────────
# 배선 — 프리픽스 가정
# ──────────────────────────────────────────────────────────────────────────────

# 성공 경로는 업스트림이 있어야 탄다. 실제 rag-search 를 띄우지 않고 응답만 흉내낸다.

_FIXTURE = {
    "ok": True,
    "period": {"start": "2026-08-22 00:00:00", "end": "2026-08-22 23:59:59",
               "first_turn": "", "last_turn": ""},
    "assumptions": {"model": "claude-haiku-4-5", "krw_per_usd": 1400.0},
    "totals": {"turns": 7, "users": 3, "sessions": 3, "input_tokens": 100,
               "output_tokens": 200, "cache_read_tokens": 0,
               "cache_creation_tokens": 50, "weighted_tokens": 1_162,
               "usd": 0.0012, "krw": 2, "usd_per_turn": 0.0002, "turns_per_user": 2.3},
    "by_kind": [], "top_users": [], "by_hour": [],
}


class _FakeResp:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return _FIXTURE


# 업스트림에 실제로 무엇이 넘어갔는지 확인하려면 렌더 결과가 아니라 이 기록을 봐야 한다
# (가짜 업스트림은 고정 픽스처를 돌려주므로 화면 문자열로는 검증되지 않는다).
_CALLS: list = []


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        _CALLS.append((url, dict(params or {})))
        return _FakeResp()


@pytest.fixture
def live(client, monkeypatch):
    """업스트림이 정상 응답하는 상태의 클라이언트."""
    monkeypatch.setattr(server, "_RAG_UPSTREAM", "http://rag-search:8100", raising=False)
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    _CALLS.clear()
    return client


def _report_params():
    """/api/usage/report 로 넘어간 마지막 파라미터."""
    for url, params in reversed(_CALLS):
        if url.endswith("/api/usage/report"):
            return params
    raise AssertionError(f"리포트 조회가 없었다: {_CALLS}")


def test_live_page_renders_numbers(live):
    body = live.get(f"/report?token={TOKEN}").text
    assert "불러오지 못했습니다" not in body
    assert "1,162" in body, "가중토큰이 화면에 나와야 한다"


def test_form_action_is_relative(live):
    """★ 앱은 자신이 /agent 아래 붙어 있는지 모른다(NPM 이 프리픽스를 떼고 넘긴다).

    절대경로를 만들면 프록시 뒤에서 링크가 깨진다.
    """
    import re
    body = live.get(f"/report?token={TOKEN}").text
    actions = re.findall(r'<form[^>]*action="([^"]*)"', body)
    assert actions, "조회 폼이 있어야 한다"
    for action in actions:
        assert not action.startswith("/"), f"절대경로 action 은 프록시 뒤에서 깨진다: {action}"


def test_token_preserved_in_form(live):
    """조회 버튼을 눌러도 토큰이 유지돼야 한다 — 아니면 매번 404."""
    body = live.get(f"/report?token={TOKEN}").text
    assert f'name="token" value="{TOKEN}"' in body


def test_llm_mode_shown(live, monkeypatch):
    """CLI 구독 모드면 실청구가 0원이라, 금액을 읽기 전에 모드를 알아야 한다."""
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "false")
    assert '<span class="mode">api</span>' in live.get(f"/report?token={TOKEN}").text
    monkeypatch.setenv("USE_LOCAL_CLAUDE", "true")
    assert '<span class="mode">cli</span>' in live.get(f"/report?token={TOKEN}").text


# ──────────────────────────────────────────────────────────────────────────────
# 빈 쿼리 파라미터 — 폼이 실제로 보내는 모양
# ──────────────────────────────────────────────────────────────────────────────
#
# 2026-08-21 운영에서 조회 버튼을 누르자 리포트 대신 이게 떴다:
#
#   {"detail":[{"type":"float_parsing","loc":["query","budget_usd"],
#               "msg":"Input should be a valid number, unable to parse string as a number",
#               "input":""}]}
#
# HTML 폼은 비어 있는 입력도 `budget_usd=` 로 **보낸다**. 타입을 float 로 선언하면
# FastAPI 가 422 를 내고 화면 전체가 사라진다. 조회 화면의 선택 입력은 못 읽으면
# 무시하는 게 맞다 — 숫자 하나 때문에 리포트를 못 보는 건 과한 실패다.

EMPTY_FORM = ("start=2026-07-23&end=2026-08-21&budget_usd=&preset=7d")


@pytest.mark.parametrize("path", ["/reports", "/report"])
def test_empty_budget_does_not_422(live, path):
    """★ 실제 실패 URL 재현 — 폼이 보낸 빈 예산값."""
    r = live.get(f"{path}?token={TOKEN}&{EMPTY_FORM}")
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("qs", [
    "budget_usd=", "budget_usd=abc", "budget_usd=-5", "budget_usd= ",
    "limit_users=", "limit_users=xyz", "limit_users=-1",
])
def test_garbage_numeric_params_fall_back(live, qs):
    """손으로 URL 을 고쳐도 화면은 떠야 한다 — 못 읽는 값은 기본값으로 흡수."""
    assert live.get(f"/reports?token={TOKEN}&{qs}").status_code == 200


def test_valid_budget_still_applies(live):
    """방어가 정상 입력을 삼키면 안 된다."""
    body = live.get(f"/reports?token={TOKEN}&budget_usd=100").text
    assert "예산 대비" in body
    assert "budget_usd" in body      # 폼에 값이 되돌아가야 재조회가 유지된다


def test_preset_overrides_explicit_range(live):
    """프리셋 버튼은 폼 전체를 보내므로 start/end 가 함께 온다 — 프리셋이 이겨야 한다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    live.get(f"/reports?token={TOKEN}&start=2020-01-01&end=2020-01-02&preset=today")
    p = _report_params()
    assert p["start"] == today and p["end"] == today, p


def test_no_range_defaults_to_30_days(live):
    """목록 화면의 목적은 '추세'라 하루만 보면 의미가 없다."""
    live.get(f"/reports?token={TOKEN}")
    p = _report_params()
    assert p["start"] and p["end"] and p["start"] < p["end"]


def test_garbage_limit_users_becomes_default(live):
    """쓰레기 값이 업스트림까지 흘러가면 안 된다."""
    live.get(f"/reports?token={TOKEN}&limit_users=xyz")
    assert _report_params()["limit_users"] == 25   # 한 페이지 기본 행 수


def test_archive_tolerates_empty_budget(live):
    """아카이브도 같은 폼에서 링크가 만들어진다."""
    r = live.get(f"/report/archive/2026-08-22?token={TOKEN}&budget_usd=")
    assert r.status_code in (200, 404)   # 확정본 유무와 무관하게 422 는 아니어야 한다


# ──────────────────────────────────────────────────────────────────────────────
# 페이지네이션 · 검색
# ──────────────────────────────────────────────────────────────────────────────
#
# 표를 다 뿌리지 않는 이유는 성능만이 아니다 — 수백 줄을 한 화면에 쏟으면 읽히지 않는다.
# 페이징 링크는 **현재 쿼리를 물고 가야** 한다. 안 그러면 페이지를 넘길 때마다
# 토큰·기간이 날아가 404 나 엉뚱한 기간이 뜬다.

@pytest.mark.parametrize("path", ["/reports", "/report"])
def test_paging_params_reach_upstream(live, path):
    live.get(f"{path}?token={TOKEN}&poff=50&uoff=25&pq=등대")
    p = _report_params()
    assert p["project_offset"] == 50
    assert p["user_offset"] == 25
    assert p["project_q"] == "등대"


@pytest.mark.parametrize("bad", ["", "abc", "-10", " "])
def test_garbage_offsets_fall_back_to_zero(live, bad):
    """손으로 URL 을 고쳐도 첫 페이지를 보여줘야 한다."""
    r = live.get(f"/reports?token={TOKEN}&poff={bad}&uoff={bad}")
    assert r.status_code == 200
    assert _report_params()["project_offset"] == 0


def test_search_term_is_not_interpolated_into_sql(live):
    """★ 검색어는 파라미터로만 넘어가야 한다 — 여기서 문자열을 이어붙이면 주입이 된다."""
    evil = "'; DROP TABLE sessions; --"
    r = live.get(f"/reports?token={TOKEN}&pq={evil}")
    assert r.status_code == 200
    assert _report_params()["project_q"] == evil   # 그대로 전달(가공·이스케이프는 DB 계층)


def test_pager_links_keep_context():
    """★ 페이징 링크가 토큰·기간을 잃으면 다음 페이지에서 404 가 난다."""
    import report_html as H
    out = H._pager(total=120, offset=25, page_size=25, param="poff",
                   base_qs={"token": "tok", "start": "2026-08-01", "budget_usd": ""},
                   label="개")
    assert "token=tok" in out and "start=2026-08-01" in out
    assert "budget_usd=" not in out, "빈 값을 실어 보내면 다시 422 를 부른다"
    assert "poff=50" in out and "poff=0" in out


def test_pager_hidden_when_one_page():
    """한 페이지에 다 들어가면 컨트롤을 보이지 않는다 — 없는 선택지를 그리지 않는다."""
    import report_html as H
    assert H._pager(total=10, offset=0, page_size=25, param="poff",
                    base_qs={}, label="개") == ""


def test_pager_boundaries_are_disabled():
    """첫 페이지에서 '이전', 마지막에서 '다음'은 링크가 아니어야 한다."""
    import report_html as H
    first = H._pager(total=100, offset=0, page_size=25, param="poff",
                     base_qs={"token": "t"}, label="개")
    assert '<span class="pg-off">이전</span>' in first
    last = H._pager(total=100, offset=75, page_size=25, param="poff",
                    base_qs={"token": "t"}, label="개")
    assert '<span class="pg-off">다음</span>' in last
