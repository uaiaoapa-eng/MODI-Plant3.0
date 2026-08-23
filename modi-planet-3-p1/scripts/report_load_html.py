"""부하 리포트 패널 — "얼마나 버텼나"를 읽는 화면.

report_html 이 답하는 질문은 "얼마 썼나"다. 여기는 다른 질문을 답한다:
학생이 얼마나 기다렸나 · 동시에 몇 명이었나 · 몇 명이 튕겼나 · 붐빌수록 느려졌나.

report_html 과 순환 임포트를 피하려고, 공용 헬퍼는 **함수 안에서** 지연 임포트한다
(report_html 쪽이 이 모듈을 render 안에서 부르기 때문에 반대 방향은 모듈 상단이어도
문제가 없지만, 한쪽만 지연으로 두면 나중에 방향이 바뀌어도 안 깨진다).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def _h():
    from importlib import import_module
    return import_module("report_html")


def _ms(v: Any) -> str:
    """밀리초를 사람이 읽는 단위로. 부하 화면에서 '93000'은 아무 의미가 없다."""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    if n < 1000:
        return f"{int(n)}ms"
    if n < 60_000:
        return f"{n / 1000:.1f}초"
    return f"{int(n // 60_000)}분 {int((n % 60_000) / 1000)}초"


def _tone(value: float, warn: float, bad: float) -> str:
    """수치를 상태로 — 숫자만 보면 괜찮은지 아닌지 판단이 안 된다."""
    if value >= bad:
        return "bad"
    if value >= warn:
        return "warn"
    return "good"


# ─────────────────────────────────────────────────────────────────────────────
# 요약 — 제일 먼저 보이는 것
# ─────────────────────────────────────────────────────────────────────────────

def summary_panel(load: Mapping[str, Any]) -> str:
    """성공률·응답시간·피크 동접을 한눈에.

    상세보다 요약을 먼저 둔다 — 수업 직후에 알고 싶은 건 "버텼나?" 한 줄이고,
    원인은 그 다음이다.
    """
    H = _h()
    if not load:
        return ""
    if not load.get("ok", True):
        return f"""<section class="panel"><div class="panel-head">
      <h2>부하</h2>
      <p class="muted">부하 지표를 불러오지 못했습니다 — {H._e(load.get('error', ''))}
         (비용 집계는 정상입니다)</p>
    </div></section>"""

    turns = H._n(load.get("turns"))
    if not turns:
        return """<section class="panel"><div class="panel-head">
      <h2>부하</h2>
      <p class="muted">이 기간에 기록된 턴이 없습니다.</p>
    </div></section>"""

    dur = load.get("duration") or {}
    ttft = load.get("ttft") or {}
    conc = load.get("concurrency") or {}
    ops = load.get("ops") or {}
    fail_pct = H._f(load.get("fail_pct"))
    rejected = sum(r.get("n", 0) for r in (ops.get("by_kind") or [])
                   if r.get("kind") in ("session_busy", "user_quota", "blocked"))

    # 임계는 수업 체감 기준이다: 첫 글자가 10초 넘게 안 나오면 학생이 새로고침한다.
    cards = "".join([
        _card("성공률", f"{100 - fail_pct:.1f}%",
              f"{H._comma(load.get('success'))}/{H._comma(turns)}턴",
              _tone(fail_pct, 2, 10)),
        _card("첫 글자까지", _ms(ttft.get("p95")), "p95 · 학생 체감 대기",
              _tone(H._f(ttft.get("p95")), 10_000, 25_000)),
        _card("응답 완료", _ms(dur.get("p95")), f"p95 · 중앙값 {_ms(dur.get('p50'))}",
              _tone(H._f(dur.get("p95")), 120_000, 240_000)),
        _card("최대 동시", H._comma(conc.get("peak")),
              f"{H._e(conc.get('peak_at') or '')}", "good"),
        _card("튕김", H._comma(rejected), "동시처리 거절·쿼터·차단",
              _tone(float(rejected), 1, 10)),
        _card("중단", H._comma(load.get("aborted")), "끝까지 안 간 턴(이탈)",
              _tone(H._f(load.get("aborted")), 1, 5)),
    ])

    warn = ""
    if load.get("truncated"):
        warn = ('<p class="muted">⚠ 기간이 넓어 최근 일부만 계산했습니다 — '
                '정확한 부하 분석은 하루씩 조회하세요.</p>')

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>부하 요약</h2>
    <p>수업이 <strong>버텼는지</strong>를 먼저 봅니다. 평균이 아니라 <strong>p95</strong>
       (100명 중 95번째로 느린 사람)를 씁니다 — 평균은 소수의 긴 대기를 감춥니다.</p>
    {warn}
  </header>
  <div class="cards">{cards}</div>
</section>"""


