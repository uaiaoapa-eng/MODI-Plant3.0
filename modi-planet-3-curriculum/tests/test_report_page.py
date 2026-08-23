"""사용량 리포트 웹페이지 회귀 테스트 — 서버·DB·네트워크 미사용.

이 페이지는 **사용자별 사용량과 비용**을 담는다. 학생이 쓰는 도메인과 같은 곳에
붙으므로, 렌더 품질보다 먼저 지켜야 할 것이 두 가지다.

    ① 토큰 없이는 존재조차 드러나지 않는다 (fail-closed, 404)
    ② 사용자 입력(user_id)이 HTML 로 실행되지 않는다 (XSS)

그리고 스냅샷과 라이브 페이지가 **같은 렌더러**를 쓴다는 것 — 둘이 갈라지면
"웹에서 본 값"과 "보관된 값"이 달라져 청구 근거가 무너진다.
"""
import re
import sys
from html.parser import HTMLParser
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

try:
    import report_html as H
    import report_snapshot as S
except Exception as e:  # 의존성 미설치 환경에서는 스킵
    pytest.skip(f"report 모듈 import 불가: {e}", allow_module_level=True)


def _report(**over):
    base = {
        "ok": True,
        "period": {"start": "2026-08-22 00:00:00", "end": "2026-08-22 23:59:59",
                   "first_turn": "2026-08-22 09:03:11", "last_turn": "2026-08-22 11:47:52"},
        "assumptions": {"model": "claude-haiku-4-5", "usd_per_weighted_mtok": 1.0,
                        "krw_per_usd": 1400.0},
        "totals": {"turns": 1240, "users": 40, "sessions": 152,
                   "input_tokens": 410_000, "output_tokens": 5_800_000,
                   "cache_read_tokens": 120_000, "cache_creation_tokens": 3_400_000,
                   "weighted_tokens": 33_450_000, "usd": 33.45, "krw": 46_830,
                   "usd_per_turn": 0.027, "turns_per_user": 31.0},
        "by_kind": [{"mode": "quick", "coding_type": "blockly", "turns": 800,
                     "weighted_tokens": 24_000_000, "usd": 24.0, "krw": 33_600}],
        "top_users": [{"user_id": "3f8a91c2-77bd-4e10-9a44-1b2c3d4e5f60", "turns": 62,
                       "weighted_tokens": 2_100_000, "usd": 2.1, "krw": 2_940}],
        "by_hour": [{"hour": "2026-08-22 10:00", "turns": 540, "users": 40,
                     "weighted_tokens": 14_600_000, "usd": 14.6, "krw": 20_440}],
    }
    base.setdefault("patterns", {"ok": True, "users": [],
                                 "session_depth": [], "user_segments": []})
    base.setdefault("billing", {})
    base.setdefault("reuse", {})
    base.setdefault("by_day", [])
    base.update(over)
    return base


# ──────────────────────────────────────────────────────────────────────────────
# 보안 — 사용자 입력이 HTML 로 실행되면 안 된다
# ──────────────────────────────────────────────────────────────────────────────

def _with_evil(value):
    """user_id·제목이 화면에 나오는 **모든 지점**에 같은 문자열을 심는다.

    한 곳만 검사하면 패널을 옮기거나 새로 만들 때 조용히 구멍이 난다(실제로
    top_users 표를 사용자 패턴 패널로 옮기면서 그럴 뻔했다).
    """
    return _report(
        top_users=[{"user_id": value, "turns": 1, "weighted_tokens": 1,
                    "usd": 0, "krw": 0}],
        patterns={"ok": True, "users": [{"user_id": value, "turns": 1, "sessions": 1,
                                         "turns_per_session": 1, "design_ratio": 0,
                                         "blockly_ratio": 0, "cold_ratio": 0, "usd": 0}],
                  "session_depth": [], "user_segments": []},
        projects={"ok": True, "created": 1, "by_type": [],
                  "items": [{"session_id": value, "user_id": value, "title": value,
                             "coding_type": value, "app_type": value, "phase": value,
                             "created_at": "2026-08-22 09:00:00"}]},
    )


@pytest.mark.parametrize("render_fn", ["render", "render_index"])
def test_user_id_is_escaped(render_fn):
    """★ user_id 는 외부 입력이다. 리포트를 여는 건 관리자이므로 XSS 는 곧 관리자 세션 탈취다."""
    evil = '<script>alert(1)</script>'
    out = getattr(H, render_fn)(_with_evil(evil))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


@pytest.mark.parametrize("render_fn", ["render", "render_index"])
def test_attribute_injection_escaped(render_fn):
    """title·href 속성으로도 빠져나갈 수 없어야 한다(따옴표 탈출)."""
    evil = '" onmouseover="alert(1)'
    out = getattr(H, render_fn)(_with_evil(evil))
    assert 'onmouseover="alert(1)"' not in out
    assert "&quot;" in out


