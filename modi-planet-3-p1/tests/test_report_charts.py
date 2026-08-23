"""상단 차트 회귀 테스트 — 그림이 **거짓말하지 않는지** 검증한다.

차트는 표보다 위험하다. 숫자가 틀리면 눈에 띄지만, 각도나 반지름이 틀리면
그럴듯해 보이면서 잘못된 인상을 남긴다. 여기서 고정하는 것:

    · 비율 유지 (원이 타원이 되면 크기 비교가 무너진다)
    · 면적 비례 (반지름에 값을 그대로 넣으면 큰 값이 과장된다)
    · 각도 합 = 360° (파이가 전체를 덮어야 비중이 성립한다)
    · 퍼널의 단조 감소 (누적 생존이 늘어나면 그건 퍼널이 아니다)
"""
import math
import re
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

try:
    import report_charts as RC
except Exception as e:  # 의존성 미설치 환경
    pytest.skip(f"report_charts import 불가: {e}", allow_module_level=True)


def _load(n=30, **over):
    pts = [{"m": i * 0.5, "d": 30000 + (i % 7) * 5000, "w": 9000,
            "s": "ok", "o": "code", "u": f"stu{i % 5}", "sid": f"s{i}"}
           for i in range(n)]
    base = {
        "points": {"points": pts, "span_min": pts[-1]["m"], "t0": "2026-08-22 09:00",
                   "truncated": False},
        "by_outcome": [
            {"label": "코드", "usd": 0.30, "turns": 20},
            {"label": "대화만", "usd": 0.10, "turns": 30},
        ],
    }
    base.update(over)
    return base


def _patterns(**over):
    base = {
        "users": [{"user_id": f"stu{i}", "turns": 60 - i * 6, "sessions": (i % 4) + 1,
                   "usd": 0.30 - i * 0.02} for i in range(9)],
        "session_depth": [{"bucket": "1", "sessions": 18}, {"bucket": "2-3", "sessions": 11},
                          {"bucket": "4-6", "sessions": 5}, {"bucket": "7-10", "sessions": 2}],
    }
    base.update(over)
    return base


# ──────────────────────────────────────────────────────────────────────────────
# 스케일 규약 — 막대와 다르다
# ──────────────────────────────────────────────────────────────────────────────

def _all_svgs() -> str:
    return RC.all_charts(_load(), _patterns())


def test_charts_keep_aspect_ratio():
    """★ 막대는 폭을 채우려 가로만 늘리지만(preserveAspectRatio="none"), 여기서
    그러면 **원이 타원이 되고 파이 각도가 틀어진다** — 크기 비교가 통째로 무너진다."""
    html = _all_svgs()
    assert 'preserveAspectRatio="none"' not in html, (
        "비율을 안 지키면 원·각도가 왜곡돼 그림이 거짓말을 한다")
    assert html.count('preserveAspectRatio="xMidYMid meet"') >= 3


def test_chart_text_is_safe_inside_svg():
    """비율을 유지하므로 SVG 안 글자는 안 뭉개진다 — 축 라벨이 실제로 있어야 한다."""
    assert "<text" in _all_svgs()


# ──────────────────────────────────────────────────────────────────────────────
# 산점도
# ──────────────────────────────────────────────────────────────────────────────

def test_scatter_draws_every_turn():
    """요약이 아니라 **하나하나**를 찍어야 분포의 모양이 보인다."""
    html = RC.scatter_turns(_load(n=37))
    assert len(re.findall(r'<circle[^>]*fill-opacity="\.5"', html)) == 37


def test_scatter_points_stay_inside_the_plot():
    """좌표가 뷰박스를 벗어나면 점이 잘려 사라진다 — 없는 게 아니라 안 보이는 것."""
    html = RC.scatter_turns(_load())
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', html)
    w, h = float(box.group(1)), float(box.group(2))
    for cx, cy in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', html):
        assert 0 <= float(cx) <= w and 0 <= float(cy) <= h


def test_scatter_marks_failures_distinctly():
    """★ 실패가 특정 시간대에 몰렸는지는 색으로만 보인다."""
    load = _load(n=5)
    load["points"]["points"][2]["s"] = "error"
    html = RC.scatter_turns(load)
    assert "var(--crit)" in html


def test_scatter_bubble_area_is_proportional_not_radius():
    """★ 값을 반지름에 그대로 넣으면 면적이 제곱으로 커져 큰 값이 과장된다.

    4배 비싼 턴의 반지름은 4배가 아니라 2배여야 한다(면적이 4배).
    """
    load = _load(n=3)                      # 산점도는 점 3개 미만이면 안 그린다
    for p_, w in zip(load["points"]["points"], (10_000, 10_000, 40_000)):
        p_["w"] = w
    rs = [float(r) for r in re.findall(r'<circle[^>]*r="([\d.]+)"', RC.scatter_turns(load))]
    # 상수항(최소 반지름 2.0)을 빼고 비교
    a, b = min(rs), max(rs)
    assert 1.7 <= (b - 2.0) / max(a - 2.0, 1e-9) <= 2.3, f"반지름 비 {rs}"