def _card(label: str, value: str, sub: str, tone: str) -> str:
    e = _h()._e
    return f"""<div class="card card-{e(tone)}">
    <span class="card-label">{e(label)}</span>
    <span class="card-value">{e(value)}</span>
    <span class="card-sub">{e(sub)}</span>
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# 동시 접속 곡선
# ─────────────────────────────────────────────────────────────────────────────

def concurrency_panel(load: Mapping[str, Any], *, height: int = 150) -> str:
    """분 단위 동시 실행 곡선.

    시간 단위로는 2시간 수업이 두 칸이라 아무것도 안 보인다. 그리고 이 곡선은
    샘플링이 아니라 **구간 겹침 전수 계산**이라 표본 사이로 피크가 새지 않는다.
    """
    H = _h()
    conc = (load or {}).get("concurrency") or {}
    buckets: Sequence[Mapping[str, Any]] = conc.get("buckets") or []
    skipped = H._n(conc.get("skipped"))
    if not buckets:
        # ★ 곡선이 비어도 예외가 안 난다. 아무 말 없이 빈 화면을 보여 주면
        #   "한산했다"로 오독된다 — 계산이 안 된 것과 반드시 구별해 말한다.
        if skipped:
            return f"""<section class="panel"><div class="panel-head">
      <h2>동시 접속 추이</h2>
      <p class="muted">시작·소요 시각이 없어 {H._comma(skipped)}건을 계산에서 제외했습니다 —
         관측 컬럼 배포 이전 턴입니다. 동접은 배포 이후 턴부터 나옵니다.</p>
    </div></section>"""
        return ""

    vals = [H._n(b.get("concurrent")) for b in buckets]
    peak = max(vals) or 1
    n = len(vals)
    W, Hh, PAD_B, PAD_T = 100.0, float(height), 4.0, 8.0
    plot_h = Hh - PAD_B - PAD_T
    slot = W / n

    pts = []
    for i, v in enumerate(vals):
        x = i * slot + slot / 2
        y = PAD_T + (plot_h - (v / peak) * plot_h)
        pts.append(f"{x:.2f},{y:.2f}")
    # 면적으로 채워야 "얼마나 오래 붐볐는지"가 눈에 들어온다(점 하나짜리 피크와 구분).
    area = (f'<polygon class="area" points="{slot/2:.2f},{PAD_T + plot_h:.2f} '
            f'{" ".join(pts)} {W - slot/2:.2f},{PAD_T + plot_h:.2f}" />') if n > 1 else ""
    line = f'<polyline class="ma" points="{" ".join(pts)}" />' if n > 1 else ""

    # x 라벨은 SVG 밖 HTML 로 — preserveAspectRatio="none" 이 글자까지 가로로 10배
    # 늘려 버린다(report_html._xlabels 주석의 실측 참조). 시:분만 남긴다.
    labels = H._xlabels([str(b.get("t", ""))[11:] for b in buckets], n)
    # y 눈금 — 동접은 **정수 인원수**라 반올림된 중간값을 보여 주면 안 된다.
    # 피크가 홀수면 중간 눈금을 내림해 실제로 존재하는 값만 찍는다.
    mid = peak // 2
    yt = H._yticks([(PAD_T, f"{peak}건"),
                    (PAD_T + plot_h / 2, f"{mid}건" if mid and mid != peak else ""),
                    (PAD_T + plot_h, "0")], Hh)

    # ⚠ f-string 안에 같은 따옴표를 다시 쓰면 Python 3.11 에서 문법 오류다(CI 가 3.11).
    #   svg 를 먼저 조립해 두고 본문에서는 이름만 끼운다.
    svg = (
        f'<svg viewBox="0 0 {W:.0f} {Hh:.0f}" preserveAspectRatio="none"'
        f' role="img" aria-label="동시 접속 추이">'
        f'<line class="grid" x1="0" y1="{PAD_T:.1f}" x2="100" y2="{PAD_T:.1f}"/>'
        f'<line class="grid" x1="0" y1="{PAD_T + plot_h / 2:.1f}" x2="100"'
        f' y2="{PAD_T + plot_h / 2:.1f}"/>'
        f'<line class="axis" x1="0" y1="{PAD_T + plot_h:.1f}" x2="100"'
        f' y2="{PAD_T + plot_h:.1f}"/>'
        f'{area}{line}</svg>')
    plot = H._plot(svg, yticks=yt, xlabels=labels)

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>동시 접속 추이</h2>
    <p>같은 순간에 <strong>동시에 처리 중이던 턴</strong>의 수(분 단위).
       최고 <strong>{H._comma(peak)}건</strong> — {H._e(conc.get('peak_at') or '')}.
       시작 건수가 아니라 <strong>겹침</strong>을 세므로 실제 부하와 일치합니다.</p>
    {'<p class="muted">⚠ 시각이 없는 %s건은 제외했습니다(관측 이전 턴).</p>' % H._comma(skipped) if skipped else ''}
  </header>
  {plot}
</section>"""


def capacity_panel(load: Mapping[str, Any]) -> str:
    """★ "40명이 버티나"의 직접적인 답 — 동접이 오를 때 응답시간이 꺾이는가."""
    H = _h()
    rows_in = (load or {}).get("by_concurrency") or []
    if not rows_in:
        return ""
    rows = [(f'동시 {H._e(r.get("bucket"))}건', H._comma(r.get("turns")),
             _ms(r.get("p50")), _ms(r.get("p95")), _ms(r.get("max")))
            for r in rows_in]
    return H._table(
        "동시 접속 대비 응답시간",
        "전체 p95 하나로는 '원래 느린 것'과 '붐빌 때만 느린 것'이 안 갈립니다. "
        "동접이 오를수록 p95 가 꺾이는 구간이 곧 수용 한계입니다.",
        ["동시 실행", "턴", "중앙값", "p95", "최대"], rows,
        empty="구간별로 나눌 만큼 데이터가 없습니다")


# ─────────────────────────────────────────────────────────────────────────────
# 실패 · 운영 사건
# ─────────────────────────────────────────────────────────────────────────────

_OPS_LABEL = {
    "session_busy": "동시처리 거절 (같은 세션이 처리 중)",
    "user_quota": "쿼터 소진",
    "blocked": "차단된 사용자",
    "error": "서버 에러",
    "restart": "컨테이너 재시작",
    "health_fail": "헬스체크 실패",
}