class _AttrScan(HTMLParser):
    """실제 파서로 속성을 훑는다.

    문자열 정규식으로 ` onerror=` 를 찾으면 **이스케이프된 텍스트까지 걸려** 오탐이 난다
    (title="&quot; onerror=&quot;x&quot;" 는 안전하다 — 속성으로 파싱되지 않는다).
    진짜 위험은 '파서가 속성으로 인식하는가' 이므로 파서에게 물어본다.
    """

    def __init__(self):
        super().__init__()
        self.handlers = []
        self.bad_href = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.handlers.append((tag, name))
            if name.lower() in ("href", "src") and (value or "").strip().lower().startswith(
                    ("javascript:", "data:text/html")):
                self.bad_href.append((tag, value))


def _scan(html_text):
    s = _AttrScan()
    s.feed(html_text)
    return s


def test_no_inline_event_handlers_anywhere():
    """★ 어떤 입력을 넣어도 파서가 이벤트 핸들러로 읽는 속성이 생기면 안 된다."""
    s = _scan(H.render_index(_with_evil('" onerror="x" onload="y')))
    assert s.handlers == [], f"인라인 이벤트 핸들러가 생겼다: {s.handlers}"


def test_no_javascript_urls():
    """프로젝트 링크는 사용자 입력(session_id/user_id)으로 만들어진다 — 스킴 주입 차단."""
    s = _scan(H.render_index(_with_evil("javascript:alert(1)")))
    assert s.bad_href == [], f"위험한 링크가 생겼다: {s.bad_href}"


def test_no_external_resources():
    """★ 자체 완결이어야 한다 — 사내망·오프라인·엄격한 CSP 어디서든 같은 모습.

    스냅샷은 몇 달 뒤 열릴 수 있는데, 그때 외부 CDN 이 사라졌으면 깨진 페이지가 된다.
    """
    out = H.render(_report())
    for attr in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', out):
        assert not attr.startswith(("http://", "https://", "//")), \
            f"외부 리소스를 참조한다: {attr}"
    assert "@import" not in out


def test_noindex_header_present():
    """검색엔진에 색인되면 안 된다."""
    assert 'name="robots"' in H.render(_report())
    assert "noindex" in H.render(_report())


# ──────────────────────────────────────────────────────────────────────────────
# 렌더 — 값이 실제로 화면에 나오나
# ──────────────────────────────────────────────────────────────────────────────

def test_headline_numbers_rendered():
    out = H.render(_report())
    assert "$33.45" in out
    assert "46,830원" in out
    assert "1,240" in out          # 턴 수


def test_small_amounts_not_flattened_to_zero():
    """★ 실측 턴당 단가가 $0.029 다. 소수 2자리로 반올림하면 $0.03 도 아닌 $0.00 이 된다."""
    out = H.render(_report(totals={**_report()["totals"], "usd_per_turn": 0.0029}))
    assert "$0.0029" in out


def test_empty_period_renders_without_crashing():
    """수업이 없던 날도 페이지는 떠야 한다 — 빈 화면이 아니라 '없음'을 보여준다."""
    out = H.render({"ok": True, "period": {}, "assumptions": {}, "totals": {},
                    "by_kind": [], "top_users": [], "by_hour": []})
    assert "기록된 턴이 없습니다" in out


def test_error_report_renders_diagnosis():
    """조회 실패 시 빈 화면 대신 원인을 짚어 준다."""
    out = H.render({"ok": False, "error": "usage_turns 없음"})
    assert "불러오지 못했습니다" in out
    assert "RAG_UPSTREAM" in out


def test_token_mix_uses_weighted_share_not_raw():
    """★ 원시 토큰 수로 줄 세우면 오해가 생긴다 — 입력이 제일 많아도 비용은 미미하다.

    본문 데이터에서 출력의 비용 기여도는 5.8M×5 = 29M 로 전체 33.4M 중 최다다.
    """
    out = H.render(_report())
    shares = [float(x) for x in re.findall(r'class="mix-fill" style="width:([\d.]+)%', out)]
    assert shares and shares[0] == max(shares), "첫 줄(출력)이 최대 기여도여야 한다"
    assert shares[0] > 80.0


def test_budget_bar_only_when_requested():
    assert "예산 대비" not in H.render(_report())
    assert "예산 대비" in H.render(_report(), budget_usd=100)


@pytest.mark.parametrize("spent,budget,level", [
    (10.0, 100.0, "ok"), (75.0, 100.0, "warn"), (95.0, 100.0, "crit"),
])
def test_budget_level_colors(spent, budget, level):
    out = H.render(_report(totals={**_report()["totals"], "usd": spent}), budget_usd=budget)
    assert f'budget-fill {level}' in out


def test_budget_bar_clamped_over_100pct():
    """예산을 넘겨도 막대가 컨테이너를 뚫고 나가면 안 된다."""
    out = H.render(_report(totals={**_report()["totals"], "usd": 500.0}), budget_usd=100)
    widths = [float(x) for x in re.findall(r'budget-fill \w+" style="width:([\d.]+)%', out)]
    assert widths and widths[0] == 100.0