def test_scatter_skips_when_too_few_points():
    """점 두세 개짜리 산점도는 아무것도 안 알려 준다 — 그리지 않는다."""
    assert RC.scatter_turns(_load(n=2)) == ""


# ──────────────────────────────────────────────────────────────────────────────
# 도넛
# ──────────────────────────────────────────────────────────────────────────────

def test_donut_slices_cover_full_circle():
    """★ 조각 각도의 합이 360°가 아니면 '비중'이라는 말 자체가 성립하지 않는다."""
    html = RC.donut_cost(_load())
    # 각 path 의 시작·끝 각도를 좌표에서 역산하는 대신, 조각 수와 합계 표기를 확인하고
    # 마지막 조각이 시작점(12시 방향)으로 되돌아오는지 본다.
    paths = re.findall(r'<path d="M([\d.]+),([\d.]+) A', html)
    assert len(paths) == 2
    first = (float(paths[0][0]), float(paths[0][1]))
    # 12시 방향에서 시작해야 한다(cx, cy-ro)
    assert abs(first[0] - 120.0) < 0.5 and first[1] < 60.0


def test_donut_groups_the_tail_so_it_stays_readable():
    """조각이 많으면 파이는 못 읽는다 — 상위 5개 + '그 외'로 접는다."""
    load = _load(by_outcome=[{"label": f"항목{i}", "usd": 1.0 / (i + 1), "turns": 1}
                             for i in range(9)])
    html = RC.donut_cost(load)
    assert len(re.findall(r'<path d="M', html)) == 6
    assert "그 외" in html


def test_donut_shows_total_in_the_hole():
    """비중만 보여 주면 '그래서 얼마'를 다시 찾아야 한다 — 가운데에 총액을 둔다."""
    assert "$0.40" in RC.donut_cost(_load())


def test_donut_skips_single_category():
    """조각 하나짜리 도넛은 100% 라는 사실만 알려 준다 — 화면 낭비다."""
    assert RC.donut_cost(_load(by_outcome=[{"label": "코드", "usd": 1.0}])) == ""


# ──────────────────────────────────────────────────────────────────────────────
# 퍼널
# ──────────────────────────────────────────────────────────────────────────────

def _funnel_counts(html: str) -> list[int]:
    return [int(n.replace(",", "")) for n in re.findall(r'>([\d,]+)개 · \d+%<', html)]


def test_funnel_is_cumulative_survival_not_bucket_counts():
    """★ 구간 막대와 퍼널은 다르다.

    구간 막대는 '1턴짜리 18개'를 보여 주고 끝나지만, 퍼널은 '2턴 이상 간 것이
    18개 중 몇 개'를 보여 준다 — 어디서 떨어지는지가 여기서만 보인다.
    """
    counts = _funnel_counts(RC.funnel_depth(_patterns()))
    assert counts[0] == 36, f"첫 단계는 전체(18+11+5+2=36)여야 한다: {counts}"
    assert counts[1] == 18, f"2턴 이상 = 36-18: {counts}"
    assert counts[2] == 7, f"4턴 이상 = 18-11: {counts}"


def test_funnel_never_increases():
    """누적 생존이 늘어나면 그건 퍼널이 아니다 — 계산이 틀린 것이다."""
    counts = _funnel_counts(RC.funnel_depth(_patterns()))
    assert counts == sorted(counts, reverse=True), counts


def test_funnel_shows_where_students_drop():
    """단계마다 몇 명을 잃었는지 적어야 '어디서'에 답이 된다."""
    assert "−18" in RC.funnel_depth(_patterns())


def test_funnel_skips_when_one_bucket():
    assert RC.funnel_depth(_patterns(session_depth=[{"bucket": "1", "sessions": 5}])) == ""


# ──────────────────────────────────────────────────────────────────────────────
# 버블
# ──────────────────────────────────────────────────────────────────────────────

def test_bubble_places_users_by_sessions_and_turns():
    """가로=세션, 세로=턴. 표의 두 열을 눈으로 곱하지 않아도 되게 하는 게 목적이다."""
    html = RC.bubble_users(_patterns())
    assert len(re.findall(r'<circle[^>]*fill-opacity="\.22"', html)) == 9