def failures_panel(load: Mapping[str, Any], *, root: str = "", token: str = "") -> str:
    """실패·거절을 한 표로. 원인이 다르면 대응도 다르다."""
    H = _h()
    if not load:
        return ""
    ops = load.get("ops") or {}
    by_err = load.get("by_error") or []
    by_kind = ops.get("by_kind") or []

    rows = []
    for r in by_kind:
        k = r.get("kind", "")
        rows.append((H._e(_OPS_LABEL.get(k, k)), H._comma(r.get("n")),
                     H._comma(r.get("users")), "ops_events"))
    for r in by_err:
        rows.append((H._e(f'턴 실패 · {r.get("code", "")}'), H._comma(r.get("n")),
                     "—", "usage_turns"))

    hint = ("동시처리 거절과 쿼터 소진은 **턴이 만들어지지 않아** 사용량 원장에 "
            "남지 않습니다. 그래서 따로 기록합니다 — 40명 수업에서 '몇 명이 튕겼나'가 "
            "여기 있습니다.")
    if not ops.get("ok", True):
        hint += f" (운영 사건 조회 실패: {ops.get('error', '')})"

    table = H._table("실패 · 거절", hint, ["종류", "건수", "영향 사용자", "원천"],
                     rows, empty="이 기간에 실패·거절이 없습니다 👍")

    recent = ops.get("recent") or []
    if not recent:
        return table
    rrows = [(_session_link(r.get("session_id"), r.get("t", ""), root=root, token=token),
              H._e(_OPS_LABEL.get(r.get("kind", ""), r.get("kind", ""))),
              H._short_user(r.get("user_id")), H._e(r.get("replica") or "—"),
              H._e((r.get("detail") or "")[:80]))
             for r in recent[:30]]
    return table + H._table(
        "최근 사건 (최신 30건)",
        "언제 · 누가 · 어느 서버에서. 같은 학생이 반복해 튕겼다면 개별 대응이 필요합니다.",
        ["시각(KST)", "종류", "사용자", "서버", "상세"], rrows,
        empty="사건 없음")


def replica_panel(load: Mapping[str, Any]) -> str:
    """3대에 고르게 갔나, 한 대만 느린가."""
    H = _h()
    rows_in = (load or {}).get("by_replica") or []
    if len(rows_in) <= 1 and not (rows_in and rows_in[0].get("replica", "").startswith("edu")):
        return ""
    rows = [(H._e(r.get("replica")), H._comma(r.get("turns")), H._comma(r.get("fail")),
             _ms(r.get("p50")), _ms(r.get("p95")), _ms(r.get("max")))
            for r in rows_in]
    return H._table(
        "서버(레플리카)별",
        "3대에 고르게 분산됐는지, 특정 대만 느리거나 실패가 몰리는지 봅니다. "
        "한 대만 나쁘면 그 컨테이너 문제고, 셋 다 나쁘면 용량 문제입니다.",
        ["서버", "턴", "실패", "중앙값", "p95", "최대"], rows,
        empty="레플리카 정보가 기록되지 않았습니다")


# ─────────────────────────────────────────────────────────────────────────────
# 질문 유형 · 결과
# ─────────────────────────────────────────────────────────────────────────────

def types_panel(load: Mapping[str, Any]) -> str:
    """어떤 질문이 오고, 무엇이 나왔고, 각각 얼마나 비싸고 느린가."""
    H = _h()
    if not load:
        return ""
    out = ""

    intents = load.get("by_intent") or []
    if intents:
        rows = [(H._e(r.get("label")), H._comma(r.get("turns")),
                 H._usd(r.get("usd")), H._usd(r.get("usd_per_turn")),
                 _ms(r.get("p50")), _ms(r.get("p95")),
                 f'{H._f(r.get("fail_pct")):.1f}%')
                for r in intents]
        out += H._table(
            "질문 유형별",
            "학생이 무엇을 물었나. 구현 요청과 단순 질문은 비용이 자릿수로 갈립니다 — "
            "어느 쪽을 재사용으로 돌려야 이득인지가 여기서 정해집니다.",
            ["유형", "턴", "비용", "턴당", "중앙값", "p95", "실패율"], rows,
            empty="유형이 기록되지 않았습니다")

    outcomes = load.get("by_outcome") or []
    if outcomes:
        rows = [(H._e(r.get("label")), H._comma(r.get("turns")),
                 H._usd(r.get("usd")), H._usd(r.get("usd_per_turn")),
                 _ms(r.get("p50")), _ms(r.get("p95")),
                 f'{H._f(r.get("fail_pct")):.1f}%')
                for r in outcomes]
        out += H._table(
            "결과물 유형별",
            "그 턴이 실제로 무엇을 내놨나. 하드웨어(블록)와 소프트웨어(코드)의 "
            "단가·소요가 여기서 갈립니다. ‘산출물 없음’이 많으면 헛돈 턴이 많다는 뜻입니다.",
            ["결과물", "턴", "비용", "턴당", "중앙값", "p95", "실패율"], rows,
            empty="결과가 기록되지 않았습니다")
    return out


def reuse_curve_panel(load: Mapping[str, Any]) -> str:
    """★ 비용을 '줄이는' 결정에 쓰는 유일한 표.

    재사용 패널(_reuse_panel)은 "얼마나 아꼈나"(과거)를 답한다. 이 표는
    "임계를 어디까지 내리면 얼마나 더 아낄 수 있나"(미래)를 답한다.
    """
    H = _h()
    rows_in = (load or {}).get("reuse_curve") or []
    if not rows_in:
        return ""
    rows = [(f'{H._f(r.get("threshold")):.2f}', H._comma(r.get("turns")),
             f'{H._f(r.get("pct_of_cold")):.1f}%',
             H._usd(H._n(r.get("weighted_tokens")) / 1_000_000))
            for r in rows_in]
    return H._table(
        "재사용 임계값을 내리면 (절감 여력)",
        "새로 생성한(cold) 턴들의 재사용 후보 점수 분포입니다. "
        "점수가 임계 바로 아래에 몰려 있으면 임계를 조금만 내려도 재사용이 크게 늘고, "
        "넓게 흩어져 있으면 내려도 오탐만 늡니다. 이 판단은 점수를 남긴 날에만 가능합니다.",
        ["임계값 후보", "추가로 재사용될 턴", "cold 중 비중", "그만큼의 비용"], rows,
        empty="재사용 후보 점수가 기록된 턴이 없습니다")