# ──────────────────────────────────────────────────────────────────────────────
# 테마 — 뷰어 설정 3상태 모두
# ──────────────────────────────────────────────────────────────────────────────

def test_all_three_theme_states_defined():
    """명시 라이트/명시 다크/미지정(OS 설정) 셋 다 색이 정해져야 한다.

    미디어쿼리 안에서만 정의된 색이 있으면 기본 상태에서 한쪽 테마의 글자를
    다른 쪽 배경에 얹는 고전적인 버그가 난다.
    """
    css = H._CSS
    assert ":root{" in css, "기본(라이트) 팔레트가 없다"
    assert "prefers-color-scheme:dark" in css, "OS 다크 대응이 없다"
    assert ':root[data-theme="dark"]' in css, "명시 다크 선택 대응이 없다"
    assert ':root:not([data-theme="light"])' in css, \
        "OS 다크가 명시 라이트 선택을 덮어쓴다"


def test_body_paints_its_own_background():
    """배경을 안 칠하면 호스트 배경이 비쳐 글자가 안 보일 수 있다."""
    assert re.search(r"body\{[^}]*background:var\(--bg\)", H._CSS)


# ──────────────────────────────────────────────────────────────────────────────
# 스냅샷 vs 라이브 — 같은 렌더러를 써야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_snapshot_omits_interactive_form():
    """정적 보관본에서 동작하지 않는 조회 폼은 아예 내보내지 않는다."""
    assert "<form" in H.render(_report(), show_form=True)
    assert "<form" not in H.render(_report(), show_form=False)


def test_snapshot_and_live_agree_on_numbers():
    """★ 같은 데이터면 같은 숫자여야 한다 — 갈라지면 청구 근거가 무너진다."""
    rep = _report()
    live = H.render(rep, show_form=True)
    snap = H.render(rep, show_form=False)
    for token in ("$33.45", "46,830원", "1,240"):
        assert token in live and token in snap


def test_snapshot_module_does_not_render_html():
    """확정본은 **데이터**로 굳힌다 — HTML 을 굳히면 화면을 고칠 때 과거가 안 따라온다.

    렌더는 조회 시점에 하고, DB 에는 payload(집계 원본)를 둔다.
    """
    assert not hasattr(S, "report_html")


# ──────────────────────────────────────────────────────────────────────────────
# 스냅샷 스크립트
# ──────────────────────────────────────────────────────────────────────────────

def test_resolve_day_defaults_to_yesterday():
    """크론은 자정 직후에 도니 기본값이 '어제'여야 그날 확정본이 나온다."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    want = (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert S.resolve_day("") == want
    assert S.resolve_day("yesterday") == want


def test_resolve_day_explicit_and_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert S.resolve_day("2026-08-22") == "2026-08-22"
    assert S.resolve_day("today") == datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def test_resolve_day_rejects_garbage():
    """형식이 틀리면 조용히 엉뚱한 날을 굳히느니 죽는 게 낫다."""
    with pytest.raises(ValueError):
        S.resolve_day("2026/08/22")


def test_days_back_is_oldest_first_and_inclusive():
    """소급 생성은 과거→현재 순서여야 진행 로그가 읽힌다."""
    got = S.days_back(3, end="2026-08-22")
    assert got == ["2026-08-20", "2026-08-21", "2026-08-22"]


def test_snapshot_targets_db_endpoint(monkeypatch):
    """★ 파일이 아니라 DB 에 굳혀야 한다 — 파일은 배포 rsync 한 번에 사라진다."""
    seen = {}

    def fake_post(upstream, path, params, timeout):
        seen.update(path=path, params=params)
        return {"ok": True, "turns": 3, "usd": 0.01, "insight_stored": True}

    monkeypatch.setattr(S, "_post", fake_post)
    out = S.snapshot("http://rag:8100", "2026-08-22", llm_mode="api")
    assert out["ok"] is True
    assert seen["path"] == "/api/usage/snapshot"
    assert seen["params"]["day"] == "2026-08-22"
    assert seen["params"]["with_insight"] == "true"


def test_snapshot_can_skip_insight(monkeypatch):
    """LLM 비용을 0 으로 두고 숫자만 굳히는 경로가 있어야 한다."""
    seen = {}
    monkeypatch.setattr(S, "_post",
                        lambda u, p, params, t: seen.update(params=params) or {"ok": True})
    S.snapshot("http://rag:8100", "2026-08-22", with_insight=False)
    assert seen["params"]["with_insight"] == "false"


def test_snapshot_writes_no_files(tmp_path, monkeypatch):
    """DB 전환 후에도 파일을 남기면 두 원천이 갈라진다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(S, "_post", lambda *a, **k: {"ok": True, "turns": 1, "usd": 0.0})
    S.snapshot("http://rag:8100", "2026-08-22")
    assert list(tmp_path.iterdir()) == []


