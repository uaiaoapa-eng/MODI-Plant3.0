"""리포트 상단 차트 — 표가 답하지 못하는 것만 그린다.

표와 막대는 "얼마"를 답한다. 여기 있는 차트들은 다른 걸 답한다:

    산점도  분포의 **모양** — 고르게 느린가, 특정 구간만 터졌나, 한 명만 튀나
    버블    두 축 + 크기의 **동시** 관계 — 많이 쓰는 사람이 깊게도 파는가
    도넛    비중 — 무엇이 돈을 먹는가
    퍼널    단계별 **이탈** — 시작한 학생 중 몇 %가 끝까지 갔나

장식으로 차트를 늘리지 않는다. 표로 이미 답한 것을 다시 그리면 화면만 길어지고
읽는 사람은 어디를 봐야 할지 잃는다.

── 스케일 규약 (중요) ────────────────────────────────────────────────────────
막대 차트(report_html._chart)는 폭을 꽉 채워야 해서 preserveAspectRatio="none" 으로
가로만 늘린다. 그래서 글자를 SVG 안에 두면 뭉개진다(2026-08-21 실측: 가로 10배).

여기 차트들은 **비율을 유지**한다(xMidYMid meet). 원이 타원이 되면 안 되고,
각도가 틀어지면 파이가 거짓말을 하기 때문이다. 비율이 유지되므로 **글자를 SVG 안에
둬도 안전하다** — 두 종류가 다른 규칙을 따른다는 걸 알고 봐야 한다.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def _h():
    from importlib import import_module
    return import_module("report_html")


# 상태·결과물 색은 액센트가 아니라 **의미** 색이다 — 액센트와 섞지 않는다.
_SERIES = ("var(--accent)", "var(--warn)", "var(--ok)", "var(--ink-3)",
           "var(--crit)", "var(--line-2)")


def _fmt_ms(ms: float) -> str:
    if ms >= 60_000:
        return f"{ms / 60_000:.0f}분"
    if ms >= 1000:
        return f"{ms / 1000:.0f}초"
    return f"{ms:.0f}ms"


def _panel(title: str, hint: str, body: str) -> str:
    H = _h()
    return (f'<section class="panel"><header class="panel-head">'
            f'<h2>{H._e(title)}</h2><p>{hint}</p></header>{body}</section>')


# ─────────────────────────────────────────────────────────────────────────────
# 산점도 — 턴 하나하나
# ─────────────────────────────────────────────────────────────────────────────

def scatter_turns(load: Mapping[str, Any]) -> str:
    """가로 = 수업 경과 시간, 세로 = 응답 소요, 점 = 턴 하나.

    ★ 이 리포트에서 가장 많은 걸 알려 주는 그림이다. p95 하나로는
      "전 구간 고르게 느림" 과 "특정 10분만 폭발" 이 구별되지 않는데, 대응은 정반대다
      (전자는 용량 문제, 후자는 그 시간에 무슨 일이 있었는지 찾아야 한다).

    실패한 턴은 색으로 갈라 놓는다 — 실패가 특정 시간대에 몰렸는지가 즉시 보인다.
    """
    H = _h()
    pts_block = (load or {}).get("points") or {}
    pts: Sequence[Mapping[str, Any]] = pts_block.get("points") or []
    if len(pts) < 3:
        return ""

    span = float(pts_block.get("span_min") or 0) or 1.0
    ymax = max(float(p["d"]) for p in pts) or 1.0

    W, Hh = 720.0, 260.0
    L, R, T, B = 52.0, 14.0, 14.0, 30.0
    pw, ph = W - L - R, Hh - T - B

    def X(m):
        return L + (float(m) / span) * pw

    def Y(d):
        return T + ph - (float(d) / ymax) * ph

    dots = ""
    for p in pts:
        fill = "var(--crit)" if p.get("s") != "ok" else (
            "var(--accent)" if p.get("o") in ("code", "blockly") else "var(--ink-3)")
        # 반지름으로 비용 기여를 싣는다 — 면적이 값에 비례하도록 sqrt 를 쓴다
        # (반지름에 그대로 비례시키면 큰 값이 시각적으로 과장된다).
        r = 2.0 + math.sqrt(max(float(p.get("w") or 0), 0)) / 120.0
        r = min(r, 9.0)
        dots += (f'<circle cx="{X(p["m"]):.1f}" cy="{Y(p["d"]):.1f}" r="{r:.1f}" '
                 f'fill="{fill}" fill-opacity=".5" stroke="{fill}" stroke-opacity=".8" '
                 f'stroke-width=".7"><title>{H._e(p.get("u") or "")} · '
                 f'{_fmt_ms(p["d"])} · {H._comma(p.get("w"))}토큰</title></circle>')

    grid, ylab = "", ""
    for f in (0.0, 0.5, 1.0):
        y = T + ph - f * ph
        grid += (f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" '
                 f'stroke="var(--line)" stroke-width="1"/>')
        ylab += (f'<text x="{L - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
                 f'font-size="11" fill="var(--ink-3)">{_fmt_ms(ymax * f)}</text>')
    xlab = ""
    for f in (0.0, 0.5, 1.0):
        xlab += (f'<text x="{X(span * f):.1f}" y="{Hh - 10:.1f}" text-anchor="middle" '
                 f'font-size="11" fill="var(--ink-3)">{span * f:.0f}분</text>')

    trunc = ("" if not pts_block.get("truncated") else
             '<p class="muted">⚠ 턴이 많아 고르게 추려 그렸습니다(분포는 유지).</p>')
    legend = ('<p class="muted">'
              '<span style="color:var(--accent)">●</span> 산출물 생성 · '
              '<span style="color:var(--ink-3)">●</span> 대화 · '
              '<span style="color:var(--crit)">●</span> 실패 &nbsp;|&nbsp; '
              '점 크기 = 그 턴의 비용</p>')

    svg = (f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="턴별 응답시간 산점도" class="fig">'
           f'{grid}{ylab}{xlab}{dots}'
           f'<line x1="{L}" y1="{T + ph:.1f}" x2="{W - R}" y2="{T + ph:.1f}" '
           f'stroke="var(--rule)" stroke-width="1.2"/></svg>')

    return _panel(
        "턴별 응답시간 — 하나하나",
        "가로는 수업 시작부터의 경과, 세로는 그 턴이 걸린 시간입니다. "
        "<b>p95 하나로는 '전 구간 고르게 느림'과 '특정 10분만 폭발'이 구별되지 않습니다</b> — "
        "대응이 정반대라(용량 문제 vs 그 시각에 무슨 일) 분포를 직접 봐야 합니다. "
        f"기준 시각 {H._e(pts_block.get('t0') or '')} KST.",
        f'<div class="figwrap">{svg}</div>{legend}{trunc}')


# ─────────────────────────────────────────────────────────────────────────────
# 버블 — 사용자
# ─────────────────────────────────────────────────────────────────────────────

def bubble_users(patterns: Mapping[str, Any], *, top: int = 40) -> str:
    """가로 = 세션 수, 세로 = 턴 수, 크기 = 비용.

    "많이 쓰는 학생"과 "깊게 파는 학생"은 다르다. 세션 1개에 40턴이면 한 프로젝트에
    매달린 것이고(막혔을 가능성), 세션 20개에 40턴이면 여러 개를 얕게 건드린 것이다.
    표로는 두 열을 눈으로 곱해 봐야 하는데, 위치로 두면 즉시 갈린다.
    """
    H = _h()
    users = [u for u in ((patterns or {}).get("users") or [])
             if H._n(u.get("turns"))]
    if len(users) < 3:
        return ""
    users = sorted(users, key=lambda u: H._n(u.get("turns")), reverse=True)[:top]

    xmax = max(H._n(u.get("sessions")) for u in users) or 1
    ymax = max(H._n(u.get("turns")) for u in users) or 1
    wmax = max(H._f(u.get("usd")) for u in users) or 1.0

    W, Hh = 720.0, 300.0
    L, R, T, B = 52.0, 16.0, 16.0, 34.0
    pw, ph = W - L - R, Hh - T - B

    bubbles = ""
    for u in users:
        x = L + (H._n(u.get("sessions")) / xmax) * pw
        y = T + ph - (H._n(u.get("turns")) / ymax) * ph
        r = 3.5 + math.sqrt(max(H._f(u.get("usd")), 0) / wmax) * 14.0
        bubbles += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
                    f'fill="var(--accent)" fill-opacity=".22" stroke="var(--accent)" '
                    f'stroke-width="1"><title>{H._e(H._short_user(u.get("user_id")))} · '
                    f'{H._comma(u.get("turns"))}턴 · 세션 {H._comma(u.get("sessions"))} · '
                    f'{H._usd(u.get("usd"))}</title></circle>')

    grid = ylab = ""
    for f in (0.0, 0.5, 1.0):
        y = T + ph - f * ph
        grid += (f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" '
                 f'stroke="var(--line)" stroke-width="1"/>')
        ylab += (f'<text x="{L - 8}" y="{y + 3.5:.1f}" text-anchor="end" font-size="11" '
                 f'fill="var(--ink-3)">{ymax * f:.0f}턴</text>')
    xlab = ""
    for f in (0.0, 0.5, 1.0):
        xlab += (f'<text x="{L + f * pw:.1f}" y="{Hh - 12:.1f}" text-anchor="middle" '
                 f'font-size="11" fill="var(--ink-3)">세션 {xmax * f:.0f}</text>')

    svg = (f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="사용자별 세션·턴 버블차트" class="fig">'
           f'{grid}{ylab}{xlab}{bubbles}'
           f'<line x1="{L}" y1="{T + ph:.1f}" x2="{W - R}" y2="{T + ph:.1f}" '
           f'stroke="var(--rule)" stroke-width="1.2"/></svg>')
    return _panel(
        "학생별 사용 패턴",
        "가로는 만든 프로젝트 수, 세로는 대화 턴 수, 원 크기는 비용입니다. "
        "<b>오른쪽 아래</b>는 여러 개를 얕게 건드린 학생, <b>왼쪽 위</b>는 하나에 매달린 "
        "학생입니다(막혔을 가능성). 위로 튀는 원 하나는 쿼터를 다 먹는 학생입니다.",
        f'<div class="figwrap">{svg}</div>')


# ─────────────────────────────────────────────────────────────────────────────
# 도넛 — 비용 비중
# ─────────────────────────────────────────────────────────────────────────────

def donut_cost(load: Mapping[str, Any], *, key: str = "by_outcome") -> str:
    """무엇이 돈을 먹었나.

    파이는 항목이 많으면 못 읽는다 — 상위 5개만 그리고 나머지는 묶는다.
    도넛으로 가운데를 비워 총액을 넣는다(비중과 총액을 한 번에 읽게).
    """
    H = _h()
    rows = [r for r in ((load or {}).get(key) or []) if H._f(r.get("usd")) > 0]
    if len(rows) < 2:
        return ""
    rows = sorted(rows, key=lambda r: H._f(r.get("usd")), reverse=True)
    if len(rows) > 5:
        rest = sum(H._f(r.get("usd")) for r in rows[5:])
        rows = rows[:5] + [{"label": "그 외", "usd": rest, "turns": 0}]
    total = sum(H._f(r.get("usd")) for r in rows) or 1.0

    W = Hh = 240.0
    cx = cy = Hh / 2
    ro, ri = 96.0, 58.0
    a0 = -math.pi / 2
    arcs, legend = "", ""
    for i, r in enumerate(rows):
        frac = H._f(r.get("usd")) / total
        a1 = a0 + frac * 2 * math.pi
        large = 1 if (a1 - a0) > math.pi else 0
        x0, y0 = cx + ro * math.cos(a0), cy + ro * math.sin(a0)
        x1, y1 = cx + ro * math.cos(a1), cy + ro * math.sin(a1)
        xi1, yi1 = cx + ri * math.cos(a1), cy + ri * math.sin(a1)
        xi0, yi0 = cx + ri * math.cos(a0), cy + ri * math.sin(a0)
        color = _SERIES[i % len(_SERIES)]
        arcs += (f'<path d="M{x0:.1f},{y0:.1f} A{ro},{ro} 0 {large} 1 {x1:.1f},{y1:.1f} '
                 f'L{xi1:.1f},{yi1:.1f} A{ri},{ri} 0 {large} 0 {xi0:.1f},{yi0:.1f} Z" '
                 f'fill="{color}" fill-opacity=".85"><title>{H._e(r.get("label"))} '
                 f'{H._usd(r.get("usd"))} ({frac * 100:.0f}%)</title></path>')
        legend += (f'<div class="lg-row"><span class="lg-dot" style="background:{color}">'
                   f'</span><span class="lg-name">{H._e(r.get("label"))}</span>'
                   f'<span class="lg-val">{H._usd(r.get("usd"))}</span>'
                   f'<span class="lg-pct">{frac * 100:.0f}%</span></div>')
        a0 = a1

    svg = (f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="결과물별 비용 비중" class="fig fig-sm">{arcs}'
           f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" font-size="22" '
           f'font-weight="700" fill="var(--ink)">{H._usd(total)}</text>'
           f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" font-size="11" '
           f'fill="var(--ink-3)">합계</text></svg>')
    return _panel(
        "무엇이 돈을 먹었나",
        "결과물 종류별 비용 비중입니다. 한 조각이 지배적이면 그쪽을 재사용으로 "
        "돌리는 것이 가장 큰 절감입니다.",
        f'<div class="fig-split"><div class="figwrap">{svg}</div>'
        f'<div class="legend">{legend}</div></div>')


# ─────────────────────────────────────────────────────────────────────────────
# 퍼널 — 대화 깊이 이탈
# ─────────────────────────────────────────────────────────────────────────────

def funnel_depth(patterns: Mapping[str, Any]) -> str:
    """프로젝트를 시작한 학생 중 몇 %가 계속 갔나.

    session_depth 는 "1턴 / 2-3턴 / 4-6턴 …" 구간별 세션 수다. 이걸 **누적 생존**으로
    바꾸면 진짜 퍼널이 된다 — 2턴 이상 간 세션, 4턴 이상 간 세션…
    구간 막대로 보면 "1턴이 제일 많네" 로 끝나지만, 퍼널로 보면 **어디서 떨어지는지**가
    보인다. 1턴에서 급락하면 첫 응답이 기대에 못 미쳤다는 뜻이다.
    """
    H = _h()
    buckets = [b for b in ((patterns or {}).get("session_depth") or [])]
    if len(buckets) < 2:
        return ""
    order = ["1", "2-3", "4-6", "7-10", "11+"]
    got = {str(b.get("bucket")): H._n(b.get("sessions")) for b in buckets}
    seq = [(b, got.get(b, 0)) for b in order if b in got]
    if len(seq) < 2:
        return ""

    total = sum(v for _, v in seq) or 1
    # 누적 생존 — "이 구간 **이상** 간 세션"
    surv, acc = [], total
    labels = ["1턴 이상", "2턴 이상", "4턴 이상", "7턴 이상", "11턴 이상"]
    for i, (_, v) in enumerate(seq):
        surv.append((labels[i] if i < len(labels) else seq[i][0], acc))
        acc -= v

    W = 720.0
    row_h, gap = 34.0, 8.0
    Hh = len(surv) * (row_h + gap) + 10
    bars = ""
    for i, (lab, n) in enumerate(surv):
        frac = n / total
        w = max(frac * (W - 190), 2.0)
        y = 6 + i * (row_h + gap)
        drop = ""
        if i:
            prev = surv[i - 1][1] or 1
            lost = prev - n
            if lost:
                drop = (f'<text x="{W - 6}" y="{y + row_h / 2 + 4:.1f}" text-anchor="end" '
                        f'font-size="11" fill="var(--crit)">−{H._comma(lost)}</text>')
        bars += (
            f'<text x="0" y="{y + row_h / 2 + 4:.1f}" font-size="12.5" '
            f'font-weight="600" fill="var(--ink)">{H._e(lab)}</text>'
            f'<rect x="118" y="{y:.1f}" width="{w:.1f}" height="{row_h:.1f}" rx="2" '
            f'fill="var(--accent)" fill-opacity="{0.85 - i * 0.13:.2f}"/>'
            f'<text x="{118 + w + 8:.1f}" y="{y + row_h / 2 + 4:.1f}" font-size="12" '
            f'fill="var(--ink-2)">{H._comma(n)}개 · {frac * 100:.0f}%</text>{drop}')

    svg = (f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="대화 깊이 퍼널" class="fig">{bars}</svg>')
    return _panel(
        "어디서 그만두나",
        "프로젝트를 시작한 뒤 몇 턴까지 갔는지의 <b>누적 생존</b>입니다. "
        "구간 막대는 '1턴이 제일 많네'로 끝나지만 퍼널은 <b>어디서 떨어지는지</b>를 "
        "보여 줍니다 — 1→2턴에서 급락하면 첫 응답이 기대에 못 미친 것입니다.",
        f'<div class="figwrap">{svg}</div>')


def chart_groups(load: Mapping[str, Any],
                 patterns: Mapping[str, Any] | None = None) -> dict[str, str]:
    """탭별 차트. 산점도·수용곡선은 '버텼나'(요약)에, 나머지는 성격에 맞는 탭에."""
    return {
        "summary": scatter_turns(load) + capacity_curve(load),
        "cost": donut_cost(load),
        "users": bubble_users(patterns or {}) + funnel_depth(patterns or {}),
    }


def all_charts(load: Mapping[str, Any], patterns: Mapping[str, Any] | None = None) -> str:
    """상단 차트 묶음 — 분포 → 사람 → 비중 → 이탈.

    표보다 먼저 둔다. 숫자를 읽기 전에 '모양'을 보면 어느 표를 봐야 할지가 정해진다.
    """
    return "".join([
        scatter_turns(load),
        capacity_curve(load),
        bubble_users(patterns or {}),
        donut_cost(load),
        funnel_depth(patterns or {}),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# 수용 곡선 — "몇 명까지 버티나"
# ─────────────────────────────────────────────────────────────────────────────

def capacity_curve(load: Mapping[str, Any]) -> str:
    """동시 실행 수가 오를 때 응답시간이 **어디서 꺾이는가**.

    ★ 이 리포트에서 가장 중요한 질문("40명이 버티나")의 직접적인 답인데, 지금까지
      표로만 있었다. 표는 구간별 숫자를 나열할 뿐 **꺾이는 지점**을 보여 주지 못한다.
      완만하게 오르면 여유가 있는 것이고, 특정 구간에서 급격히 치솟으면 거기가
      수용 한계다 — 그 모양은 선으로 봐야 읽힌다.

    p50 과 p95 를 같이 그린다. 둘이 벌어지는 지점이 "평균은 괜찮은데 일부가
    심하게 밀리기 시작한" 순간이고, 학생이 이탈하는 건 그 일부다.
    """
    H = _h()
    rows = [r for r in ((load or {}).get("by_concurrency") or []) if H._n(r.get("turns"))]
    if len(rows) < 2:
        return ""

    ymax = max(H._n(r.get("p95")) for r in rows) or 1
    W, Hh = 720.0, 250.0
    L, R, T, B = 56.0, 16.0, 16.0, 38.0
    pw, ph = W - L - R, Hh - T - B
    n = len(rows)
    step = pw / max(n - 1, 1)

    def pt(i, v):
        return L + i * step, T + ph - (H._n(v) / ymax) * ph

    def series(key, color, dash=""):
        pts = [pt(i, r.get(key)) for i, r in enumerate(rows)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}">'
            f'<title>동시 {H._e(rows[i].get("bucket"))}건 · {key} '
            f'{_fmt_ms(H._n(rows[i].get(key)))} · {H._comma(rows[i].get("turns"))}턴</title>'
            f'</circle>' for i, (x, y) in enumerate(pts))
        return (f'<polyline points="{line}" fill="none" stroke="{color}" '
                f'stroke-width="2.2"{dash}/>{dots}')

    grid = ylab = ""
    for f in (0.0, 0.5, 1.0):
        y = T + ph - f * ph
        grid += (f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" '
                 f'stroke="var(--line)" stroke-width="1"/>')
        ylab += (f'<text x="{L - 8}" y="{y + 3.5:.1f}" text-anchor="end" font-size="11" '
                 f'fill="var(--ink-3)">{_fmt_ms(ymax * f)}</text>')
    xlab = "".join(
        f'<text x="{L + i * step:.1f}" y="{Hh - 14:.1f}" text-anchor="middle" '
        f'font-size="11" fill="var(--ink-3)">{H._e(r.get("bucket"))}</text>'
        for i, r in enumerate(rows))
    xtitle = (f'<text x="{L + pw / 2:.1f}" y="{Hh - 1:.1f}" text-anchor="middle" '
              f'font-size="11" fill="var(--ink-3)">동시 실행 턴 수</text>')

    # ⚠ f-string 안에 백슬래시·같은 따옴표를 넣으면 Python 3.11 에서 문법 오류다(CI 가 3.11).
    #   시리즈를 먼저 조립해 두고 본문에서는 이름만 끼운다.
    dash_attr = " stroke-dasharray=\"5 4\""
    s_p50 = series("p50", "var(--ok)", dash_attr)
    s_p95 = series("p95", "var(--crit)")
    svg = (f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="동시 실행 대비 응답시간 곡선" class="fig">'
           f'{grid}{ylab}{xlab}{xtitle}{s_p50}{s_p95}'
           f'<line x1="{L}" y1="{T + ph:.1f}" x2="{W - R}" y2="{T + ph:.1f}" '
           f'stroke="var(--rule)" stroke-width="1.2"/></svg>')

    legend = ('<p class="muted">'
              '<span style="color:var(--crit)">━</span> p95(느린 쪽) · '
              '<span style="color:var(--ok)">╌</span> 중앙값 &nbsp;|&nbsp; '
              '두 선이 벌어지는 지점부터 일부 학생만 심하게 밀립니다</p>')
    return _panel(
        "몇 명까지 버티나 — 수용 곡선",
        "동시 실행이 늘 때 응답시간이 <b>어디서 꺾이는지</b>가 수용 한계입니다. "
        "완만히 오르면 여유가 있고, 특정 구간에서 치솟으면 거기가 벽입니다. "
        "표는 숫자를 나열할 뿐 이 모양을 보여 주지 못합니다.",
        f'<div class="figwrap">{svg}</div>{legend}')