def panel_groups(load: Mapping[str, Any], *, patterns: Mapping[str, Any] | None = None,
                 quota_max_turns: int = 70, root: str = "",
                 token: str = "") -> dict[str, str]:
    """탭 단위로 묶은 패널. 화면이 길어지면 **어디를 봐야 할지**를 잃는다.

    묶는 기준은 '무엇을 알고 싶은가'다:
        summary  버텼나 — 수업 직후 첫 질문
        load     왜 느렸나 — 원인 추적
        users    누가 어떻게 썼나 — 사람 쪽
    """
    pat = patterns or {}
    return {
        "summary": summary_panel(load),
        "load": "".join([
            concurrency_panel(load),
            capacity_panel(load),
            slowest_panel(load, root=root, token=token),
            failures_panel(load, root=root, token=token),
            replica_panel(load),
            resources_panel(load),
        ]),
        "users": "".join([
            concentration_panel(pat, root=root, token=token),
            quota_watch_panel(pat, max_turns=quota_max_turns, root=root, token=token),
            distribution_panels(pat),
            clients_panel(load),
            types_panel(load),
            reuse_curve_panel(load),
        ]),
    }


def all_panels(load: Mapping[str, Any], *, patterns: Mapping[str, Any] | None = None,
               quota_max_turns: int = 70, root: str = "", token: str = "") -> str:
    """부하 블록 전체 — 요약 → 곡선 → 수용한계 → 실패 → 유형 → 절감여력 순.

    순서가 곧 읽는 순서다: 버텼나 → 언제 붐볐나 → 왜 느렸나 → 뭐가 실패했나 →
    무엇이 비쌌나 → 어디를 줄일까.
    """
    if not load:
        return ""
    pat = patterns or {}
    return "".join([
        summary_panel(load),
        concurrency_panel(load),
        capacity_panel(load),
        failures_panel(load, root=root, token=token),
        slowest_panel(load, root=root, token=token),
        replica_panel(load),
        resources_panel(load),
        clients_panel(load),
        # 사용자 쪽 — "누가 많이 쓰나 → 조치가 필요한가 → 어떤 분포인가" 순서
        concentration_panel(pat, root=root, token=token),
        quota_watch_panel(pat, max_turns=quota_max_turns, root=root, token=token),
        distribution_panels(pat),
        types_panel(load),
        reuse_curve_panel(load),
    ])



# ─────────────────────────────────────────────────────────────────────────────
# 실시간 화면 — 수업 중에 보는 것
# ─────────────────────────────────────────────────────────────────────────────

def render_live(load: Mapping[str, Any], *, window_min: int = 15,
                stamp: str = "", refresh: int = 10, health: Mapping[str, Any] | None = None,
                query: str = "", root: str = "") -> str:
    """최근 N분만 보는 자동 새로고침 화면.

    사후 리포트만으로는 **수업 중에 대응할 수 없다.** 학생이 손을 들기 전에
    "지금 붐빈다 / 지금 튕기고 있다"를 봐야 쉬는 시간을 넣거나 순서를 바꾼다.

    일부러 얇게 만든다 — 표를 다 그리면 새로고침마다 무거워지고, 수업 중에는
    숫자 몇 개만 본다.
    """
    H = _h()
    conc = (load or {}).get("concurrency") or {}
    ops = (load or {}).get("ops") or {}
    dur = (load or {}).get("duration") or {}
    ttft = (load or {}).get("ttft") or {}
    turns = H._n((load or {}).get("turns"))
    fail_pct = H._f((load or {}).get("fail_pct"))
    rejected = sum(r.get("n", 0) for r in (ops.get("by_kind") or [])
                   if r.get("kind") in ("session_busy", "user_quota", "blocked"))

    # 마지막 버킷이 '지금'에 가장 가깝다 — 곡선의 최고점이 아니라 현재값을 크게 둔다.
    buckets = conc.get("buckets") or []
    now_conc = H._n(buckets[-1].get("concurrent")) if buckets else 0

    cards = "".join([
        _card("지금 동시", H._comma(now_conc), f"최근 {window_min}분 최고 {H._comma(conc.get('peak'))}",
              _tone(float(now_conc), 20, 35)),
        _card("턴", H._comma(turns), f"최근 {window_min}분", "good"),
        _card("성공률", f"{100 - fail_pct:.1f}%", f"실패 {H._comma((load or {}).get('errors'))}",
              _tone(fail_pct, 2, 10)),
        _card("첫 글자까지", _ms(ttft.get("p95")), "p95", _tone(H._f(ttft.get("p95")), 10_000, 25_000)),
        _card("응답 완료", _ms(dur.get("p95")), f"중앙값 {_ms(dur.get('p50'))}",
              _tone(H._f(dur.get("p95")), 120_000, 240_000)),
        _card("튕김", H._comma(rejected), "동시처리 거절·쿼터",
              _tone(float(rejected), 1, 5)),
    ])

    # 기록이 새고 있으면 지금 알아야 한다 — 수업이 끝난 뒤에는 되돌릴 수 없다.
    hwarn = ""
    h = health or {}
    wb = h.get("writeback") or {}
    if H._n(wb.get("dropped")) or H._n(wb.get("failed")):
        hwarn = (f'<p class="muted">⚠ 사용량 기록 유실 — 버림 {H._comma(wb.get("dropped"))}건 · '
                 f'실패 {H._comma(wb.get("failed"))}건. 리포트가 과소집계됩니다.</p>')

    recent = ops.get("recent") or []
    rows = [(H._e(r.get("t", "")[11:]), H._e(_OPS_LABEL.get(r.get("kind", ""), r.get("kind", ""))),
             H._short_user(r.get("user_id")), H._e(r.get("replica") or "—"))
            for r in recent[:12]]
    events = H._table("최근 사건", "지금 튕기고 있는 학생이 있는지.",
                      ["시각", "종류", "사용자", "서버"], rows,
                      empty="조용합니다 👍")

    band = H._band("LIVE", "실시간 부하",
                   f"최근 {window_min}분 · {refresh}초마다 갱신 · {H._e(stamp)} KST")
    sheet = f"""<section class="panel">
  <header class="panel-head">
    <h2>지금 상태</h2>
    <p>수업 중에 보는 화면입니다. 사후 리포트는
       <a href="{H._e(root)}reports{H._e(query)}">기간 전체 보기</a>에 있습니다.</p>
    {hwarn}
  </header>
  <div class="cards">{cards}</div>
</section>""" + concurrency_panel(load, height=120) + events

    page = H._page(f"실시간 부하 · 최근 {window_min}분", band, sheet)
    # 새로고침은 meta 로 — 자바스크립트 타이머는 탭이 백그라운드로 가면 늦춰지고,
    # 수업 중에는 화면을 띄워 두고 가끔 보기 때문에 정확한 주기가 중요하다.
    return page.replace("<head>", f'<head><meta http-equiv="refresh" content="{int(refresh)}">', 1)