# ──────────────────────────────────────────────────────────────────────────────
# 크론 파일 — 조용히 안 도는 실수를 막는다
# ──────────────────────────────────────────────────────────────────────────────

CRON = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "cron" / "edu-agent-report.cron"


def test_cron_file_ends_with_newline():
    """★ 마지막 줄이 개행으로 안 끝나면 cron 이 그 줄을 통째로 무시한다."""
    assert CRON.read_bytes().endswith(b"\n")


def test_cron_entry_has_user_field():
    """/etc/cron.d 형식은 5필드 뒤에 실행 사용자가 온다 — 빠지면 파싱 실패."""
    line = [ln for ln in CRON.read_text().splitlines()
            if ln and not ln.startswith("#") and re.match(r"^[\d*]", ln)]
    assert len(line) == 1, "실행 엔트리는 정확히 하나여야 한다"
    fields = line[0].split()
    assert fields[5] == "root", f"6번째 필드는 실행 사용자여야 한다: {fields[:7]}"


def test_cron_uses_absolute_python_path():
    """크론 PATH 는 빈약하다 — 절대경로가 아니면 command not found."""
    assert "/usr/bin/python3" in CRON.read_text()


def test_cron_runs_after_midnight_kst():
    """자정 직전 시작된 턴이 넘어와 적재되는 창이 있어 여유를 둔다."""
    line = [ln for ln in CRON.read_text().splitlines()
            if ln and not ln.startswith("#") and re.match(r"^[\d*]", ln)][0]
    minute, hour = line.split()[0], line.split()[1]
    assert hour == "0" and int(minute) >= 5, "자정 직후 + 여유 시간이어야 한다"


def test_cron_targets_yesterday():
    assert "--day yesterday" in CRON.read_text()


# ──────────────────────────────────────────────────────────────────────────────
# 배포 — 굳혀 둔 리포트를 배포가 지우면 안 된다
# ──────────────────────────────────────────────────────────────────────────────

DEPLOY_YML = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


def test_snapshots_live_in_db_not_files():
    """★ 확정본은 DB(usage_reports)에 둔다.

    파일로 두면 배포 rsync(`--delete`)·볼륨 교체·디스크 정리 어디서든 조용히 사라진다.
    리포트는 세션·사용량과 같은 급의 운영 데이터다.
    """
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "deploy" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS usage_reports" in schema
    # 시간 규약: 저장 UTC, 표시 KST
    assert "generated_at_utc" in schema and "insight_at_utc" in schema


def test_deploy_applies_schema_for_new_tables():
    """확정본 테이블이 배포로 실제 반영되는지 — CREATE TABLE IF NOT EXISTS 는
    이미 있는 DB 에 새 테이블을 만들지만, 스텝이 없으면 아예 안 돈다."""
    text = DEPLOY_YML.read_text(encoding="utf-8")
    assert "apply_schema.py" in text, "배포에 스키마 적용 스텝이 없다"

# ──────────────────────────────────────────────────────────────────────────────
# 차트 x축 라벨 — SVG 안에 두면 글자가 늘어난다
# ──────────────────────────────────────────────────────────────────────────────