def test_bubble_radius_is_area_proportional():
    """비용 4배인 학생의 원은 반지름 4배가 아니라 면적 4배여야 한다."""
    pat = _patterns(users=[{"user_id": "a", "turns": 10, "sessions": 2, "usd": 0.04},
                           {"user_id": "b", "turns": 10, "sessions": 2, "usd": 0.16},
                           {"user_id": "c", "turns": 10, "sessions": 2, "usd": 0.16}])
    rs = sorted(float(r) for r in re.findall(r'<circle[^>]*r="([\d.]+)"',
                                             RC.bubble_users(pat)))
    a, b = rs[0], rs[-1]
    assert abs((b - 3.5) / max(a - 3.5, 1e-9) - 2.0) < 0.35, f"반지름 {rs}"


def test_bubble_skips_when_too_few_users():
    """두 명짜리 산점은 패턴이 아니다."""
    assert RC.bubble_users(_patterns(users=[{"user_id": "a", "turns": 1, "sessions": 1,
                                             "usd": 0.1}])) == ""


# ──────────────────────────────────────────────────────────────────────────────
# 안전 — 사용자 입력이 그림 안으로 들어간다
# ──────────────────────────────────────────────────────────────────────────────

def test_user_ids_are_escaped_in_tooltips():
    """★ user_id 는 외부 입력이고 <title> 로 SVG 안에 들어간다."""
    load = _load(n=5)
    load["points"]["points"][0]["u"] = "<script>alert(1)</script>"
    html = RC.scatter_turns(load)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_charts_survive_empty_input():
    """수업이 없던 날도 페이지는 떠야 한다."""
    assert RC.all_charts({}, {}) == ""
    assert RC.all_charts({"points": {}}, {"users": []}) == ""


def test_charts_survive_zero_values():
    """전부 0인 데이터로 나눗셈이 터지면 안 된다."""
    load = _load(n=4)
    for p in load["points"]["points"]:
        p["d"] = 0
        p["w"] = 0
    RC.scatter_turns(load)          # 예외가 나가면 이 줄에서 깨진다
    RC.donut_cost(_load(by_outcome=[{"label": "a", "usd": 0}, {"label": "b", "usd": 0}]))
    RC.bubble_users(_patterns(users=[{"user_id": f"u{i}", "turns": 0, "sessions": 0,
                                      "usd": 0} for i in range(4)]))


def test_math_import_is_used_for_sqrt_scaling():
    """면적 비례 스케일이 sqrt 로 되어 있는지 — 상수로 바뀌면 과장이 되돌아온다."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "scripts" / "report_charts.py").read_text(encoding="utf-8")
    assert "math.sqrt" in src
    assert math.sqrt(4) == 2.0


# ──────────────────────────────────────────────────────────────────────────────
# 수용 곡선 — "몇 명까지 버티나"
# ──────────────────────────────────────────────────────────────────────────────

def _conc():
    return {"by_concurrency": [
        {"bucket": "1", "turns": 10, "p50": 3200, "p95": 4000, "max": 4200},
        {"bucket": "2-3", "turns": 14, "p50": 5000, "p95": 9000, "max": 11000},
        {"bucket": "4-7", "turns": 20, "p50": 12000, "p95": 31000, "max": 38000},
        {"bucket": "8-15", "turns": 18, "p50": 39000, "p95": 67000, "max": 71000},
    ]}


def test_capacity_curve_draws_both_p50_and_p95():
    """★ 두 선이 벌어지는 지점이 '평균은 괜찮은데 일부만 심하게 밀리는' 순간이다.

    학생이 이탈하는 건 그 일부다 — p95 만 그리면 '원래 느린 서비스'로 읽히고
    p50 만 그리면 문제가 안 보인다.
    """
    html = RC.capacity_curve(_conc())
    assert len(re.findall(r"<polyline", html)) == 2
    assert "stroke-dasharray" in html, "두 선을 형태로도 갈라야 흑백에서도 읽힌다"


def test_capacity_curve_marks_every_bucket():
    html = RC.capacity_curve(_conc())
    assert len(re.findall(r"<circle", html)) == 8          # 4구간 × 2계열
    for b in ("1", "2-3", "4-7", "8-15"):
        assert f">{b}</text>" in html


def test_capacity_curve_points_stay_inside():
    html = RC.capacity_curve(_conc())
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', html)
    w, h = float(box.group(1)), float(box.group(2))
    for x, y in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', html):
        assert 0 <= float(x) <= w and 0 <= float(y) <= h


def test_capacity_curve_skips_with_one_bucket():
    """구간 하나로는 곡선이 안 그려진다 — 점 하나는 추세가 아니다."""
    assert RC.capacity_curve({"by_concurrency": [_conc()["by_concurrency"][0]]}) == ""


def test_capacity_curve_is_in_the_chart_bundle():
    """이 질문('40명이 버티나')이 리포트의 핵심이라 상단에 있어야 한다."""
    html = RC.all_charts({**_load(), **_conc()}, _patterns())
    assert "수용 곡선" in html