def _session_link(sid: Any, label: str, *, root: str, token: str) -> str:
    """세션 상세로 가는 링크. 표 안의 값이 **무엇에 대한 것인지** 확인할 길을 준다.

    숫자만 있는 표는 "느렸다"까지만 알려 주고 거기서 끊긴다. 그 턴의 대화를 볼 수
    있어야 "무슨 요청이라 느렸나"로 넘어간다.
    ⚠ 토큰을 반드시 실어야 한다 — 리포트는 fail-closed 라 없으면 404 다.
    """
    H = _h()
    sid = str(sid or "")
    if not sid:
        return H._e(label)
    tq = f"?token={H._e(token)}" if token else ""
    return (f'<a href="{H._e(root)}report/session/{H._e(sid)}{tq}" '
            f'title="{H._e(sid)}">{H._e(label)}</a>')


def slowest_panel(load: Mapping[str, Any], *, root: str = "", token: str = "") -> str:
    """가장 느린 턴 — 분포에서 원인 추적으로 넘어가는 다리.

    p95 가 나쁘다는 걸 알아도 "누가 무엇을 하다 그랬나"를 모르면 고칠 수가 없다.
    시작·종료를 나란히 두면 그 턴이 언제 겹쳤는지도 눈으로 확인된다.
    """
    H = _h()
    rows_in = (load or {}).get("slowest") or []
    if not rows_in:
        return ""
    rows = [(_session_link(r.get("session_id"), r.get("start", "")[11:] or "—",
                           root=root, token=token),
             H._e(r.get("end", "")[11:]),
             _ms(r.get("duration_ms")), _ms(r.get("ttft_ms")),
             H._short_user(r.get("user_id")),
             H._e(_INTENT_LABEL_SHORT.get(r.get("intent", ""), r.get("intent") or "—")),
             H._e(r.get("outcome") or "—"), H._e(r.get("replica") or "—"),
             H._e(r.get("status") or "ok"))
            for r in rows_in]
    return H._table(
        "가장 느렸던 턴", "",
        hint_html=("p95 가 나쁘다는 것만으로는 고칠 수 없습니다 — 누가 무엇을 하다 "
                   "느렸는지를 봅니다. <b>시작 시각을 누르면 그 턴의 대화</b>가 열립니다. "
                   "시작·종료가 나란히 있어 어느 턴끼리 겹쳤는지도 확인됩니다."),
        headers=["시작", "종료", "소요", "첫글자", "사용자", "질문", "결과", "서버", "상태"],
        rows=rows, empty="소요 시간이 기록된 턴이 없습니다")


_INTENT_LABEL_SHORT = {
    "question": "질문", "chat": "잡담", "modify_request": "수정",
    "implement_request": "구현", "clarify_request": "되묻기",
    "phase_change": "단계전환", "continue_pending_action": "이어하기",
}


# ─────────────────────────────────────────────────────────────────────────────
# 세션 상세 — 프로젝트 제목을 눌렀을 때
# ─────────────────────────────────────────────────────────────────────────────

_ROLE_LABEL = {"user": "학습자", "ai": "튜터", "assistant": "튜터", "system": "시스템"}