def _charts_html() -> str:
    """비용 차트 + 동접 차트를 한 번에."""
    import sys as _s
    import pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    import report_load_html as RL
    import load_analysis as LA
    from datetime import datetime, timedelta

    days = [{"day": f"2026-07-{d:02d}", "usd": 0.1 + d * 0.01} for d in range(1, 32)]

    def turn(m):
        st = datetime(2026, 8, 22, 0, 0) + timedelta(minutes=m)
        return {"started_at": st, "ts": st + timedelta(milliseconds=90000),
                "duration_ms": 90000, "status": "ok"}

    load = {"concurrency": LA.concurrency_timeline([turn(i // 2) for i in range(60)])}
    return H._chart(days) + RL.concurrency_panel(load)


def test_charts_have_no_text_inside_stretched_svg():
    """★ 2026-08-21 실측 회귀.

    차트는 폭을 꽉 채워야 해서 preserveAspectRatio="none" 으로 늘리는데, 그러면
    **글자도 같이 늘어난다.** viewBox 100 폭이 약 1104px 로 그려져 가로 11배 /
    세로 1.07배 — 날짜 라벨이 높이 3.3px 에 가로만 10배로 뭉개졌다.
    막대·선은 늘어나야 맞고 글자는 늘어나면 안 되므로 분리한 상태를 고정한다.
    """
    html = _charts_html()
    assert 'preserveAspectRatio="none"' in html, "차트가 폭을 채우는 설정이 사라졌다"
    assert "<text" not in html, (
        "늘어나는 SVG 안에 <text> 가 들어갔다 — 글자가 가로로 10배 뭉개진다. "
        "x축 라벨은 _xlabels() 로 SVG 밖 HTML 에 그려야 한다")


def test_charts_render_x_labels_as_html():
    """라벨이 사라진 게 아니라 SVG 밖으로 옮겨졌는지 확인한다(둘 다 빈 축은 무의미)."""
    html = _charts_html()
    assert html.count('<div class="xlabels">') == 2, "두 차트 모두 x축 라벨이 있어야 한다"
    spans = re.findall(r'<span class="[a-z]*" style="left:([\d.]+)%">', html)
    assert len(spans) >= 8, f"라벨이 너무 적다: {len(spans)}"
    assert all(0.0 <= float(p) <= 100.0 for p in spans), "라벨이 차트 밖에 놓였다"


def test_x_labels_are_thinned_so_they_do_not_collide():
    """31일치를 다 찍으면 겹쳐서 못 읽는다 — 골라 내야 한다."""
    import sys as _s
    import pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    html = H._xlabels([f"07-{d:02d}" for d in range(1, 32)], 31)
    n = html.count("<span")
    assert 2 <= n <= 8, f"라벨 개수가 {n} — 너무 촘촘하면 겹치고 너무 적으면 축을 못 읽는다"


def test_first_and_last_labels_stay_inside():
    """양 끝 라벨이 가운데 정렬이면 시트 밖으로 잘린다 — 안쪽으로 붙여야 한다."""
    import sys as _s
    import pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    html = H._xlabels(["a", "b", "c", "d"], 4)
    assert 'class="first"' in html and 'class="last"' in html


def test_last_data_point_is_always_labeled():
    """마지막 날짜가 안 보이면 '언제까지의 데이터인가'를 화면에서 알 수 없다."""
    import sys as _s
    import pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    labels = [f"07-{d:02d}" for d in range(1, 32)]
    assert labels[-1] in H._xlabels(labels, 31)


def test_labels_never_collide_at_any_data_length():
    """★ 어떤 데이터 길이에서도 라벨이 겹치면 안 된다.

    2026-08-21 실측 회귀: 마지막 지점 라벨을 그냥 덧붙였더니 n=23 에서 마지막
    간격만 8.7% 로 좁아졌다(모바일 30px 인데 라벨 폭이 약 38px). 특정 길이에서만
    터지는 종류라 한두 개 표본으로는 못 잡는다 — 전 구간을 훑는다.

    16% 기준: 모바일(375px, 콘텐츠 약 343px)에서 55px 로, 5글자 라벨(약 33~38px)이
    들어가고도 여유가 남는 최소선.
    """
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    worst, worst_n = 100.0, 0
    for n in range(2, 300):
        html = H._xlabels([str(i) for i in range(n)], n)
        pos = [float(p) for p in re.findall(r"left:([\d.]+)%", html)]
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        if gaps and min(gaps) < worst:
            worst, worst_n = min(gaps), n
    assert worst >= 16.0, (
        f"데이터 {worst_n}개일 때 라벨 간격이 {worst:.2f}% 로 좁아진다 "
        f"(모바일 {worst * 3.43:.0f}px, 라벨 폭 약 38px) — 겹친다")


def test_last_point_labeled_at_any_data_length():
    """마지막 지점은 길이와 무관하게 항상 라벨이 붙어야 한다.

    없으면 "언제까지의 데이터인가"를 화면에서 알 수 없다.
    """
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as H
    for n in (2, 5, 23, 24, 31, 60, 144, 240):
        html = H._xlabels([str(i) for i in range(n)], n)
        pos = [float(p) for p in re.findall(r"left:([\d.]+)%", html)]
        expected = (n - 1) * (100.0 / n) + 50.0 / n
        assert abs(pos[-1] - expected) < 0.01, f"n={n}: 마지막 지점 라벨이 없다"


# ──────────────────────────────────────────────────────────────────────────────
# y축 눈금 — 격자선만 있고 숫자가 없으면 높이를 읽을 수 없다
# ──────────────────────────────────────────────────────────────────────────────

def test_charts_label_the_y_axis():
    """★ 막대 높이가 얼마인지 화면에서 읽을 수 있어야 한다.

    "피크 6건" 이라고 글로 써 둬도 중간 지점이 3인지 4인지는 눈금 없이는 모른다.
    """
    html = _charts_html()
    assert html.count('<div class="yaxis">') == 2, "두 차트 모두 y눈금이 있어야 한다"
    tops = [float(t) for t in re.findall(r'<span style="top:([\d.]+)%">', html)]
    assert tops, "y눈금 값이 하나도 없다"
    assert all(0.0 <= t <= 100.0 for t in tops), "눈금이 차트 밖에 놓였다"


def test_y_axis_is_html_not_stretched_svg_text():
    """y눈금도 SVG 안에 두면 x라벨과 똑같이 가로로 늘어난다."""
    assert "<text" not in _charts_html()


def test_concurrency_y_ticks_are_whole_people():
    """동접은 정수 인원수다 — 존재하지 않는 소수 눈금을 찍으면 안 된다."""
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import load_analysis as LA
    import report_load_html as RL
    from datetime import datetime, timedelta

    def turn(m):
        st = datetime(2026, 8, 22, 0, 0) + timedelta(minutes=m)
        return {"started_at": st, "ts": st + timedelta(milliseconds=90000),
                "duration_ms": 90000, "status": "ok"}

    # 피크가 홀수(5)가 되도록 구성
    html = RL.concurrency_panel({"concurrency": LA.concurrency_timeline(
        [turn(i // 3) for i in range(15)])})
    ticks = re.findall(r'<span style="top:[\d.]+%">([^<]*)</span>', html)
    for t in ticks:
        assert "." not in t, f"동접 눈금에 소수가 찍혔다: {t}"


# ──────────────────────────────────────────────────────────────────────────────
# 세션 상세 — 프로젝트를 클릭했을 때
# ──────────────────────────────────────────────────────────────────────────────

def _session_html(**over):
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_load_html as RL
    sess = {"session_id": "abc-1", "user_id": "u-1", "title": "LED 깜빡이",
            "coding_type": "blockly", "phase": "design",
            "messages": [{"role": "user", "content": "안녕"},
                         {"role": "ai", "content": "첫 줄\n\n둘째 줄"}]}
    sess.update(over)
    return RL.render_session(sess, back_qs="?token=t")


def test_session_page_renders_conversation_readably():
    """★ 원본 JSON 을 그대로 열면 대화가 한 줄로 뭉개져 못 읽는다 — 말한 사람과 내용을 가른다."""
    html = _session_html()
    assert "학습자" in html and "튜터" in html
    assert "대화 2턴" in html


def test_session_page_preserves_line_breaks():
    """★ 실측 회귀: 줄바꿈이 사라져 튜터 답변이 통째로 한 줄이 됐다.

    이스케이프 뒤에 <br> 를 끼워 넣는 방식은 실수 여지가 커서(리터럴 "\\n" 을 찾는
    오타가 실제로 있었다) CSS(white-space:pre-wrap)로 살린다.
    """
    html = _session_html()
    assert "white-space:pre-wrap" in html
    bodies = re.findall(r'<div class="msg-body">(.*?)</div>', html, re.S)
    assert bodies, "대화 본문이 렌더되지 않았다"
    assert any("\n" in b for b in bodies), "줄바꿈이 본문에서 사라졌다"


def test_session_page_escapes_student_text():
    """★ 대화 원문은 학생이 쓴 값이다 — 관리자 화면에서 실행되면 세션 탈취다."""
    html = _session_html(messages=[{"role": "user", "content": "<script>alert(1)</script>"}])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_session_page_escapes_title_and_ids():
    html = _session_html(title='" onload="x', session_id="abc-1", user_id='"><b>')
    s = _scan(html)
    assert s.handlers == [], f"인라인 이벤트 핸들러가 생겼다: {s.handlers}"


def test_session_page_survives_empty_session():
    """아직 아무것도 안 만든 세션도 페이지는 떠야 한다."""
    html = _session_html(messages=[], generated_code={}, learning_notes=[])
    assert "저장된 대화나 산출물이 없습니다" in html


def test_session_page_links_back_to_report():
    """상세에서 돌아갈 길이 없으면 막다른 길이 된다."""
    assert "리포트로" in _session_html()


def test_project_link_points_at_readable_page_not_raw_json():
    """★ 실측 회귀: 제목을 누르면 원본 JSON 이 떴다 — 읽을 수 있는 화면으로 가야 한다."""
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    html = RH._projects_panel(
        {"ok": True, "created": 1, "by_type": [], "matched": 1, "offset": 0, "page_size": 25,
         "items": [{"session_id": "abc-1", "user_id": "u-1", "title": "LED",
                    "coding_type": "blockly", "phase": "design",
                    "created_at": "2026-08-22 09:00:00"}]},
        base_qs={"token": "t"})
    assert "report/session/abc-1" in html
    assert "projects/abc-1.json" not in html, "원본 JSON 링크가 남아 있다"


# ──────────────────────────────────────────────────────────────────────────────
# 상대 링크 — 페이지 깊이가 다르면 같은 href 가 다른 곳으로 풀린다
# ──────────────────────────────────────────────────────────────────────────────

def _pages():
    """실제 URL 과 그 URL 로 서빙되는 HTML 을 짝지어 돌려준다."""
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    import report_load_html as RL

    rep = _report(
        by_day=[{"day": "2026-08-21", "turns": 1, "usd": 0.1, "users": 1, "sessions": 1,
                 "projects": 1, "weighted_tokens": 1, "krw": 1}],
        projects={"ok": True, "created": 1, "by_type": [], "matched": 1, "offset": 0,
                  "page_size": 25,
                  "items": [{"session_id": "s1", "user_id": "u1", "title": "T",
                             "coding_type": "react", "phase": "design",
                             "created_at": "2026-08-21 09:00:00"}]})
    qs = {"token": "t"}
    return [
        ("/agent/reports", RH.render_index(rep, root="", page_qs=qs)),
        ("/agent/report", RH.render(rep, root="", page_qs=qs)),
        ("/agent/report/live", RL.render_live({"turns": 0}, root="../", query="?token=t")),
        ("/agent/report/archive/2026-08-21", RH.render(rep, root="../../", page_qs=qs)),
        ("/agent/report/session/s1",
         RL.render_session({"session_id": "s1", "user_id": "u1", "messages": []},
                           root="../../", back_qs="?token=t")),
    ]


_VALID_PREFIXES = ("/agent/reports", "/agent/report?", "/agent/report/session/",
                   "/agent/report/archive/", "/agent/projects/")


def test_relative_links_resolve_correctly_at_every_page_depth():
    """★ 2026-08-21 실측 회귀: 세션 상세에서 '리포트로'가 에러 페이지로 갔다.

    앱은 자신이 /agent 아래 붙어 있는지 모르므로(프록시가 붙였다 뗀다) 링크를
    절대경로로 못 쓴다. 그런데 **상대경로는 현재 URL 의 디렉터리 기준**이라 깊이마다
    다르게 풀린다:

        /reports            → 디렉터리 /            → "reports"
        /report/live        → 디렉터리 /report/     → "../reports"
        /report/session/xxx → 디렉터리 /report/xxx/ → "../../reports"

    깊이를 안 맞추면 "reports" 가 /report/session/reports 로 풀려, 세션 id 가
    "reports" 인 것으로 해석돼 '세션을 찾을 수 없습니다'가 떴다.
    한 페이지만 검사하면 새 중첩 라우트가 생길 때 같은 일이 반복된다 — 전부 훑는다.
    """
    from urllib.parse import urljoin
    broken = []
    for url, html in _pages():
        for href in {h for h in re.findall(r'href="([^"]+)"', html)
                     if not h.startswith(("http", "#", "?"))}:
            resolved = urljoin(url, href)
            if not resolved.startswith(_VALID_PREFIXES):
                broken.append(f"{url} 에서 {href!r} → {resolved}")
    assert not broken, "상대 링크가 엉뚱한 곳으로 풀린다:\n  " + "\n  ".join(broken)


def test_every_page_offers_a_way_back():
    """상세 화면에서 돌아갈 길이 없으면 막다른 길이 된다."""
    from urllib.parse import urljoin
    for url, html in _pages():
        if url == "/agent/reports":
            continue          # 여기가 목적지다
        backs = [urljoin(url, h) for h in re.findall(r'href="([^"]+)"', html)]
        assert any(b.startswith("/agent/reports") for b in backs), \
            f"{url} 에 리포트로 돌아가는 링크가 없다"


def test_every_internal_link_carries_the_token():
    """★ 2026-08-21 실측 회귀: 일자 링크에 토큰이 빠져 날짜를 누르면 에러가 났다.

    리포트는 fail-closed 다 — 토큰이 없으면 **404**(403 이 아니라 404 라 '왜 안 되지'로
    더 헷갈린다). 그래서 화면 안의 모든 내부 링크는 토큰을 실어야 한다.
    한 곳만 고치면 다음 링크를 추가할 때 같은 일이 반복되므로 전부 훑는다.

    예외: 쿼리만 바꾸는 링크(?poff=…)는 브라우저가 현재 경로를 유지하지만 쿼리는
    통째로 갈아치우므로, 그쪽도 토큰을 포함해야 한다 — 함께 검사한다.
    """
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH

    rep = _report(
        by_day=[{"day": d, "turns": 1, "usd": 0.1, "users": 1, "sessions": 1,
                 "projects": 1, "weighted_tokens": 1, "krw": 1}
                for d in ("2026-08-21", "2026-08-20")],
        projects={"ok": True, "created": 1, "by_type": [], "matched": 60, "offset": 0,
                  "page_size": 25,
                  "items": [{"session_id": "s1", "user_id": "u1", "title": "T",
                             "coding_type": "react", "phase": "design",
                             "created_at": "2026-08-21 09:00:00"}]})
    qs = {"token": "luxrobo"}
    pages = {
        "render_index": RH.render_index(rep, token="luxrobo", page_qs=qs,
                                        confirmed={"2026-08-21": {"day": "2026-08-21"}}),
        "render": RH.render(rep, token="luxrobo", page_qs=qs),
        # ★ 확정본 화면은 page_qs 를 **안 넘긴다.** 그래서 페이저 링크에서 토큰이
        #   사라져 '다음 페이지'가 404 였다(2026-08-21 실측). 호출부가 잊어도
        #   렌더러가 토큰을 채우는지 이 케이스로 고정한다.
        "render(확정본 · page_qs 없음)": RH.render(
            rep, title="일별 리포트 (확정본)", show_form=False,
            token="luxrobo", root="../../"),
        "render_index(page_qs 없음)": RH.render_index(rep, token="luxrobo"),
    }
    missing = []
    for name, html in pages.items():
        for href in {h for h in re.findall(r'href="([^"]+)"', html)
                     if not h.startswith(("http", "#"))}:
            if "token=" not in href:
                missing.append(f"{name}: {href}")
    assert not missing, ("토큰 없는 내부 링크는 404 가 된다:\n  "
                         + "\n  ".join(sorted(missing)))


# ──────────────────────────────────────────────────────────────────────────────
# 탭 — 화면이 길어지면 어디를 봐야 할지 잃는다
# ──────────────────────────────────────────────────────────────────────────────

def _tabbed(**over):
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    rep = _report(
        load={"ok": True, "turns": 3, "success": 3, "errors": 0, "aborted": 0,
              "fail_pct": 0.0,
              "duration": {"p50": 1, "p95": 2, "max": 3, "avg": 2, "n": 3},
              "ttft": {"p50": 1, "p95": 2, "max": 3, "avg": 2, "n": 3},
              "concurrency": {"buckets": [], "peak": 1, "peak_at": "", "measured": 3,
                              "skipped": 0},
              "by_concurrency": [], "by_intent": [], "by_outcome": [], "by_replica": [],
              "by_error": [], "reuse_curve": [], "slowest": [], "clients": {},
              "points": {}, "resources": {}, "ops": {"ok": True, "by_kind": [],
                                                     "recent": [], "total": 0}},
        projects={"ok": True, "created": 7, "by_type": [], "matched": 7, "offset": 0,
                  "page_size": 25, "items": []})
    rep.update(over)
    return RH.render(rep, token="t", page_qs={"token": "t"})


def test_report_is_split_into_tabs():
    """★ 한 화면에 다 쏟으면 스크롤만 길어지고 어디를 봐야 할지 잃는다."""
    html = _tabbed()
    labels = re.findall(r'<label for="t\d">([^<]*)', html)
    assert len(labels) >= 3, f"탭이 너무 적다: {labels}"
    assert labels[0] == "요약", "첫 탭은 '버텼나'에 답하는 요약이어야 한다"


def test_first_tab_is_open_by_default():
    """아무 탭도 안 열려 있으면 빈 화면으로 보인다."""
    assert 'id="t1" checked' in _tabbed()


def test_tabs_need_no_javascript():
    """★ CSP 가 엄격한 환경·오프라인·몇 달 뒤 확정본 열람에서도 동작해야 한다."""
    html = _tabbed()
    assert "<script" not in html.replace("<script>window", "<SAFE"), "탭에 JS 가 들어갔다"
    assert html.count('type="radio" name="tab"') == html.count('class="tabpane"')


def test_empty_tabs_are_dropped():
    """내용 없는 탭이 남으면 눌렀을 때 빈 화면이 나온다."""
    html = _tabbed()
    n_panes = html.count('class="tabpane"')
    n_labels = len(re.findall(r'<label for="t\d">', html))
    assert n_panes == n_labels


def test_tab_labels_carry_counts_where_useful():
    """프로젝트 탭은 개수를 라벨에 실어 열지 않고도 규모를 알 수 있게 한다."""
    assert re.search(r'<label for="t\d">프로젝트<span class=n>7</span>', _tabbed())


def test_tab_controls_are_keyboard_reachable():
    """라디오를 display:none 으로 숨기면 키보드로 탭을 못 옮긴다."""
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    css = RH._CSS
    assert ".tabs > input[type=radio]{position:absolute; opacity:0" in css
    assert "display:none" not in css.split(".tabs > input")[1].split("}")[0]
    assert ":focus-visible ~ .tabbar label" in css, "포커스가 보이지 않으면 못 쓴다"


def test_print_shows_every_tab():
    """종이에는 클릭이 없다 — 인쇄하면 전부 펼쳐져야 한다."""
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    assert "@media print" in RH._CSS and ".tabpane{display:block !important}" in RH._CSS


def test_hint_markup_is_not_escaped_but_data_is():
    """★ 설명의 <b> 가 글자로 보이던 회귀.

    _table 의 hint 는 이스케이프된다(데이터가 섞여도 안전). 강조는 hint_html 로만
    넣고, 그쪽에 들어가는 데이터는 호출부가 직접 escape 해야 한다.
    """
    import pathlib as _p
    import sys as _s
    _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "scripts"))
    import report_html as RH
    safe = RH._table("t", "<b>데이터</b>", ["a"], [("1",)], empty="")
    assert "&lt;b&gt;" in safe, "hint 는 이스케이프돼야 한다(데이터 통로)"
    trusted = RH._table("t", "", ["a"], [("1",)], empty="", hint_html="<b>강조</b>")
    assert "<b>강조</b>" in trusted