def render_session(sess: Mapping[str, Any], *, usage: Mapping[str, Any] | None = None,
                   back_qs: str = "", root: str = "") -> str:
    """학생이 만든 결과물 한 건을 읽는 화면.

    왜 원본 JSON 링크로 두지 않았나: 관리자가 알고 싶은 건 "이 학생이 무엇을 물었고
    무엇이 나왔나"인데, JSON 을 그대로 던지면 대화가 `\\n` 이 박힌 한 줄로 뭉개져
    사실상 못 읽는다(실제로 그렇게 보였다).

    프론트(별도 SPA)로 보내는 방법도 있지만 그쪽 URL 규약은 이 저장소가 모른다 —
    추측해서 깨진 링크를 만드는 대신 리포트가 직접 보여 준다. 앱으로 여는 링크가
    필요하면 PROJECT_URL_TEMPLATE 로 주입한다(server 참조).

    ⚠ 여기 들어가는 값은 전부 **학생이 쓴 텍스트**다. 반드시 이스케이프한다.
    """
    H = _h()
    if not sess:
        return H.render_error("세션을 찾을 수 없습니다",
                              detail="삭제되었거나 아직 저장되지 않은 세션입니다")

    sid = str(sess.get("session_id") or "")
    title = str(sess.get("title") or "") or "(제목 없음)"
    uid = str(sess.get("user_id") or "")

    meta = " · ".join(x for x in [
        H._e(sess.get("coding_type") or ""),
        H._e(sess.get("app_type") or ""),
        f"단계 {H._e(sess.get('phase') or '-')}",
        f"학생 {H._e(H._short_user(uid))}",
    ] if x)

    band = H._band("PROJECT", title, meta,
                   f'<a href="{H._e(root)}reports{H._e(back_qs)}" '
                   f'class="backlink">← 리포트로</a>')

    # ── 사용량 ──
    stats = ""
    if usage:
        w = H._n(usage.get("weighted_tokens"))
        stats = f"""<div class="cards">
      {_card("턴", H._comma(usage.get("turns")), "이 프로젝트에서", "good")}
      {_card("비용", H._usd(w / 1_000_000), "API 과금 환산", "good")}
      {_card("총 소요", _ms(usage.get("duration_ms")), "모든 턴 합계", "good")}
    </div>"""

    # ── 대화 ──
    msgs = [m for m in (sess.get("messages") or []) if isinstance(m, dict)]
    turns_html = ""
    for m in msgs:
        role = str(m.get("role") or "")
        cls = "u" if role == "user" else "a"
        # 줄바꿈은 CSS(white-space:pre-wrap)가 살린다 — <br> 치환은 이스케이프 뒤에
        # 태그를 다시 끼워 넣는 셈이라 실수 여지가 크다(실제로 리터럴 "\\n" 을 찾는
        # 오타가 있었고, 그래서 튜터 답변이 통째로 한 줄로 뭉개졌다).
        body = H._e(str(m.get("content") or ""))
        turns_html += (f'<div class="msg msg-{cls}">'
                       f'<span class="msg-who">{H._e(_ROLE_LABEL.get(role, role))}</span>'
                       f'<div class="msg-body">{body}</div></div>')
    convo = f"""<section class="panel">
  <header class="panel-head"><h2>대화 {H._comma(len(msgs))}턴</h2>
    <p>학생이 무엇을 묻고 어떻게 풀어 갔는지 — 비용이 <strong>무엇으로 바뀌었는지</strong>가 여기 있습니다.</p>
  </header>
  <div class="convo">{turns_html or '<p class="muted">대화가 없습니다.</p>'}</div>
</section>""" if msgs else ""

    # ── 산출물 ──
    out = ""
    code = sess.get("generated_code") or {}
    if isinstance(code, dict) and code:
        blocks = "".join(
            f'<details class="code"><summary>{H._e(name)}'
            f'<span class="muted"> · {H._comma(len(str(src)))}자</span></summary>'
            f'<pre>{H._e(str(src))}</pre></details>'
            for name, src in list(code.items())[:20])
        out += _doc_panel(f"생성된 코드 {H._comma(len(code))}개", blocks)
    for key, label in (("design_doc", "설계 문서"), ("task_plan", "작업 계획"),
                       ("blockly_flowchart", "블록 흐름도"), ("diagram", "다이어그램")):
        v = sess.get(key)
        if v and str(v).strip() and "아직" not in str(v):
            out += _doc_panel(label, f"<pre>{H._e(_pretty(v))}</pre>")
    notes = sess.get("learning_notes") or []
    if notes:
        items = "".join(f"<li>{H._e(_pretty(nt))}</li>" for nt in notes[:30])
        out += _doc_panel(f"학습 노트 {H._comma(len(notes))}개", f"<ul class='notes'>{items}</ul>")

    if not (convo or out):
        out = ('<section class="panel"><p class="muted">'
               '아직 저장된 대화나 산출물이 없습니다.</p></section>')

    foot = (f'<footer>세션 <code>{H._e(sid)}</code> · '
            f'원본 JSON <a href="{H._e(root)}projects/{H._e(sid)}.json'
            f'?user_id={H._e(uid)}" '
            f'target="_blank" rel="noopener">내려받기</a></footer>')
    return H._page(f"{title} · 프로젝트", band, stats + convo + out + foot)


def _doc_panel(heading: str, body: str) -> str:
    H = _h()
    return (f'<section class="panel"><header class="panel-head">'
            f'<h2>{H._e(heading)}</h2></header>{body}</section>')


def _pretty(v: Any) -> str:
    """dict/list 는 읽을 수 있게 편다 — JSON 한 줄은 사람이 못 읽는다."""
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, indent=2)
    return str(v)


def clients_panel(load: Mapping[str, Any]) -> str:
    """어떤 기기·브라우저로 접속했나.

    수업 환경(태블릿인가 PC인가)에 따라 대응이 달라지고, 특정 브라우저에서만
    실패가 몰리는지도 여기서 갈린다. 그리고 부수적으로 **사람과 스크립트를 가른다** —
    2026-08-21 에 '사용자 46명'이 찍혔는데 45명이 부하 테스트 계정이었다.
    """
    H = _h()
    c = (load or {}).get("clients") or {}
    rows_in = c.get("by_client") or []
    if not rows_in:
        return ""

    human, script = H._n(c.get("human_users")), H._n(c.get("script_users"))
    note = ""
    if script:
        note = (f' 이 중 <b>{H._comma(script)}명은 스크립트</b>(부하 테스트 등)이고 '
                f'실제 사람은 <b>{H._comma(human)}명</b>입니다.')

    rows = [(H._e(f'{r["device"]} · {r["os"]}'), H._e(r["browser"]),
             H._comma(r["turns"]), H._comma(r["users"]),
             _ms(r.get("p50")), _ms(r.get("p95")), H._comma(r.get("fail")))
            for r in rows_in]
    out = H._table(
        "접속 환경", "",
        hint_html=("기기·브라우저별 분포와 지연입니다. 한 종류에서만 느리거나 실패가 "
                   "몰리면 서버가 아니라 그 환경의 문제입니다." + note),
        headers=["기기 · OS", "브라우저", "턴", "사용자", "중앙값", "p95", "실패"],
        rows=rows, empty="접속 환경이 기록되지 않았습니다")

    nets = c.get("networks") or []
    if len(nets) > 1:
        nrows = [(H._e(_mask_ip(n["ip"])), H._comma(n["users"])) for n in nets]
        out += H._table(
            "접속 네트워크",
            "같은 네트워크에서 오면 IP 가 같습니다 — 교실 안인지 밖인지 구분됩니다. "
            "IP 뒷자리는 가려서 보여 줍니다.",
            ["네트워크", "사용자"], nrows, empty="")
    return out


def _mask_ip(ip: str) -> str:
    """마지막 옥텟을 가린다. 네트워크 구분에는 충분하고 개인 식별성은 낮춘다."""
    ip = (ip or "").strip()
    if ip.count(".") == 3:
        return ".".join(ip.split(".")[:3]) + ".*"
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + ":*"
    return ip or "(미상)"


def concentration_panel(patterns: Mapping[str, Any], *, root: str = "",
                        token: str = "", top: int = 12) -> str:
    """★ "특정 사용자가 너무 많이 쓰고 있나" — 편중을 막대로 본다.

    표의 숫자만 보면 1등이 62턴인지 620턴인지는 알아도 **그게 많은 건지**는 모른다.
    막대로 나란히 두면 한 명이 튀는지 고르게 퍼졌는지가 한눈에 들어오고,
    상위 비중을 함께 적어 판단 기준을 준다.

    수업에서 이게 중요한 이유: 한 명이 쿼터를 다 먹으면 다른 학생이 못 쓴다.
    비용 총액이 같아도 '고르게 40명'과 '한 명이 절반'은 완전히 다른 상황이다.
    """
    H = _h()
    users = list((patterns or {}).get("users") or [])
    if len(users) < 2:
        return ""

    users.sort(key=lambda r: H._n(r.get("turns")), reverse=True)
    total = sum(H._n(u.get("turns")) for u in users) or 1
    n = len(users)
    avg = total / n
    top1 = H._n(users[0].get("turns")) / total * 100
    top5 = sum(H._n(u.get("turns")) for u in users[:5]) / total * 100
    ratio = (H._n(users[0].get("turns")) / avg) if avg else 0

    # 판단 기준을 글로 준다 — 숫자만 던지면 "그래서 문제인가"에 답이 안 된다.
    if top1 >= 40:
        verdict, tone = "한 사람에게 크게 쏠려 있습니다", "bad"
    elif top1 >= 20:
        verdict, tone = "상위 사용자 쏠림이 보입니다", "warn"
    else:
        verdict, tone = "고르게 분포합니다", "good"

    cards = "".join([
        _card("최다 사용자 비중", f"{top1:.0f}%", f"전체 {H._comma(total)}턴 중", tone),
        _card("상위 5명 비중", f"{top5:.0f}%", f"사용자 {H._comma(n)}명", "good"),
        _card("평균 대비", f"{ratio:.1f}배", f"1인 평균 {avg:.1f}턴", tone),
    ])

    peak = H._n(users[0].get("turns")) or 1
    bars = ""
    for u in users[:top]:
        turns = H._n(u.get("turns"))
        share = turns / peak
        label = _session_link(u.get("session_id"), H._short_user(u.get("user_id")),
                              root=root, token=token) if u.get("session_id") else \
            f'<span title="{H._e(u.get("user_id"))}">{H._e(H._short_user(u.get("user_id")))}</span>'
        bars += f"""<div class="mix-row">
      <span class="mix-name">{label}</span>
      <span class="mix-track"><span class="mix-fill" style="width:{H._bar(share)}%"></span></span>
      <span class="mix-pct">{H._comma(turns)}턴</span>
      <span class="mix-raw">{H._usd(u.get("usd"))} · 세션 {H._comma(u.get("sessions"))}</span>
    </div>"""

    more = ("" if n <= top else
            f'<p class="muted">상위 {top}명만 표시 — 전체 {H._comma(n)}명은 아래 표에 있습니다.</p>')
    return f"""<section class="panel">
  <header class="panel-head">
    <h2>사용자 편중 — {H._e(verdict)}</h2>
    <p>한 명이 몰아 쓰면 총액이 같아도 상황이 다릅니다 — 그 학생이 쿼터를 다 먹으면
       나머지가 못 씁니다. 막대는 <strong>최다 사용자 대비</strong> 비율입니다.</p>
  </header>
  <div class="cards">{cards}</div>
  <div class="mix" style="margin-top:14px">{bars}</div>
  {more}
</section>"""


def _histogram(title: str, hint: str, buckets: Sequence[Mapping[str, Any]],
               *, key: str, value_key: str, unit: str = "명") -> str:
    """구간별 분포를 막대로. 표로 두면 '어디에 몰렸나'가 안 보인다.

    구간이 이미 정해져 나오므로(session_depth·user_segments) 여기서는 그리기만 한다.
    가로 막대를 쓰는 이유: 구간 라벨이 한글이라 세로 막대 아래에 두면 겹친다.
    """
    H = _h()
    rows = [b for b in (buckets or []) if H._n(b.get(value_key))]
    if not rows:
        return ""
    peak = max(H._n(b.get(value_key)) for b in rows) or 1
    total = sum(H._n(b.get(value_key)) for b in rows) or 1
    bars = ""
    for b in rows:
        v = H._n(b.get(value_key))
        bars += f"""<div class="mix-row">
      <span class="mix-name">{H._e(b.get(key))}</span>
      <span class="mix-track"><span class="mix-fill" style="width:{H._bar(v / peak)}%"></span></span>
      <span class="mix-pct">{H._comma(v)}{H._e(unit)}</span>
      <span class="mix-raw">{v / total * 100:.0f}%</span>
    </div>"""
    return f"""<section class="panel">
  <header class="panel-head"><h2>{H._e(title)}</h2><p>{hint}</p></header>
  <div class="mix">{bars}</div>
</section>"""


def distribution_panels(patterns: Mapping[str, Any]) -> str:
    """사용 패턴 분포 — 얕게 여러 번인가, 깊게 파고드는가."""
    p = patterns or {}
    return (
        _histogram(
            "사용량 분포", "학생 한 명이 하루에 몇 턴을 썼나. "
            "오른쪽 구간에 사람이 몰리면 쿼터(하루 70턴)에 닿는 학생이 나옵니다.",
            p.get("user_segments") or [], key="bucket", value_key="users")
        + _histogram(
            "대화 깊이 분포", "한 프로젝트를 몇 턴 만에 끝냈나. "
            "1턴이 많으면 시작만 하고 이탈한 것이고, 길면 막혀서 반복한 것입니다.",
            p.get("session_depth") or [], key="bucket", value_key="sessions",
            unit="개")
    )


def quota_watch_panel(patterns: Mapping[str, Any], *, max_turns: int = 70,
                      root: str = "", token: str = "") -> str:
    """★ 쿼터 한도에 닿는 학생 — "비정상적으로 많으면 처리가 필요하다"의 실행 화면.

    편중 패널이 '누가 많이 쓰나'를 보여 준다면 여기는 **'조치가 필요한가'** 를 답한다.
    집행은 이미 켜져 있으므로(QUOTA_ENABLED=true, 하루 70턴) 한도에 닿은 학생은
    자동으로 막힌다 — 문제는 그 사실을 운영자가 모른 채 학생이 "안 돼요"라고
    말하는 상황이다. 미리 보이면 수업 중에 대응할 수 있다.
    """
    H = _h()
    users = list((patterns or {}).get("users") or [])
    if not users or max_turns <= 0:
        return ""
    watch = sorted((u for u in users if H._n(u.get("turns")) >= max_turns * 0.6),
                   key=lambda r: H._n(r.get("turns")), reverse=True)
    if not watch:
        return ""

    rows = []
    for u in watch[:20]:
        t = H._n(u.get("turns"))
        pct = t / max_turns * 100
        state = ("소진" if t >= max_turns else
                 "임박" if pct >= 85 else "주의")
        rows.append((
            f'<span title="{H._e(u.get("user_id"))}">{H._e(H._short_user(u.get("user_id")))}</span>',
            H._comma(t), f"{pct:.0f}%", H._e(state),
            H._usd(u.get("usd")), H._comma(u.get("sessions"))))

    hint = (f"하루 상한은 <b>{H._comma(max_turns)}턴</b>이고 집행이 켜져 있습니다 — "
            "소진하면 그 학생은 자동으로 막힙니다. 여기서 미리 보이면 수업 중에 "
            "순서를 바꾸거나 상한을 조정할 수 있습니다. "
            "특정 학생을 즉시 막아야 하면 <code>QUOTA_DENY_SUBJECTS</code> 에 "
            "<code>u:&lt;user_id&gt;</code> 를 넣고 재기동하세요.")
    return H._table("쿼터 주의 — 상한의 60% 이상 사용", "", hint_html=hint,
                    headers=["학생", "턴", "상한 대비", "상태", "비용", "세션"], rows=rows,
                    empty="")


def resources_panel(load: Mapping[str, Any]) -> str:
    """서버 메모리 — 상한에 다가가는 레플리카가 있는지.

    ★ 상한(1g)에 닿으면 그 컨테이너만 재시작되고 나머지가 서빙한다. 설계상 안전하지만
      **재시작된 뒤에 아는 건 늦다.** 대회 중에 손을 쓰려면 다가가는 게 보여야 한다.

    이 값은 턴 기록에 실려 오므로 별도 수집기가 없고, 어느 레플리카가 언제 무거웠는지가
    응답시간·실패와 같은 시간축 위에 놓인다.
    """
    H = _h()
    res = (load or {}).get("resources") or {}
    rows_in = res.get("replicas") or []
    if not rows_in:
        return ""
    limit = H._n(res.get("limit_mb")) or 1024
    peak = H._n(res.get("peak_pct"))

    def cell(r):
        pct = H._n(r.get("pct_of_limit"))
        tone = _tone(float(pct), 70, 85)
        return (f'<span class="mix-track" style="display:inline-block;width:120px;'
                f'vertical-align:middle"><span class="mix-fill" '
                f'style="width:{H._bar(pct / 100)}%"></span></span> '
                f'<b style="color:var(--{"crit" if tone == "bad" else "warn" if tone == "warn" else "ink"})">'
                f'{pct}%</b>')

    rows = [(H._e(r["replica"]), H._comma(r["turns"]),
             f'{H._comma(r["avg_mb"])}MB', f'{H._comma(r["p95_mb"])}MB',
             f'{H._comma(r["max_mb"])}MB', cell(r))
            for r in rows_in]

    warn = ""
    if peak >= 85:
        warn = ('<p class="muted">⚠ 상한에 근접한 레플리카가 있습니다 — 닿으면 그 '
                '컨테이너가 재시작됩니다(나머지는 계속 서빙). 진행 중 대화는 '
                'graceful drain 으로 보호되지만 반복되면 원인을 봐야 합니다.</p>')
    return H._table(
        "서버 메모리", "",
        hint_html=(f"레플리카별 컨테이너 메모리입니다. 상한은 "
                   f"<b>{H._comma(limit)}MB</b>이고, 닿으면 <b>그 컨테이너만</b> "
                   f"재시작됩니다(호스트 전체가 아니라). 재시작된 뒤에 아는 건 늦으므로 "
                   f"다가가는 걸 봅니다." + warn),
        headers=["서버", "턴", "평균", "p95", "최대", "상한 대비"],
        rows=rows, empty="메모리가 기록되지 않았습니다")
