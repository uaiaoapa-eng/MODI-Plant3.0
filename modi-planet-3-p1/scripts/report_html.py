"""사용량·비용 리포트를 사람이 보는 HTML 한 장으로 렌더한다.

왜 별도 모듈인가:
    ① 순수 함수라 서버 없이 테스트할 수 있다(dict → str).
    ② 라이브 페이지(server.py `/report`)와 밤 스냅샷(scripts/report_snapshot.py)이
       **같은 렌더러**를 쓴다. 둘이 갈라지면 "웹에서 본 값"과 "보관된 값"이 달라진다.

자체 완결 문서다 — 외부 CSS/폰트/스크립트를 전혀 받지 않는다. 사내망·오프라인·
엄격한 CSP 어디서 열어도 같은 모습이어야 하고, 스냅샷 파일은 몇 달 뒤 열어도
그대로 보여야 하기 때문이다.
"""
from __future__ import annotations

import html
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["render", "render_index", "render_error"]

# ── 서식 ──────────────────────────────────────────────────────────────────────


def _n(v: Any) -> int:
    """표시용 정수화. DB/JSON 어느 쪽에서 와도(Decimal·str·None) 깨지지 않게."""
    try:
        return int(v or 0)
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError, ArithmeticError):
        return 0.0


def _comma(v: Any) -> str:
    return f"{_n(v):,}"


def _usd(v: Any) -> str:
    """작은 금액이 0.00 으로 뭉개지지 않게 자릿수를 조절한다."""
    x = _f(v)
    if x and abs(x) < 0.01:
        return f"${x:.4f}"
    return f"${x:,.2f}"


def _krw(v: Any) -> str:
    return f"{_n(v):,}원"


def _e(v: Any) -> str:
    """HTML 이스케이프. user_id 는 외부 입력이므로 반드시 통과시킨다."""
    return html.escape("" if v is None else str(v), quote=True)


def _short_user(uid: Any) -> str:
    """uuid 는 길어서 표를 무너뜨린다 — 앞 8자만 보이고 원본은 title 로."""
    s = str(uid or "")
    return s if len(s) <= 12 else s[:8] + "…"


def _bar(ratio: float) -> str:
    """막대 너비를 % 문자열로. 0~1 밖의 값이 들어와도 막대가 넘치지 않게 클램프."""
    return f"{max(0.0, min(1.0, ratio)) * 100:.1f}"


# ── 조각 ──────────────────────────────────────────────────────────────────────


def _stat(label: str, value: str, sub: str = "") -> str:
    """정의목록 한 칸. 카드가 아니라 왼쪽 괘선으로 구분한다 — 원장의 항목 표기."""
    sub_html = f'<div class="stat-sub">{sub}</div>' if sub else ""
    return (f'<div class="stat"><div class="stat-label">{label}</div>'
            f'<div class="stat-value">{value}</div>{sub_html}</div>')


def _kst(utc_str: str, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC 로 저장된 시각을 KST 로 바꿔 보여준다.

    저장은 UTC, 표시는 KST 가 규약이다. 서버 로케일이나 컨테이너 TZ 가 바뀌어도
    저장값이 흔들리지 않게 하면서, 보는 사람은 늘 한국 시간으로 읽게 한다.
    """
    if not utc_str:
        return ""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    txt = str(utc_str).replace("T", " ")[:19]
    for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(txt, pat).replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo("Asia/Seoul")).strftime(fmt) + " KST"
        except ValueError:
            continue
    return str(utc_str)


def _table(caption: str, hint: str, headers: Sequence[str],
           rows: Iterable[Sequence[str]], *, empty: str, hint_html: str = "") -> str:
    """표 한 덩이. `hint` 는 **이스케이프된다**(데이터가 섞여도 안전).

    강조가 필요하면 `hint_html` 로 넘긴다 — 이쪽은 **신뢰하는 마크업**으로 그대로
    나가므로 호출부가 데이터를 직접 _e() 로 감싸야 한다.

    왜 hint 의 의미를 바꾸지 않고 파라미터를 나누나: 기존 호출 중에는 설명에
    DB 에러 문자열을 이어 붙이는 곳이 있다(failures_panel). hint 를 신뢰 마크업으로
    바꾸면 그런 호출이 **조용히 XSS 통로가 된다.** 안전이 기본이고, 신뢰는 명시다.
    """
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    if not body:
        body = f'<tr><td class="empty" colspan="{len(headers)}">{_e(empty)}</td></tr>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return f"""<section class="panel">
  <header class="panel-head">
    <h2>{_e(caption)}</h2>
    <p>{hint_html or _e(hint)}</p>
  </header>
  <div class="scroll">
    <table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
  </div>
</section>"""


def _token_mix(totals: Mapping[str, Any]) -> str:
    """어디에 돈이 쓰였나 — 가중치를 곱한 '비용 기여도' 기준으로 보여준다.

    원시 토큰 수로 줄을 세우면 오해가 생긴다. 입력 토큰이 제일 많아도 단가가 1배라
    비용에서는 거의 안 보이고, 출력은 5배라 적은 양으로도 지배적이다.
    """
    parts = [
        ("출력", "×5", _n(totals.get("output_tokens")) * 5.0,
         _n(totals.get("output_tokens"))),
        ("캐시쓰기", "×1.25", _n(totals.get("cache_creation_tokens")) * 1.25,
         _n(totals.get("cache_creation_tokens"))),
        ("입력", "×1", _n(totals.get("input_tokens")) * 1.0,
         _n(totals.get("input_tokens"))),
        ("캐시읽기", "×0.1", _n(totals.get("cache_read_tokens")) * 0.1,
         _n(totals.get("cache_read_tokens"))),
    ]
    total = sum(p[2] for p in parts) or 1.0
    rows = ""
    for name, mult, weighted, raw in parts:
        share = weighted / total
        rows += f"""<div class="mix-row">
      <span class="mix-name">{_e(name)}<span class="mix-mult">{_e(mult)}</span></span>
      <span class="mix-track"><span class="mix-fill" style="width:{_bar(share)}%"></span></span>
      <span class="mix-pct">{share * 100:.1f}%</span>
      <span class="mix-raw">원시 {_comma(raw)}</span>
    </div>"""
    return f"""<section class="panel">
  <header class="panel-head">
    <h2>토큰 구성</h2>
    <p>가중치를 반영한 <strong>실제 비용 기여도</strong>. 출력이 지배적인 게 정상입니다(단가 5배).</p>
  </header>
  <div class="mix">{rows}</div>
</section>"""


def _budget(totals: Mapping[str, Any], budget_usd: float,
            krw_per_usd: float) -> str:
    if budget_usd <= 0:
        return ""
    spent = _f(totals.get("usd"))
    ratio = spent / budget_usd
    left = budget_usd - spent
    level = "ok" if ratio < 0.7 else ("warn" if ratio < 0.9 else "crit")
    return f"""<section class="panel">
  <header class="panel-head">
    <h2>예산 대비</h2>
    <p>입력한 예산 기준 계산치입니다 — Anthropic 이 잔액 API 를 제공하지 않아
       <strong>실제 잔액은 Console 에서 확인</strong>해야 합니다.</p>
  </header>
  <div class="budget">
    <div class="budget-track"><span class="budget-fill {level}" style="width:{_bar(ratio)}%"></span></div>
    <div class="budget-nums">
      <span><b>{ratio * 100:.1f}%</b> 소진</span>
      <span>{_usd(spent)} / {_usd(budget_usd)}</span>
      <span class="muted">남은 예산 {_usd(left)} (약 {_krw(round(left * krw_per_usd))})</span>
    </div>
  </div>
</section>"""


# ── 스타일 ────────────────────────────────────────────────────────────────────
#
# 라이트/다크 3상태를 모두 다룬다: 명시 선택(data-theme)과, 아무 표시도 없는
# 기본값(prefers-color-scheme)까지. 색은 전부 토큰으로만 정의하고 컴포넌트는
# 토큰만 참조한다 — 미디어쿼리 안에서만 정의된 색이 하나라도 있으면 기본 상태에서
# 한쪽 테마의 글자를 다른 쪽 배경에 얹는 고전적인 버그가 난다.

_CSS = """
/* 방향: 인쇄된 회계 원장 / 재무 보고서.
   흔한 대시보드 문법(그림자 진 라운드 카드 격자, 인디고 액센트)을 피하고
   괘선·여백·활자 위계로 정보를 나눈다. 숫자가 주인공이므로 표시용 숫자는
   세리프(ui-serif)로, UI 문자는 산세리프로 짝지었다 — 웹폰트를 받지 않고도
   두 가지 목소리를 만든다(오프라인·CSP·수개월 뒤 열람 모두 견뎌야 한다). */
:root{
  --bg:#e6eae7; --sheet:#fcfdfc; --surface-2:#eff4f1; --band:#0e1512;
  /* 줄무늬 — green-bar 용지의 옅은 초록 띠. 장식이 아니라 넓은 표를 눈으로 따라가는 장치다. */
  --row:#eef4f0;
  --ink:#101613; --ink-2:#3f4a45; --ink-3:#6c7873;
  --line:#dde3df; --line-2:#c3ccc7; --rule:#101613;
  --accent:#0f5d43; --accent-soft:#e2eee9;
  --ok:#1f6b45; --warn:#8d640f; --crit:#a32a2a;
  --band-ink:#eef3f0;
  /* 가로 스크롤 그늘 — 테마마다 방향이 반대라 토큰으로 둔다 */
  --shade:rgba(16,22,19,.16);
  /* 액센트 위에 얹는 글자색. 다크에서 액센트가 밝아지므로 흰색을 고정하면
     대비가 2:1 로 무너진다 — 테마마다 뒤집는다. */
  --on-accent:#ffffff;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0b0e0d; --sheet:#131916; --surface-2:#1a211d; --band:#080b0a;
    --row:#171f1b;
    --ink:#e6ece9; --ink-2:#a3afa9; --ink-3:#6e7a75;
    --line:#232b27; --line-2:#333d38; --rule:#3a453f;
    --accent:#5cc39b; --accent-soft:#122720;
    --ok:#5fbd8c; --warn:#d0a24e; --crit:#e28084;
    --band-ink:#e6ece9;
    --shade:rgba(0,0,0,.45);
    --on-accent:#0b0e0d;
  }
}
:root[data-theme="dark"]{
    --bg:#0b0e0d; --sheet:#131916; --surface-2:#1a211d; --band:#080b0a;
    --row:#171f1b;
    --ink:#e6ece9; --ink-2:#a3afa9; --ink-3:#6e7a75;
    --line:#232b27; --line-2:#333d38; --rule:#3a453f;
    --accent:#5cc39b; --accent-soft:#122720;
    --ok:#5fbd8c; --warn:#d0a24e; --crit:#e28084;
    --band-ink:#e6ece9;
    --shade:rgba(0,0,0,.45);
    --on-accent:#0b0e0d;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Pretendard","Apple SD Gothic Neo",
    "Noto Sans KR","Segoe UI",Roboto,sans-serif;
  font-size:15.5px; line-height:1.68; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1180px; margin:0 auto; padding:0 0 64px}
.sheet{background:var(--sheet); border:1px solid var(--line);
  border-top:none; padding:0 clamp(16px,3vw,38px) 34px}
a{color:var(--accent)}
.muted{color:var(--ink-3)}
b,strong{font-weight:650}
.num{font-family:ui-serif,Georgia,"Times New Roman",serif;
  font-variant-numeric:tabular-nums; letter-spacing:-.015em}

/* ── 부하 요약 셀: 원장의 '집계란'. 그림자 진 라운드 카드 격자를 쓰지 않는다.
      상태는 색 숫자만이 아니라 **위쪽 굵은 괘선**으로도 표시한다 — 색만으로 구분하면
      색각 이상·흑백 인쇄에서 정보가 사라진다. ── */
.cards{display:grid; gap:0; margin:14px 0 4px;
  grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
  border:1px solid var(--line); border-left:none}
.card{padding:13px 15px 14px; border-left:1px solid var(--line);
  display:flex; flex-direction:column; gap:2px; position:relative}
.card::before{content:""; position:absolute; inset:0 0 auto 0; height:3px;
  background:var(--line-2)}
.card-good::before{background:var(--ok)}
.card-warn::before{background:var(--warn)}
.card-bad::before{background:var(--crit)}
.card-label{font-size:11.5px; letter-spacing:.06em; color:var(--ink-2);
  font-weight:650}
.card-value{font-family:ui-serif,Georgia,"Times New Roman",serif;
  font-variant-numeric:tabular-nums; font-size:32px; line-height:1.1;
  letter-spacing:-.025em; font-weight:600; margin:1px 0}
.card-good .card-value{color:var(--ok)}
.card-warn .card-value{color:var(--warn)}
.card-bad .card-value{color:var(--crit)}
.card-sub{font-size:12px; color:var(--ink-3); line-height:1.45;
  white-space:normal}
/* 동접 곡선의 면적 — 점 하나짜리 피크와 '오래 붐빈 구간'을 눈으로 가른다 */
.area{fill:var(--accent); fill-opacity:.13; stroke:none}

/* ── 마스트헤드: 짙은 띠. 카드 격자 대신 문서의 '표지'를 만든다 ── */
.band{background:var(--band); color:var(--band-ink);
  padding:22px clamp(16px,3vw,38px) 20px}
.band-top{display:flex; flex-wrap:wrap; gap:14px; align-items:baseline;
  justify-content:space-between}
h1{font-size:22px; font-weight:700; margin:0; letter-spacing:-.015em;
  text-wrap:balance; line-height:1.25}
.eyebrow{font-size:10.5px; letter-spacing:.22em; text-transform:uppercase;
  color:var(--ink-3); margin-bottom:5px}
.period{font-size:13px; color:var(--ink-3); font-variant-numeric:tabular-nums}
.mode{display:inline-block; font-size:11px; padding:2px 8px; border-radius:2px;
  border:1px solid var(--rule); color:var(--band-ink); margin-left:7px}
.band a{color:var(--band-ink); opacity:.75}
.band a:hover{opacity:1}

/* ── 히어로: 총액 하나가 지배하고 나머지는 정의목록으로 딸려 온다 ── */
.hero{display:grid; gap:clamp(16px,3vw,44px); align-items:end;
  grid-template-columns:minmax(220px,auto) 1fr;
  padding:30px 0 24px; border-bottom:2px solid var(--rule)}
.hero-total{font-size:clamp(46px,8vw,78px); line-height:.94; font-weight:600}
.hero-sub{font-size:14.5px; color:var(--ink-2); margin-top:9px;
  font-variant-numeric:tabular-nums; font-weight:500}
.hero-sub b,.hero-sub strong{color:var(--ink); font-weight:700}
.defs{display:grid; gap:0 clamp(14px,2.4vw,30px);
  grid-template-columns:repeat(auto-fit,minmax(112px,1fr))}
.stat{padding:2px 0 2px 14px; border-left:2px solid var(--line-2)}
.stat-label{font-size:11.5px; letter-spacing:.04em; color:var(--ink-2);
  font-weight:650}
.stat-value{font-size:25px; font-weight:700; font-variant-numeric:tabular-nums;
  letter-spacing:-.025em; margin-top:2px; line-height:1.15}
.stat-sub{font-size:12px; color:var(--ink-3); font-variant-numeric:tabular-nums}

/* ── 구획: 그림자 카드 대신 괘선과 여백 ──
   제목을 '10px 대문자 라벨'로 두면 모든 구획이 똑같은 리듬으로 흘러 어디가 중요한지
   안 보인다. 제목은 크고 굵게(읽는 단위), 설명은 작게(참고) — 둘의 대비가 위계다. */
.panel{border-top:1px solid var(--line-2); padding-top:22px; margin-top:32px}
.panel:first-of-type{margin-top:24px}
.panel-head{margin-bottom:15px}
.panel-head h2{font-size:19px; margin:0; letter-spacing:-.015em;
  color:var(--ink); font-weight:700; line-height:1.25; text-wrap:balance}
/* 제목 옆 짧은 색 막대 — 구획의 시작을 눈이 바로 잡는다 */
.panel-head h2::before{content:""; display:inline-block; width:3px; height:.82em;
  background:var(--accent); margin-right:9px; vertical-align:-1px; border-radius:1px}
.panel-head p{font-size:13.5px; color:var(--ink-2); margin:7px 0 0; max-width:74ch;
  line-height:1.62}
.panel-head p strong{color:var(--ink); font-weight:700}

/* 표는 칸이 많아 좁은 화면에서 가로로 넘친다. 넘치는 것 자체는 정상이지만,
   **넘친다는 사실이 안 보이는 것**이 문제다 — 잘린 표를 '이게 전부'로 읽는다.
   오른쪽 끝에 옅은 그늘을 둬서 더 있다는 걸 알린다. background-attachment:local
   덕분에 끝까지 스크롤하면 그늘이 저절로 사라진다(자바스크립트 불필요). */
.scroll{overflow-x:auto; margin:0 calc(-1 * clamp(16px,3vw,38px));
  padding:0 clamp(16px,3vw,38px);
  background:
    linear-gradient(to right, var(--sheet) 30%, transparent) left / 24px 100% no-repeat local,
    linear-gradient(to left,  var(--sheet) 30%, transparent) right / 24px 100% no-repeat local,
    linear-gradient(to right, var(--shade), transparent) left / 12px 100% no-repeat scroll,
    linear-gradient(to left,  var(--shade), transparent) right / 12px 100% no-repeat scroll;
  -webkit-overflow-scrolling:touch}
table{width:100%; border-collapse:collapse; font-size:13.5px}
th,td{padding:9px 14px; text-align:right; white-space:nowrap;
  font-variant-numeric:tabular-nums}
/* 첫 칸은 '무엇에 대한 줄인가' — 눈이 여기서 출발하므로 왼쪽 정렬 + 굵게 */
th:first-child,td:first-child{text-align:left; padding-left:10px;
  font-variant-numeric:normal; font-weight:600; color:var(--ink)}
th:last-child,td:last-child{padding-right:10px}
thead th{font-size:11px; font-weight:700; color:var(--ink-2);
  letter-spacing:.04em; border-bottom:1.5px solid var(--rule); padding-bottom:8px}
/* ── green-bar 줄무늬 ──
   연속용지 회계 보고서의 그 초록 띠다. 장식이 아니라 **기능**이다 —
   칸이 많은 표에서 시선이 다른 줄로 미끄러지는 걸 막는다. */
tbody tr:nth-child(even){background:var(--row)}
tbody tr{border-bottom:1px solid var(--line)}
/* 마지막 줄은 이중 괘선 — 장부에서 합계 아래에 긋던 그 선 */
tbody tr:last-child{border-bottom:3px double var(--rule)}
tbody tr:hover{background:var(--accent-soft)}
tbody td{color:var(--ink-2)}
tbody td:first-child{color:var(--ink)}
td.empty{text-align:center; color:var(--ink-3); padding:30px; font-size:14px;
  font-weight:500}
td a{text-decoration:none; font-weight:600; border-bottom:1px solid transparent}
td a:hover{border-bottom-color:currentColor}

/* ── 폼 ── */
form.pick{display:flex; gap:7px; align-items:center; flex-wrap:wrap}
form.pick label{font-size:10.5px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink-3)}
input[type=date],input[type=number]{font:inherit; font-size:13px; padding:5px 8px;
  border-radius:2px; border:1px solid var(--rule); background:transparent;
  color:var(--band-ink); font-variant-numeric:tabular-nums}
.sheet input[type=date],.sheet input[type=number]{color:var(--ink);
  border-color:var(--line-2); background:var(--sheet)}
button{font:inherit; font-size:13px; font-weight:600; padding:5px 13px;
  border-radius:2px; border:1px solid var(--accent); background:var(--accent);
  color:var(--on-accent); cursor:pointer; letter-spacing:.02em}
button:hover{filter:brightness(1.12)}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
.presets{display:inline-flex; gap:4px; margin-left:3px}
button.ghost{background:transparent; color:var(--band-ink);
  border-color:var(--rule); font-weight:500}
.sheet button.ghost{color:var(--ink-2); border-color:var(--line-2)}
button.ghost:hover{background:rgba(127,127,127,.14); filter:none}

/* ── 그래프 ──
   골격: .plot 안에 y눈금(왼쪽 고정폭)과 .pane(그림+x라벨)이 나란히 선다.
   x라벨을 바깥 상자에 붙이면 눈금 폭만큼 어긋나므로 반드시 .pane 안에 둔다. */
.chart{margin-top:10px}
.plot{position:relative; padding-left:46px}
.pane{position:relative}
/* y눈금 — 격자선만 있고 숫자가 없으면 막대 높이가 얼마인지 읽을 수 없다 */
.yaxis{position:absolute; left:0; top:0; width:42px; height:180px}
.yaxis span{position:absolute; right:0; transform:translateY(-50%);
  font-size:11.5px; font-weight:600; color:var(--ink-2); white-space:nowrap;
  font-variant-numeric:tabular-nums}
.chart svg{width:100%; height:180px; display:block; overflow:visible}
.chart .bar{fill:var(--accent); opacity:.85}
.chart .bar:hover{opacity:1}
.chart .ma{fill:none; stroke:var(--ink); stroke-width:.45;
  vector-effect:non-scaling-stroke; opacity:.62}
.chart .grid{stroke:var(--line); stroke-width:.5; vector-effect:non-scaling-stroke}
.chart .axis{stroke:var(--rule); stroke-width:.8; vector-effect:non-scaling-stroke}
/* x축 라벨은 SVG **밖**에 HTML 로 그린다.
   preserveAspectRatio="none" 은 막대·선에는 맞지만(가로로 늘려 폭을 채워야 한다)
   글자까지 같이 늘린다. 실측: viewBox 100 폭이 약 1104px 로 그려지므로 X 11배 /
   Y 1.07배 — 라벨이 높이 3.3px 에 가로만 10배로 뭉개졌다.
   HTML 로 빼면 글자는 정상 크기로 나오고, 막대 중심 정렬은 left:%% 로 맞춘다. */
.xlabels{position:relative; height:15px; margin-top:2px}
.xlabels span{position:absolute; transform:translateX(-50%); white-space:nowrap;
  font-size:11px; color:var(--ink-3); font-variant-numeric:tabular-nums}
/* 양 끝 라벨이 시트 밖으로 잘리지 않게 안쪽으로 붙인다 */
.xlabels span.first{transform:none}
.xlabels span.last{transform:translateX(-100%)}
/* 좁은 화면에서 최소 간격이 43px 까지 좁아진다(라벨 24개일 때) — 글자를 줄여 여유를 준다 */
@media (max-width:480px){ .xlabels span{font-size:10px; letter-spacing:-.02em} }

/* ── 탭 ──
   자바스크립트 없이 라디오 + :checked 형제 선택자로 만든다. 리포트는 CSP 가 엄격한
   환경이나 오프라인에서도 열려야 하고, 스냅샷은 몇 달 뒤에 열릴 수 있다.
   라디오는 화면에서 숨기되 **접근성 트리에는 남긴다**(display:none 이면 키보드로
   못 옮긴다) — 화면 밖으로 밀어내고 label 에 포커스 링을 준다. */
.tabs{margin-top:18px}
.tabs > input[type=radio]{position:absolute; opacity:0; pointer-events:none;
  width:1px; height:1px}
.tabbar{display:flex; flex-wrap:wrap; gap:2px; border-bottom:2px solid var(--rule);
  margin-bottom:2px}
.tabbar label{padding:10px 15px; font-size:14px; font-weight:650; cursor:pointer;
  color:var(--ink-3); border:1px solid transparent; border-bottom:none;
  border-radius:3px 3px 0 0; margin-bottom:-2px; white-space:nowrap;
  display:inline-flex; align-items:center; gap:7px; min-height:44px}
.tabbar label:hover{color:var(--ink); background:var(--surface-2)}
.tabbar label .n{font-size:11.5px; font-weight:600; color:var(--ink-3);
  font-variant-numeric:tabular-nums}
.tabpane{display:none}
/* 라디오가 켜지면 같은 순서의 탭이 활성화된다. 순서에 의존하므로 마크업에서
   input 들을 **한 곳에 모아** 두고 pane 순서를 그대로 맞춘다. */
#t1:checked ~ .tabbar label[for=t1],
#t2:checked ~ .tabbar label[for=t2],
#t3:checked ~ .tabbar label[for=t3],
#t4:checked ~ .tabbar label[for=t4],
#t5:checked ~ .tabbar label[for=t5]{
  color:var(--ink); border-color:var(--line-2); background:var(--sheet);
  border-bottom:2px solid var(--sheet)}
#t1:checked ~ .tabbar label[for=t1] .n,
#t2:checked ~ .tabbar label[for=t2] .n,
#t3:checked ~ .tabbar label[for=t3] .n,
#t4:checked ~ .tabbar label[for=t4] .n,
#t5:checked ~ .tabbar label[for=t5] .n{color:var(--accent)}
#t1:checked ~ #p1, #t2:checked ~ #p2, #t3:checked ~ #p3,
#t4:checked ~ #p4, #t5:checked ~ #p5{display:block}
.tabs > input[type=radio]:focus-visible ~ .tabbar label[for]{outline:none}
#t1:focus-visible ~ .tabbar label[for=t1],
#t2:focus-visible ~ .tabbar label[for=t2],
#t3:focus-visible ~ .tabbar label[for=t3],
#t4:focus-visible ~ .tabbar label[for=t4],
#t5:focus-visible ~ .tabbar label[for=t5]{outline:2px solid var(--accent); outline-offset:-2px}
/* 탭 안의 첫 구획은 위 괘선을 지운다 — 탭바 선과 겹쳐 두 줄로 보인다 */
.tabpane > .panel:first-child{border-top:none; margin-top:14px; padding-top:0}
@media (max-width:720px){
  .tabbar{gap:0}
  .tabbar label{padding:10px 11px; font-size:13px}
}
/* 인쇄할 땐 탭을 풀어 전부 보여 준다 — 종이에는 클릭이 없다 */
@media print{ .tabbar{display:none} .tabpane{display:block !important} }

/* ── 상단 차트 ──
   막대(.chart)와 **스케일 규약이 다르다**: 이쪽은 비율을 유지한다(xMidYMid meet).
   원이 타원이 되면 안 되고 각도가 틀어지면 파이가 거짓말을 하기 때문이다.
   비율이 유지되므로 글자를 SVG 안에 둬도 안 뭉개진다. */
.figwrap{overflow-x:auto; margin:12px 0 4px}
.fig{width:100%; height:auto; min-width:520px; display:block}
.fig-sm{min-width:200px; max-width:240px; margin:0 auto}
.fig-split{display:grid; gap:clamp(12px,3vw,32px); align-items:center;
  grid-template-columns:minmax(200px,240px) 1fr}
.legend{display:flex; flex-direction:column; gap:2px; min-width:0}
.lg-row{display:grid; grid-template-columns:12px 1fr auto auto; gap:10px;
  align-items:center; padding:7px 0; border-bottom:1px solid var(--line);
  font-size:13.5px; font-variant-numeric:tabular-nums}
.lg-row:last-child{border-bottom:none}
.lg-dot{width:11px; height:11px; border-radius:2px; display:block}
.lg-name{min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.lg-val{font-weight:650}
.lg-pct{color:var(--ink-3); min-width:34px; text-align:right}
@media (max-width:720px){
  .fig-split{grid-template-columns:1fr}
  .fig{min-width:440px}
}

/* ── 세션 상세(프로젝트 클릭) ──
   원본 JSON 을 그대로 던지면 대화가 \n 이 박힌 한 줄로 뭉개져 못 읽는다.
   말한 사람과 말한 내용을 갈라 놓는 것만으로 읽을 수 있게 된다. */
.convo{display:flex; flex-direction:column; gap:2px}
.msg{display:grid; grid-template-columns:78px 1fr; gap:14px; padding:11px 12px;
  border-bottom:1px solid var(--line)}
.msg:nth-child(even){background:var(--row)}
.msg-who{font-size:11.5px; font-weight:700; letter-spacing:.04em;
  color:var(--ink-3); padding-top:2px}
.msg-u .msg-who{color:var(--accent)}
/* pre-wrap: 학생·튜터가 쓴 줄바꿈과 빈 줄을 그대로 살린다.
   normal 로 두면 여러 문단이 한 줄로 뭉개져 대화를 읽을 수 없다. */
.msg-body{white-space:pre-wrap; word-break:break-word; line-height:1.72}
details.code{border-bottom:1px solid var(--line); padding:9px 12px}
details.code summary{cursor:pointer; font-weight:650; font-size:14px}
details.code pre{margin:10px 0 4px; padding:13px; background:var(--surface-2);
  border-left:3px solid var(--accent); overflow-x:auto; font-size:12.5px;
  line-height:1.6}
.panel pre{margin:0; padding:13px; background:var(--surface-2);
  border-left:3px solid var(--line-2); overflow-x:auto; font-size:12.5px;
  line-height:1.6; white-space:pre-wrap; word-break:break-word}
ul.notes{margin:0; padding-left:22px; line-height:1.75}
ul.notes li{margin-bottom:7px}
.backlink{font-size:13px; font-weight:600}

/* ── 토큰 구성 ── */
.mix{margin-top:2px}
.mix-row{display:grid; align-items:center; gap:11px; padding:5px 0;
  grid-template-columns:118px 1fr 54px 124px; border-bottom:1px solid var(--line)}
.mix-row:last-child{border-bottom:none}
.mix-name{font-size:12.5px}
.mix-mult{color:var(--ink-3); font-size:11px; margin-left:5px}
.mix-track{height:6px; background:var(--surface-2);
  border:1px solid var(--line); overflow:hidden}
.mix-fill{display:block; height:100%; background:var(--accent)}
.mix-pct{text-align:right; font-size:12.5px; font-variant-numeric:tabular-nums}
.mix-raw{text-align:right; font-size:11.5px; color:var(--ink-3);
  font-variant-numeric:tabular-nums}

/* ── 예산 ── */
.budget-track{height:10px; background:var(--surface-2);
  border:1px solid var(--line); overflow:hidden}
.budget-fill{display:block; height:100%; background:var(--accent)}
.budget-fill.ok{background:var(--ok)}
.budget-fill.warn{background:var(--warn)}
.budget-fill.crit{background:var(--crit)}
.budget-nums{display:flex; flex-wrap:wrap; gap:18px; margin-top:9px;
  font-size:13px; font-variant-numeric:tabular-nums}

/* ── 예측 ── */
.fc-grid{display:grid; gap:0 clamp(14px,2.4vw,30px);
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.fc{padding:2px 0 2px 13px; border-left:2px solid var(--accent)}
.fc-label{font-size:10.5px; letter-spacing:.13em; text-transform:uppercase;
  color:var(--ink-3)}
.fc-value{font-size:25px; font-weight:600; font-variant-numeric:tabular-nums;
  letter-spacing:-.015em; font-family:ui-serif,Georgia,serif}
.fc-sub{font-size:11.5px; color:var(--ink-3); font-variant-numeric:tabular-nums}
.fc-note{margin-top:14px; padding:11px 14px; background:var(--accent-soft);
  border-left:2px solid var(--accent); font-size:13px}
.fc-note.crit{background:transparent; border-left-color:var(--crit); color:var(--crit)}
.fc-caveat{font-size:11.5px; color:var(--ink-3); margin:12px 0 0; max-width:74ch}

/* ── AI 분석 ── */
.ai-head{display:flex; gap:12px; align-items:baseline; justify-content:space-between;
  flex-wrap:wrap}
.ai-body{font-size:14px; margin-top:12px; max-width:78ch}
.ai-body h2{font-size:11px; letter-spacing:.13em; text-transform:uppercase;
  color:var(--accent); margin:20px 0 5px; font-weight:650}
.ai-body h2:first-child{margin-top:0}
.ai-body p{margin:0 0 9px}
.ai-body ul{margin:0 0 10px; padding-left:18px}
.ai-body li{margin-bottom:4px}
.ai-status{font-size:12.5px; color:var(--ink-3)}
.ai-meta{font-size:11px; color:var(--ink-3); margin-top:14px;
  padding-top:9px; border-top:1px solid var(--line)}

/* ── 기타 ── */
.pager{display:flex; flex-wrap:wrap; gap:12px; align-items:center;
  justify-content:space-between; margin-top:11px; font-size:12.5px;
  font-variant-numeric:tabular-nums}
.pg-info{color:var(--ink-3)}
.pg-nav{display:inline-flex; gap:4px; align-items:center}
.pg-a{padding:3px 9px; border:1px solid var(--line-2); text-decoration:none;
  color:var(--ink-2)}
.pg-a:hover{background:var(--surface-2); color:var(--ink)}
.pg-off{padding:3px 9px; border:1px solid var(--line); color:var(--ink-3);
  opacity:.5}
.pg-cur{padding:0 8px; color:var(--ink-3)}
.tsearch{display:flex; gap:6px; align-items:center; margin:10px 0 2px}
.tsearch input[type=search]{font:inherit; font-size:13px; padding:5px 9px;
  border:1px solid var(--line-2); background:var(--sheet); color:var(--ink);
  border-radius:2px; min-width:min(280px,60vw)}
.chips{display:flex; flex-wrap:wrap; gap:5px; margin-top:9px}
.chip{display:inline-flex; align-items:center; gap:6px; font-size:11px;
  padding:2px 8px; border:1px solid var(--line-2); color:var(--ink-2)}
.chip b{color:var(--ink); font-variant-numeric:tabular-nums}
.pin{display:inline-block; margin-left:7px; font-size:10px; padding:0 5px;
  border:1px solid var(--accent); color:var(--accent); vertical-align:1px;
  letter-spacing:.05em}
td.spark{padding-top:11px; padding-bottom:11px; width:110px}
td.spark .mix-track{display:block; width:100%}
.note{font-size:12.5px; color:var(--ink-2); padding:13px 0 0;
  border-top:1px solid var(--line); margin-top:22px; max-width:80ch}
.note b{color:var(--ink)}
.err h1{color:var(--crit)}
footer{margin-top:30px; padding-top:13px; border-top:2px solid var(--rule);
  font-size:11.5px; color:var(--ink-3)}
code{background:var(--surface-2); border:1px solid var(--line);
  padding:1px 5px; font-size:11.5px}
/* ──────────────────────────────────────────────────────────────────────────
   좁은 화면
   기준은 '글자가 작아지는 것'이 아니라 **손가락과 시선**이다:
     · 표는 가로로 넘치되 무엇에 대한 줄인지(첫 칸)는 안 사라져야 한다
     · 누를 것은 최소 44px — 안 그러면 옆 줄이 눌린다
     · 입력 글자가 16px 미만이면 iOS 가 focus 때 화면을 확대해 레이아웃이 튄다
   ────────────────────────────────────────────────────────────────────────── */
@media (max-width:720px){
  .hero{grid-template-columns:1fr; gap:22px}
  .mix-row{grid-template-columns:92px 1fr 46px}
  .mix-raw{display:none}
  th,td{padding:10px 10px}
  form.pick{width:100%}
  form.pick label{width:100%; margin-top:4px}
  /* iOS 는 16px 미만 입력에 focus 하면 페이지를 확대한다 — 그러면 가로 스크롤이
     생기고 되돌아오지 않는다. 확대를 막는 대신(접근성 위반) 글자를 키운다. */
  input[type=date],input[type=number],button{font-size:16px; padding:9px 12px}
  .presets{display:flex; flex-wrap:wrap; margin-left:0; width:100%}

  /* 넓은 표를 가로로 훑을 때 라벨 칸이 같이 밀려나면 '어느 줄인지'를 잃는다.
     첫 칸을 고정한다. 줄무늬가 있으므로 배경을 줄별로 명시해야 뒤가 비쳐 보이지 않는다. */
  th:first-child,td:first-child{position:sticky; left:0; z-index:1;
    background:var(--sheet); box-shadow:1px 0 0 var(--line)}
  tbody tr:nth-child(even) td:first-child{background:var(--row)}
  thead th:first-child{background:var(--sheet)}

  /* 손가락 타깃 — 표 안 링크와 페이저 */
  td a{display:inline-block; padding:6px 0; min-height:32px}
  .pg-a{padding:10px 14px; min-height:44px; display:inline-flex; align-items:center}

  /* 대화: 78px 짜리 화자 칸이 좁은 화면에서 27% 를 먹는다 → 위아래로 쌓는다 */
  .msg{grid-template-columns:1fr; gap:3px; padding:12px 10px}
  .msg-who{padding-top:0}

  .cards{grid-template-columns:repeat(auto-fit,minmax(132px,1fr))}
  .card{padding:11px 12px}
  .card-value{font-size:26px}
  .panel-head h2{font-size:17px}
  .panel{margin-top:26px; padding-top:18px}
}
@media (max-width:480px){
  /* y축 라벨이 46px 을 먹으면 그래프가 좁아진다 — 눈금은 유지하되 폭을 줄인다 */
  .plot{padding-left:34px}
  .yaxis{width:30px}
  .yaxis span{font-size:10.5px}
  .chart svg,.yaxis{height:150px}
  .hero-total{font-size:42px}
  .stat-value{font-size:22px}
  table{font-size:13px}
  th,td{padding:9px 8px}
}
/* 가로 스크롤은 표 안에서만 일어나야 한다 — 페이지 전체가 흔들리면 읽기가 무너진다.
   원인을 각각 고쳤지만(위 규칙들), 새 요소가 들어와도 본문이 새지 않게 한 겹 더 둔다. */
html{-webkit-text-size-adjust:100%}
body{overflow-x:hidden}
.wrap{max-width:min(1180px,100%)}
"""


def _page(title: str, band: str, sheet: str) -> str:
    """짙은 마스트헤드 띠 + 흰 시트. 문서 한 장처럼 읽히게 하는 골격."""
    return f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head><body><div class="wrap">
<div class="band">{band}</div>
<div class="sheet">{sheet}</div>
</div></body></html>"""


def _band(eyebrow: str, heading: str, period: str, right: str = "") -> str:
    return f"""<div class="band-top">
    <div>
      <div class="eyebrow">{_e(eyebrow)}</div>
      <h1>{_e(heading)}</h1>
      <div class="period">{period}</div>
    </div>
    <div>{right}</div>
  </div>"""


def _hero(total_usd: Any, total_krw: Any, stats: str, caption: str = "") -> str:
    return f"""<div class="hero">
    <div>
      <div class="stat-label">총 비용</div>
      <div class="hero-total num">{_usd(total_usd)}</div>
      <div class="hero-sub">약 {_krw(total_krw)}{_e(caption)}</div>
    </div>
    <div class="defs">{stats}</div>
  </div>"""


# ── 공개 API ──────────────────────────────────────────────────────────────────


def render_error(message: str, *, detail: str = "", title: str = "사용량 리포트") -> str:
    """조회 실패를 사람이 읽을 수 있게. 빈 화면보다 원인을 보여주는 게 낫다."""
    d = f'<p class="muted">{_e(detail)}</p>' if detail else ""
    return _page(title,
                 _band("USAGE & COST", title, "조회 실패"),
                 f"""<div class="hero err"><div>
      <div class="stat-label">오류</div>
      <div class="hero-total num" style="font-size:34px">불러오지 못했습니다</div>
      <div class="hero-sub">{_e(message)}</div>
    </div></div>
    <section class="panel"><div class="panel-head">
      <h2>확인할 것</h2>
      <p>① rag-search(:8100)가 떠 있는지 ② <code>RAG_UPSTREAM</code> 배선
         ③ <code>usage_turns</code> 테이블 존재 여부</p>
    </div>{d}</section>""")


def render(report: Mapping[str, Any], *,
           title: str = "일별 리포트",
           budget_usd: float = 0.0,
           mode: str = "",
           show_form: bool = True,
           form_action: str = "",
           hidden_fields: Mapping[str, str] | None = None,
           insight: Mapping[str, Any] | None = None,
           can_generate: bool = False,
           insight_endpoint: str = "report/insight",
           token: str = "",
           confirmed_at_utc: str = "",
           page_qs: Mapping[str, str] | None = None,
           root: str = "") -> str:
    """하루(또는 지정 기간) 상세 화면.

    show_form=False 는 확정본 보기용이다 — 굳혀 둔 값을 보는 화면에서 날짜를 바꾸는
    폼은 의미가 없다.
    """
    if not report or not report.get("ok"):
        return render_error("업스트림이 리포트를 주지 않았습니다",
                            detail=str(report.get("error", ""))[:300] if report else "",
                            title=title)

    period = report.get("period") or {}
    totals = report.get("totals") or {}
    assume = report.get("assumptions") or {}
    projects = report.get("projects") or {}
    # 페이저·검색 링크가 물고 갈 현재 쿼리(토큰·기간 등). 없으면 링크가 컨텍스트를 잃는다.
    # 토큰은 여기서 못 박는다 — 호출부가 잊어도 링크가 404 가 되지 않게(_with_token 주석)
    page_qs = _with_token(page_qs, token)
    krw_rate = _f(assume.get("krw_per_usd")) or 1400.0
    start, end = str(period.get("start", ""))[:10], str(period.get("end", ""))[:10]
    span = start if start == end else f"{start} ~ {end}"

    first, last = str(period.get("first_turn") or ""), str(period.get("last_turn") or "")
    trail = f" · {first[11:16]}–{last[11:16]}" if first and last else ""
    mode_chip = f'<span class="mode">{_e(mode)}</span>' if mode else ""
    stamp = (f' · 확정 {_kst(confirmed_at_utc)}' if confirmed_at_utc else "")

    hid = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                  for k, v in (hidden_fields or {}).items())
    _tq = f"?token={_e(token)}" if token else ""
    # 확정본 화면에는 **지금 값으로 가는 길**을 함께 둔다.
    # 확정본은 굳힌 시점의 값이라 "지금 23:40 인데 화면은 18:11 값"이 되는데,
    # 그게 화면상 오류처럼 보인다(2026-08-21 실제로 그렇게 읽혔다).
    # 오갈 수 있으면 '왜 다른가'가 스스로 설명된다.
    live = ""
    if confirmed_at_utc:
        _d = str((report.get("period") or {}).get("start") or "")[:10]
        if _d:
            _sep = "&" if _tq else "?"
            live = (f' · <a href="{_e(root)}report{_tq}{_sep}start={_e(_d)}&end={_e(_d)}"'
                    f' style="font-size:12.5px">지금 값 보기 →</a>')
    right = (f'<a href="{_e(root)}reports{_tq}" style="font-size:12.5px">'
             f'← 기간 전체 보기</a>{live}')
    if show_form:
        right = f"""<form class="pick" method="get" action="{_e(form_action)}">{hid}
      <label for="s">시작</label><input id="s" type="date" name="start" value="{_e(start)}">
      <label for="e">종료</label><input id="e" type="date" name="end" value="{_e(end)}">
      <label for="b">예산 $</label><input id="b" type="number" name="budget_usd"
        min="0" step="1" style="width:82px" value="{_e(int(budget_usd) or '')}">
      <button type="submit">조회</button>
    </form>
    <div style="margin-top:9px"><a href="{_e(root)}reports{_tq}" style="font-size:12px">← 기간 전체 보기</a></div>"""

    band = _band("USAGE & COST", title,
                 f"{_e(span)}{_e(trail)}{_e(stamp)} {mode_chip}", right)

    turns = _n(totals.get("turns"))
    stats = "".join([
        _stat("턴", _comma(turns), f"턴당 {_usd(totals.get('usd_per_turn'))}"),
        _stat("사용자", _comma(totals.get("users")),
              f"1인 {_f(totals.get('turns_per_user')):.1f}턴"),
        _stat("세션", _comma(totals.get("sessions"))),
        _stat("프로젝트", _comma(projects.get("created"))),
        _stat("가중 토큰", _comma(totals.get("weighted_tokens")), "= 비용 환산 기준"),
    ])
    hero = _hero(totals.get("usd"), totals.get("krw"), stats)

    kind_rows = [
        (f'{_e(r.get("mode"))} · {_e(r.get("coding_type"))}',
         _comma(r.get("turns")), _comma(r.get("weighted_tokens")),
         _usd(r.get("usd")), _krw(r.get("krw")))
        for r in (report.get("by_kind") or [])
    ]
    hour_rows = [
        (_e(str(r.get("hour", ""))[11:] or r.get("hour")),
         _comma(r.get("turns")), _comma(r.get("users")),
         _comma(r.get("weighted_tokens")), _usd(r.get("usd")))
        for r in (report.get("by_hour") or [])
    ]
    # 탭으로 나눈다 — 한 화면에 다 쏟으면 어디를 봐야 할지 잃는다.
    # 순서는 수업 직후에 알고 싶은 순서다: 버텼나 → 왜 느렸나 → 누가 → 얼마 → 무엇이 나왔나.
    load_b = report.get("load") or {}
    pat_b = report.get("patterns") or {}
    g = _load_groups(load_b, pat_b,
                     _n((report.get("assumptions") or {}).get("quota_max_turns")) or 70,
                     root, token)
    ch = _chart_groups(load_b, pat_b)
    sheet = hero + _tabs([
        ("요약", "", "".join([
            g.get("summary", ""),
            _ai_panel(insight, day=start, can_generate=can_generate,
                      endpoint=insight_endpoint, token=token),
            ch.get("summary", ""),
            _budget(totals, budget_usd, krw_rate),
        ])),
        ("부하", "", g.get("load", "")),
        ("사용자", "", "".join([
            g.get("users", ""),
            ch.get("users", ""),
            _patterns_panel(pat_b, base_qs=page_qs),
        ])),
        ("비용", "", "".join([
            _billing_panel(report.get("billing") or {}),
            _reuse_panel(report.get("reuse") or {}),
            ch.get("cost", ""),
            _token_mix(totals),
            _table("시간대별", "동시 사용 규모. ‘사용자’ 열이 그 시간대 활성 인원입니다.",
                   ["시각", "턴", "사용자", "가중토큰", "비용"], hour_rows,
                   empty="이 기간에 기록된 턴이 없습니다"),
            _table("모드·타입별", "어느 작업이 비싼지. quick/design × react/blockly.",
                   ["구분", "턴", "가중토큰", "비용", "원화"], kind_rows,
                   empty="이 기간에 기록된 턴이 없습니다"),
        ])),
        ("프로젝트", _comma(projects.get("created")),
         _projects_panel(projects, base_qs=page_qs, root=root)),
    ]) + f"""<div class="note">
    <b>비용 환산</b> <code>weighted / 1,000,000 = USD</code>
    ({_e(assume.get('model', '?'))} 단가 비율 일치 · 환율 {_comma(krw_rate)}원/$).
    <b>⚠ CLI 구독 모드면 실제 청구는 0원</b>입니다 — 금액은 API 과금 환산치입니다.
    <br>시각은 모두 <b>KST</b> 로 표시합니다(저장은 UTC).
  </div>
  <footer>
    원천 <code>usage_turns</code> · <code>sessions</code> (MySQL) ·
    CLI: <code>python3 scripts/usage_report.py --start {_e(start)}</code>
  </footer>"""

    return _page(f"{title} · {span}", band, sheet)


def _load_groups(load, patterns, quota_max_turns, root, token) -> dict:
    """부하/사용자 패널을 탭 단위로. 실패해도 리포트는 떠야 한다."""
    if not load and not patterns:
        return {}
    try:
        import report_load_html as RL
        return RL.panel_groups(load, patterns=patterns,
                               quota_max_turns=quota_max_turns, root=root, token=token)
    except Exception as e:
        return {"summary": ('<section class="panel"><div class="panel-head"><h2>부하</h2>'
                            '<p class="muted">부하 화면을 그리지 못했습니다 — '
                            + _e(str(e)[:120]) + '</p></div></section>')}


def _chart_groups(load, patterns) -> dict:
    """상단 차트를 탭 단위로. 실패해도 리포트는 떠야 한다."""
    if not load and not patterns:
        return {}
    try:
        import report_charts as RC
        return RC.chart_groups(load, patterns)
    except Exception as e:
        return {"summary": ('<section class="panel"><div class="panel-head"><h2>차트</h2>'
                            '<p class="muted">차트를 그리지 못했습니다 — '
                            + _e(str(e)[:120]) + '</p></div></section>')}


def _tabs(panes: Sequence[tuple[str, str, str]]) -> str:
    """탭 묶음. panes = [(제목, 부제, 내용HTML), ...] — 내용이 빈 탭은 뺀다.

    화면이 길어지면 **어디를 봐야 할지**를 잃는다. 스크롤로 다 밀어 놓는 대신,
    "무엇을 알고 싶은가"로 갈라 한 번에 한 묶음만 보여 준다.

    자바스크립트를 쓰지 않는다 — 리포트는 CSP 가 엄격한 환경이나 오프라인에서도
    열려야 하고, 확정본은 몇 달 뒤에 열릴 수 있다. 라디오 + :checked 로 충분하다.
    """
    live = [(t, sub, body) for t, sub, body in panes if body.strip()]
    if len(live) < 2:
        return "".join(b for _, _, b in live)
    inputs = "".join(
        f'<input type="radio" name="tab" id="t{i + 1}"'
        f'{" checked" if i == 0 else ""}>' for i in range(len(live)))
    bar = "".join(
        f'<label for="t{i + 1}">{_e(t)}'
        f'{f"<span class=n>{_e(sub)}</span>" if sub else ""}</label>'
        for i, (t, sub, _) in enumerate(live))
    panes_html = "".join(
        f'<div class="tabpane" id="p{i + 1}">{body}</div>'
        for i, (_, _, body) in enumerate(live))
    return (f'<div class="tabs">{inputs}<div class="tabbar">{bar}</div>'
            f'{panes_html}</div>')


def _top_charts(load: Mapping[str, Any], patterns: Mapping[str, Any]) -> str:
    """상단 차트 — 표보다 먼저. 숫자를 읽기 전에 '모양'을 봐야 어느 표를 볼지 정해진다.

    차트가 깨져도 리포트는 떠야 한다(부하 패널과 동일 원칙).
    """
    if not load and not patterns:
        return ""
    try:
        import report_charts as RC
        return RC.all_charts(load, patterns)
    except Exception as e:
        return ('<section class="panel"><div class="panel-head"><h2>차트</h2>'
                '<p class="muted">차트를 그리지 못했습니다 — '
                + _e(str(e)[:120]) + '</p></div></section>')


def _load_panels(load: Mapping[str, Any], *, patterns: Mapping[str, Any] | None = None,
                 quota_max_turns: int = 70, root: str = "", token: str = "") -> str:
    """부하 패널 묶음. 모듈을 함수 안에서 불러 순환 임포트를 피한다.

    부하 화면이 깨져도 **비용 리포트는 떠야 한다** — 청구 근거가 부가 지표 때문에
    통째로 안 나오면 본말이 전도된다(프로젝트 블록과 동일 원칙).
    """
    if not load:
        return ""
    try:
        import report_load_html as RL
        return RL.all_panels(load, patterns=patterns, quota_max_turns=quota_max_turns,
                             root=root, token=token)
    except Exception as e:
        return ('<section class="panel"><div class="panel-head">'
                '<h2>부하</h2><p class="muted">부하 화면을 그리지 못했습니다 — '
                + _e(str(e)[:120]) + '</p></div></section>')


def _projects_panel(projects: Mapping[str, Any], *,
                    base_qs: Mapping[str, str] | None = None,
                    root: str = "") -> str:
    """기간 내 만들어진 프로젝트 수와 목록.

    비용만 보면 "얼마 썼나"는 알아도 "무엇이 나왔나"는 모른다. 수업 성과는 결과물
    개수로 읽히므로 같은 화면에 둔다.
    """
    if not projects:
        return ""
    if not projects.get("ok", True):
        return f"""<section class="panel"><div class="panel-head">
      <h2>프로젝트</h2>
      <p class="muted">목록을 불러오지 못했습니다 — {_e(projects.get('error', ''))}</p>
    </div></section>"""

    items = list(projects.get("items") or [])
    qs = dict(base_qs or {})

    def link(it) -> str:
        """작품을 **읽을 수 있는 화면**으로 보내는 링크.

        예전엔 `projects/<sid>.json` 으로 보냈는데, 그러면 원본 JSON 이 그대로 열려
        대화가 `\n` 이 박힌 한 줄로 뭉개졌다 — 사실상 못 읽는다.
        지금은 리포트 안의 세션 상세(`report/session/<sid>`)로 간다.

        상대경로다 — 앱은 자신이 /agent 아래 붙어 있는지 모른다(프록시가 붙였다 뗀다).
        `/report` 기준으로 `report/session/<sid>` 는 `/agent/report/session/...` 로 풀린다.
        """
        sid = it.get("session_id")
        title = _e(it.get("title") or "(제목 없음)")
        if not sid:
            return title
        href = f"{_e(root)}report/session/{_e(sid)}"
        tok = (base_qs or {}).get("token") if base_qs else ""
        if tok:
            href += f"?token={_e(tok)}"
        return f'<a href="{href}" title="{_e(sid)}">{title}</a>'

    def cost_cells(it) -> tuple[str, str]:
        """턴 수와 비용. 관측 시작 이전 프로젝트는 '0원'이 아니라 '기록 없음'이다.

        여기를 0 으로 보여 주면 백필된 과거 프로젝트가 전부 공짜로 만들어진 것처럼
        읽힌다 — 프로젝트 수와 비용의 계보가 다르다는 사실이 숫자에서 지워진다.
        """
        if not it.get("measured"):
            return ('<span class="muted" title="사용량 기록 시작 이전에 만들어진 '
                    '프로젝트입니다">—</span>', '<span class="muted">—</span>')
        w = _n(it.get("weighted_tokens"))
        return _comma(it.get("turns")), _usd(w / 1_000_000)

    rows = []
    for it in items:
        turns_cell, cost_cell = cost_cells(it)
        rows.append((
            _e(it.get("created_at", ""))[5:16],
            link(it),
            _e(it.get("coding_type") or "-"),
            _e(it.get("phase") or "-"),
            f'<span title="{_e(it.get("user_id"))}">{_e(_short_user(it.get("user_id")))}</span>',
            turns_cell,
            cost_cell,
        ))

    chips = "".join(
        f'<span class="chip">{_e(t.get("coding_type"))} · {_e(t.get("app_type"))}'
        f'<b>{_comma(t.get("n"))}</b></span>'
        for t in (projects.get("by_type") or [])[:12])

    q = str(projects.get("q") or "")
    matched = _n(projects.get("matched"))
    created = _n(projects.get("created"))
    search = _search_form(param="pq", value=q, base_qs=qs,
                          placeholder="제목·사용자·세션 ID 로 검색")
    pager = _pager(total=matched, offset=_n(projects.get("offset")),
                   page_size=_n(projects.get("page_size")) or 25,
                   param="poff", base_qs={**qs, "pq": q}, label="개")

    sub = "이 기간에 학생들이 만든 결과물입니다. 비용이 무엇으로 바뀌었는지 보는 칸입니다."
    if q:
        sub = f"‘{q}’ 검색 결과 {_comma(matched)}개 (전체 {_comma(created)}개 중)"

    # ── 검색 결과의 비용 ────────────────────────────────────────────────────
    # 기간 총액은 검색과 무관하게 그대로 둔다 — 그게 검색어에 따라 흔들리면 숫자를
    # 믿을 수 없게 된다. 대신 "이 검색에 해당하는 비용"을 별도 줄로 덧붙인다.
    mc = projects.get("matched_cost") or {}
    cost_line = ""
    if _n(mc.get("turns")):
        scope = f"‘{q}’ 검색 결과" if q else "이 기간 프로젝트"
        cost_line = (
            f'<p class="muted">{_e(scope)}의 사용량 — '
            f'<b>{_comma(mc.get("turns"))}턴</b> · '
            f'<b>{_usd(_n(mc.get("weighted_tokens")) / 1_000_000)}</b> · '
            f'세션 {_comma(mc.get("sessions"))}개'
            f'{" (기간 총액과 별개입니다)" if q else ""}</p>')
    elif created:
        cost_line = ('<p class="muted">이 목록의 프로젝트에는 사용량 기록이 없습니다 — '
                     '관측 시작 이전에 만들어졌습니다.</p>')

    head = f"""<section class="panel">
  <header class="panel-head">
    <h2>프로젝트 {_comma(created)}개 생성</h2>
    <p>{_e(sub)}</p>
    {cost_line}
    {f'<div class="chips">{chips}</div>' if chips else ''}
  </header>{search}"""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    if not body:
        empty = ("검색과 일치하는 프로젝트가 없습니다" if q
                 else "이 기간에 만들어진 프로젝트가 없습니다")
        body = f'<tr><td class="empty" colspan="7">{_e(empty)}</td></tr>'
    return head + f"""<div class="scroll"><table>
    <thead><tr><th>생성</th><th>제목</th><th>타입</th><th>단계</th><th>만든 사람</th>
    <th>턴</th><th>비용</th></tr></thead>
    <tbody>{body}</tbody></table></div>{pager}</section>"""


def render_index(report: Mapping[str, Any], *,
                 title: str = "기간 리포트",
                 budget_usd: float = 0.0,
                 mode: str = "",
                 form_action: str = "",
                 hidden_fields: Mapping[str, str] | None = None,
                 detail_href: str = "report",
                 confirmed: Mapping[str, Mapping[str, Any]] | None = None,
                 insight: Mapping[str, Any] | None = None,
                 can_generate: bool = False,
                 insight_endpoint: str = "report/insight",
                 token: str = "",
                 page_qs: Mapping[str, str] | None = None,
                 root: str = "") -> str:
    """기간 화면 — 위에 전체 요약, 아래에 하루 한 줄로 쌓인다.

    개별 리포트와 **같은 집계**를 쓴다. 화면마다 따로 계산하면 같은 날 숫자가
    화면에 따라 달라진다.
    """
    if not report or not report.get("ok"):
        return render_error("업스트림이 리포트를 주지 않았습니다",
                            detail=str(report.get("error", ""))[:300] if report else "",
                            title=title)

    period = report.get("period") or {}
    totals = report.get("totals") or {}
    assume = report.get("assumptions") or {}
    projects = report.get("projects") or {}
    days = list(report.get("by_day") or [])
    conf = dict(confirmed or {})
    # 토큰은 여기서 못 박는다 — 호출부가 잊어도 링크가 404 가 되지 않게(_with_token 주석)
    page_qs = _with_token(page_qs, token)
    krw_rate = _f(assume.get("krw_per_usd")) or 1400.0
    start, end = str(period.get("start", ""))[:10], str(period.get("end", ""))[:10]
    span = start if start == end else f"{start} ~ {end}"

    hid = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                  for k, v in (hidden_fields or {}).items())
    presets = "".join(
        f'<button type="submit" name="preset" value="{v}" class="ghost">{_e(lbl)}</button>'
        for v, lbl in (("today", "오늘"), ("7d", "7일"),
                       ("30d", "30일"), ("month", "이번 달")))
    form = f"""<form class="pick" method="get" action="{_e(form_action)}">{hid}
      <label for="s">시작</label><input id="s" type="date" name="start" value="{_e(start)}">
      <label for="e">종료</label><input id="e" type="date" name="end" value="{_e(end)}">
      <label for="b">예산 $</label><input id="b" type="number" name="budget_usd"
        min="0" step="1" style="width:82px" value="{_e(int(budget_usd) or '')}">
      <button type="submit">조회</button>
      <span class="presets">{presets}</span>
    </form>"""
    mode_chip = f'<span class="mode">{_e(mode)}</span>' if mode else ""
    band = _band("USAGE & COST", title,
                 f"{_e(span)} · {len(days)}일 기록 {mode_chip}", form)

    turns = _n(totals.get("turns"))
    active = [d for d in days if _f(d.get("usd")) > 0]
    n_active = len(active) or 1
    stats = "".join([
        _stat("수업일 평균", _usd(_f(totals.get("usd")) / n_active),
              f"{turns // n_active:,}턴/일"),
        _stat("총 턴", _comma(turns), f"턴당 {_usd(totals.get('usd_per_turn'))}"),
        _stat("사용자", _comma(totals.get("users")),
              f"1인 {_f(totals.get('turns_per_user')):.1f}턴"),
        _stat("프로젝트", _comma(projects.get("created")),
              f"세션 {_comma(totals.get('sessions'))}"),
        _stat("가중 토큰", _comma(totals.get("weighted_tokens")), "= 비용 환산 기준"),
    ])
    hero = _hero(totals.get("usd"), totals.get("krw"), stats,
                 caption=f" · 기록 {len(days)}일 중 사용 {len(active)}일")

    # ── 일자별 표
    peak = max((_f(d.get("usd")) for d in days), default=0.0) or 1.0
    running, cum = 0.0, {}
    for i in range(len(days) - 1, -1, -1):
        running += _f(days[i].get("usd"))
        cum[i] = running

    rows = ""
    for i, d in enumerate(days):
        day = str(d.get("day", ""))
        usd = _f(d.get("usd"))
        pinned = day in conf
        # ⚠ 토큰을 반드시 실어야 한다. 리포트는 fail-closed 라 토큰 없는 링크는
        #   그냥 404 다(403 이 아니라 404 라 '왜 안 되지'로 더 헷갈린다).
        #   2026-08-21: 일자 링크에 토큰이 빠져 날짜를 누르면 에러가 났다.
        tq = f"token={_e(token)}&" if token else ""
        # ★ 날짜를 누르면 **항상 라이브**로 간다.
        #   예전엔 확정본이 있는 날은 확정본으로 보냈는데, 확정본은 굳힌 시점의 값이라
        #   "지금 23:40 인데 화면은 18:11 값" 같은 혼란이 생겼다(2026-08-21 실제).
        #   확정본은 배지에서 따로 열도록 남겨 둔다 — 청구 근거로 필요할 때만 본다.
        href = f'{_e(root)}{_e(detail_href)}?{tq}start={_e(day)}&end={_e(day)}'
        aq = f'?token={_e(token)}' if token else ''
        badge = (f'<a class="pin" href="{_e(root)}{_e(detail_href)}/archive/{_e(day)}{aq}"'
                 f' title="그날 굳혀 둔 확정본(이후 변하지 않음)">확정본</a>'
                 if pinned else "")
        rows += f"""<tr>
      <td><a href="{href}">{_e(day)}</a>{badge}</td>
      <td>{_comma(d.get('turns'))}</td>
      <td>{_comma(d.get('users'))}</td>
      <td>{_comma(d.get('sessions'))}</td>
      <td>{_comma(d.get('projects'))}</td>
      <td>{_comma(d.get('input_tokens'))}</td>
      <td>{_comma(d.get('output_tokens'))}</td>
      <td>{_comma(_n(d.get('cache_creation_tokens')) + _n(d.get('cache_read_tokens')))}</td>
      <td>{_comma(d.get('weighted_tokens'))}</td>
      <td><b>{_usd(usd)}</b></td>
      <td>{_krw(d.get('krw'))}</td>
      <td class="spark"><span class="mix-track"><span class="mix-fill"
        style="width:{_bar(usd / peak)}%"></span></span></td>
      <td class="muted">{_usd(cum[i])}</td>
    </tr>"""
    if not rows:
        rows = '<tr><td class="empty" colspan="13">이 기간에 기록된 턴이 없습니다</td></tr>'

    day_table = f"""<section class="panel">
  <header class="panel-head">
    <h2>일자별</h2>
    <p>날짜를 누르면 그날 상세로 갑니다. <span class="pin">확정</span> 은 밤 배치가
       DB 에 굳혀 둔 값으로, 나중에 집계가 바뀌어도 변하지 않습니다.</p>
  </header>
  <div class="scroll"><table>
    <thead><tr>
      <th>날짜</th><th>턴</th><th>사용자</th><th>세션</th><th>프로젝트</th>
      <th>입력</th><th>출력</th><th>캐시</th><th>가중토큰</th>
      <th>비용</th><th>원화</th><th>추이</th><th>누적</th>
    </tr></thead><tbody>{rows}</tbody>
  </table></div>
</section>"""

    sheet = hero + "".join([
        _chart(days),
        _forecast_panel(days, budget_usd, krw_rate),
        _ai_panel(insight, day=end or start, can_generate=can_generate,
                  endpoint=insight_endpoint, token=token),
        _budget(totals, budget_usd, krw_rate),
        _billing_panel(report.get("billing") or {}),
        _reuse_panel(report.get("reuse") or {}),
        day_table,
        _token_mix(totals),
        # 부하 블록 — "얼마나 버텼나". 비용 뒤·사용자 앞에 둔다: 수업 직후에
        # 알고 싶은 순서가 비용 → 부하 → 누가·무엇이기 때문이다.
        _top_charts(report.get("load") or {}, report.get("patterns") or {}),
        _load_panels(report.get("load") or {},
                     patterns=report.get("patterns") or {},
                     quota_max_turns=_n((report.get("assumptions") or {}).get("quota_max_turns")) or 70,
                     root=root, token=token),
        _patterns_panel(report.get("patterns") or {}, base_qs=page_qs),
        _projects_panel(projects, base_qs=page_qs, root=root),
    ]) + f"""<div class="note">
    <b>비용 환산</b> <code>weighted / 1,000,000 = USD</code>
    ({_e(assume.get('model', '?'))} 단가 비율 일치 · 환율 {_comma(krw_rate)}원/$).
    <b>⚠ CLI 구독 모드면 실제 청구는 0원</b>입니다 — 금액은 API 과금 환산치입니다.
    <br>시각은 모두 <b>KST</b> 로 표시합니다(저장은 UTC).
  </div>
  <footer>
    원천 <code>usage_turns</code> · <code>sessions</code> · 확정본 <code>usage_reports</code> (MySQL) ·
    CLI: <code>python3 scripts/usage_report.py --start {_e(start)} --end {_e(end)}</code>
  </footer>"""

    return _page(f"{title} · {span}", band, sheet)


# ── 그래프 ────────────────────────────────────────────────────────────────────
#
# 차트 라이브러리를 쓰지 않고 SVG 를 직접 그린다. 이 페이지는 오프라인·엄격한 CSP
# 에서도 열려야 하고, 스냅샷은 몇 달 뒤에 열린다 — 그때 CDN 이 살아 있다는 보장이 없다.


def _yticks(ticks: Sequence[tuple[float, str]], viewbox_h: float) -> str:
    """y축 눈금 — 값·위치를 짝으로 받아 HTML 로 그린다.

    격자선만 있고 숫자가 없으면 높이가 무엇을 뜻하는지 알 수 없다("피크 6" 이라고
    글로 써 둬도 중간 지점이 3 인지 4 인지는 못 읽는다).

    x라벨과 같은 이유로 SVG 밖이다 — preserveAspectRatio="none" 이 글자를 늘린다.
    위치는 viewBox 좌표를 **높이 비율**로 바꿔 넘긴다(SVG 높이가 CSS 로 고정이라 정확).
    """
    if not ticks:
        return ""
    out = []
    for y, label in ticks:
        out.append(f'<span style="top:{y / viewbox_h * 100:.2f}%">{_e(label)}</span>')
    return f'<div class="yaxis">{"".join(out)}</div>'


def _plot(svg: str, *, yticks: str = "", xlabels: str = "") -> str:
    """차트 골격 — y눈금 · 그림 · x라벨이 **같은 상자 기준**으로 정렬되게 감싼다.

    y눈금 자리만큼 안쪽으로 들어간 `.pane` 안에 svg 와 x라벨을 함께 두는 게 핵심이다.
    바깥 상자에 x라벨을 붙이면 눈금 폭만큼 어긋난다.
    """
    return (f'<div class="chart"><div class="plot">{yticks}'
            f'<div class="pane">{svg}{xlabels}</div></div></div>')


def _xlabels(labels: Sequence[str], n_slots: int, *, max_labels: int = 6) -> str:
    """차트 아래 x축 라벨 — SVG 가 아니라 HTML 로 그린다.

    왜 SVG 밖인가: 차트는 폭을 꽉 채워야 해서 preserveAspectRatio="none" 으로 늘리는데,
    그러면 **글자도 같이 늘어난다.** 실측(2026-08-21)으로 viewBox 100 폭이 약 1104px 로
    그려져 가로 11배 / 세로 1.07배 — 라벨이 높이 3.3px 에 가로만 10배로 뭉개졌다.
    막대·선은 늘어나야 맞고 글자는 늘어나면 안 되므로, 둘을 분리한다.

    labels 는 슬롯 개수만큼의 전체 라벨이고, 촘촘하면 골라 낸다(겹치면 못 읽는다).
    """
    n = len(labels)
    if not n or n_slots <= 0:
        return ""
    step = max(1, -(-n // max_labels))          # 올림 나눗셈
    picks = list(range(0, n, step))
    # 마지막 지점은 항상 보여 준다 — 없으면 "언제까지의 데이터인가"를 화면에서 알 수 없다.
    # 다만 **덧붙이지 않고 마지막 항목을 교체한다.** 그냥 append 하면 직전 라벨과
    # 간격이 step 의 일부밖에 안 남아 좁은 화면에서 겹친다(실측: 8.7% = 모바일 30px,
    # 라벨 폭 약 38px).
    if n > 1 and picks and picks[-1] != n - 1:
        # 남은 꼬리가 한 칸(step)보다 짧으면 덧붙이지 말고 교체한다. 덧붙이면 마지막
        # 간격만 유독 좁아진다(실측: n=23 일 때 8.7% = 모바일 30px, 라벨 폭 약 38px).
        # 교체하면 마지막 간격이 step~2×step 사이가 되어 어떤 n 에서도 안 겹친다.
        if len(picks) > 1 and (n - 1) - picks[-1] < step:
            picks[-1] = n - 1
        else:
            picks.append(n - 1)
    slot = 100.0 / n_slots
    out = []
    for i in picks:
        pos = i * slot + slot / 2
        cls = "first" if i == picks[0] else ("last" if i == picks[-1] else "")
        style = f"left:{pos:.2f}%"
        out.append(f'<span class="{cls}" style="{style}">{_e(labels[i])}</span>')
    return f'<div class="xlabels">{"".join(out)}</div>'


def _chart(days: Sequence[Mapping[str, Any]], *, height: int = 168) -> str:
    """일자별 비용 막대 + 7일 이동평균 선.

    막대만 있으면 주말·휴일 요철에 눈이 끌려 추세가 안 보인다. 이동평균을 겹쳐
    "요동" 과 "흐름" 을 분리한다.
    """
    if not days:
        return ""
    seq = list(reversed(days))                 # 표는 최신순, 그래프는 과거→현재
    vals = [_f(d.get("usd")) for d in seq]
    peak = max(vals) or 1.0
    n = len(vals)

    # PAD_B 는 축선 아래 여백. 라벨을 SVG 밖으로 뺐으므로 예전(18)만큼 비울 필요가 없다.
    W, H, PAD_B, PAD_T = 100.0, float(height), 4.0, 8.0
    plot_h = H - PAD_B - PAD_T
    slot = W / n
    bar_w = max(0.6, slot * 0.62)

    bars = ""
    for i, v in enumerate(vals):
        h = (v / peak) * plot_h
        x = i * slot + (slot - bar_w) / 2
        y = PAD_T + (plot_h - h)
        day = _e(seq[i].get("day"))
        bars += (f'<rect class="bar" x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
                 f'height="{max(h, 0.4):.2f}" rx="0.35">'
                 f'<title>{day} — {_usd(v)}</title></rect>')

    # 7일 이동평균(앞쪽은 있는 만큼만 평균)
    pts = []
    for i in range(n):
        lo = max(0, i - 6)
        avg = sum(vals[lo:i + 1]) / (i - lo + 1)
        x = i * slot + slot / 2
        y = PAD_T + (plot_h - (avg / peak) * plot_h)
        pts.append(f"{x:.2f},{y:.2f}")
    line = (f'<polyline class="ma" points="{" ".join(pts)}" />' if n > 1 else "")

    # x 라벨은 SVG 밖 HTML 로(위 _xlabels 주석 참조 — 안에 두면 가로로 10배 늘어난다)
    labels = _xlabels([str(d.get("day"))[5:] for d in seq], n)
    # y 눈금 — 격자선만 있고 숫자가 없으면 막대 높이가 얼마인지 읽을 수 없다.
    yt = _yticks([(PAD_T, _usd(peak)),
                  (PAD_T + plot_h / 2, _usd(peak / 2)),
                  (PAD_T + plot_h, "$0")], H)

    # ⚠ f-string 안에 같은 따옴표를 다시 쓰면 Python 3.11 에서 문법 오류다(CI 가 3.11).
    svg = (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none"'
        f' role="img" aria-label="일자별 비용 추이">'
        f'<line class="grid" x1="0" y1="{PAD_T:.1f}" x2="100" y2="{PAD_T:.1f}"/>'
        f'<line class="grid" x1="0" y1="{PAD_T + plot_h / 2:.1f}" x2="100"'
        f' y2="{PAD_T + plot_h / 2:.1f}"/>'
        f'<line class="axis" x1="0" y1="{PAD_T + plot_h:.1f}" x2="100"'
        f' y2="{PAD_T + plot_h:.1f}"/>'
        f'{bars}{line}</svg>')
    plot = _plot(svg, yticks=yt, xlabels=labels)

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>일자별 비용 추이</h2>
    <p>막대는 그날 비용, 선은 <strong>7일 이동평균</strong>입니다.
       요철이 아니라 선의 기울기가 추세입니다. 최고 {_usd(peak)}/일.</p>
  </header>
  {plot}
</section>"""


# ── 예측 ──────────────────────────────────────────────────────────────────────


def forecast(days: Sequence[Mapping[str, Any]], *, window: int = 7) -> dict:
    """최근 실적으로 앞으로의 비용을 추정한다.

    회귀 같은 걸 쓰지 않는다. 수업은 요일·일정에 따라 몰리므로 매끈한 추세선이
    오히려 거짓 정밀도를 준다. **최근 N일의 평균과 최소·최대**로 범위를 준다 —
    "얼마쯤 나오고, 최악이면 얼마" 가 실제로 필요한 답이다.

    days 는 최신순. 사용이 0인 날(수업 없는 날)은 평균을 왜곡하므로 제외한다.
    """
    active = [d for d in days if _f(d.get("usd")) > 0][:window]
    if not active:
        return {"ok": False, "reason": "사용 기록이 없어 예측할 수 없습니다"}

    vals = [_f(d.get("usd")) for d in active]
    avg = sum(vals) / len(vals)
    return {
        "ok": True,
        "window": len(vals),
        "avg_per_active_day": avg,
        "min_per_active_day": min(vals),
        "max_per_active_day": max(vals),
        "per_30_days": avg * 30,
        "per_30_days_max": max(vals) * 30,
    }


def _forecast_panel(days: Sequence[Mapping[str, Any]], budget_usd: float,
                    krw_rate: float) -> str:
    f = forecast(days)
    if not f.get("ok"):
        return ""

    avg, lo, hi = (f["avg_per_active_day"], f["min_per_active_day"],
                   f["max_per_active_day"])
    runway = ""
    if budget_usd > 0 and avg > 0:
        spent = sum(_f(d.get("usd")) for d in days)
        left = budget_usd - spent
        if left <= 0:
            runway = ('<div class="fc-note crit">이미 예산을 넘겼습니다 — '
                      f'초과 {_usd(-left)}</div>')
        else:
            runway = (f'<div class="fc-note">남은 예산 {_usd(left)} 으로 '
                      f'<b>수업일 기준 약 {int(left // avg)}일</b> 더 쓸 수 있습니다 '
                      f'(최악이면 {int(left // hi) if hi else 0}일).</div>')

    def cell(label, value, sub):
        return (f'<div class="fc"><div class="fc-label">{_e(label)}</div>'
                f'<div class="fc-value">{value}</div>'
                f'<div class="fc-sub">{sub}</div></div>')

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>비용 예측</h2>
    <p>최근 <strong>사용이 있었던 {f['window']}일</strong> 실적 기준입니다.
       수업이 없던 날은 평균을 왜곡하므로 제외했습니다.</p>
  </header>
  <div class="fc-grid">
    {cell("수업일 1일 평균", _usd(avg), f"범위 {_usd(lo)} ~ {_usd(hi)}")}
    {cell("수업 30일 예상", _usd(f['per_30_days']),
          f"약 {_krw(round(f['per_30_days'] * krw_rate))}")}
    {cell("최악 30일", _usd(f['per_30_days_max']),
          f"약 {_krw(round(f['per_30_days_max'] * krw_rate))}")}
  </div>
  {runway}
  <p class="fc-caveat">추세선이 아니라 <b>최근 실적의 평균과 범위</b>입니다.
     수업은 일정에 따라 몰리므로 매끈한 회귀선은 거짓 정밀도를 줍니다.
     학급 수·수업 시간이 바뀌면 이 추정은 그대로 빗나갑니다.</p>
</section>"""


# ── AI 분석 ───────────────────────────────────────────────────────────────────


def _md_lite(text: str) -> str:
    """LLM 이 돌려준 마크다운을 최소한만 HTML 로 옮긴다.

    마크다운 라이브러리를 끌어오지 않는다 — 프롬프트로 형식을 `## 제목`, `- 목록`,
    문단 셋으로 좁혀 뒀으므로 그만큼만 다루면 된다. **이스케이프가 먼저**다:
    LLM 출력도 결국 데이터고, 여기 HTML 이 섞이면 그대로 실행된다.
    """
    out, buf, in_ul = [], [], False

    def flush_p():
        if buf:
            out.append("<p>" + "<br>".join(buf) + "</p>")
            buf.clear()

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in (text or "").splitlines():
        line = _e(raw.rstrip())
        stripped = line.strip()
        if not stripped:
            flush_p()
            close_ul()
            continue
        if stripped.startswith("## "):
            flush_p()
            close_ul()
            out.append(f"<h2>{stripped[3:].strip()}</h2>")
        elif stripped.startswith(("- ", "* ")):
            flush_p()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{stripped[2:].strip()}</li>")
        else:
            close_ul()
            buf.append(stripped)
    flush_p()
    close_ul()

    html_out = "".join(out)
    # **강조** 만 뒤늦게 살린다(이스케이프 이후라 안전).
    while html_out.count("**") >= 2:
        html_out = html_out.replace("**", "<b>", 1).replace("**", "</b>", 1)
    return html_out


def _ai_panel(insight: Mapping[str, Any] | None, *, day: str = "",
              can_generate: bool = False, endpoint: str = "report/insight",
              token: str = "") -> str:
    """AI 분석 — 밤에 한 번 굳히고, 필요하면 버튼으로 다시 만든다.

    페이지를 열 때마다 LLM 을 부르지 않는 이유는 하나다: 그러면 새로고침이 곧 과금이고,
    여러 명이 열면 그만큼 늘어난다. 비용을 보는 화면이 스스로 비용을 만들면 곤란하다.
    """
    ins = insight or {}
    body, meta = "", ""
    if ins.get("text"):
        body = f'<div class="ai-body">{_md_lite(ins["text"])}</div>'
        stamp = _kst(ins.get("generated_at_utc", ""))
        meta = (f'<div class="ai-meta">{_e(ins.get("model") or "")}'
                f'{" · " + stamp if stamp else ""}</div>')
    elif ins.get("error"):
        body = f'<p class="ai-status">분석을 만들지 못했습니다 — {_e(ins["error"])}</p>'
    else:
        body = ('<p class="ai-status">아직 분석이 없습니다. 밤 배치가 하루 한 번 만들고, '
                '지금 바로 필요하면 아래 버튼을 누르세요.</p>')

    btn = ""
    if can_generate and day:
        btn = (f'<button type="button" id="ai-go" data-day="{_e(day)}" '
               f'data-endpoint="{_e(endpoint)}" data-token="{_e(token)}" '
               f'class="ghost">분석 생성</button>')

    script = ""
    if btn:
        # 같은 출처 fetch 만 한다(외부 호출 없음). 실패도 화면에 그대로 드러낸다.
        script = """<script>
(function(){
  var b=document.getElementById('ai-go'); if(!b) return;
  b.addEventListener('click', function(){
    var box=document.getElementById('ai-out');
    b.disabled=true; b.textContent='분석 중… (20초쯤 걸립니다)';
    var u=b.dataset.endpoint+'?day='+encodeURIComponent(b.dataset.day)
        +(b.dataset.token?'&token='+encodeURIComponent(b.dataset.token):'');
    fetch(u,{method:'POST'}).then(function(r){return r.json()}).then(function(j){
      if(j&&j.ok&&j.html){ box.innerHTML=j.html; }
      else { box.innerHTML='<p class="ai-status">실패 — '+
             ((j&&j.error)||'알 수 없는 오류')+'</p>'; }
    }).catch(function(e){
      box.innerHTML='<p class="ai-status">요청 실패 — '+e+'</p>';
    }).finally(function(){ b.disabled=false; b.textContent='분석 다시 생성'; });
  });
})();
</script>"""

    return f"""<section class="panel">
  <div class="ai-head">
    <div class="panel-head" style="margin:0">
      <h2>AI 분석 · 다음 비용 예상</h2>
      <p>산식 예측이 "얼마"라면 이쪽은 "왜 그런가"입니다.
         여러 표를 겹쳐 읽어야 보이는 것들입니다.</p>
    </div>
    {btn}
  </div>
  <div id="ai-out">{body}{meta}</div>
  {script}
</section>"""


# ── 실과금 구분 · 절감 효과 · 사용자 패턴 ─────────────────────────────────────


def _billing_panel(billing: Mapping[str, Any]) -> str:
    """환산 금액을 '진짜 청구되는 것'과 '구독이라 0원인 것'으로 가른다.

    가중토큰 환산은 "API 로 돌렸으면 얼마"다. 실제로는 두 갈래가 섞이므로
    (구독 CLI, 인증 실패 폴백) 이걸 안 가르면 청구서와 리포트가 어긋난다.
    """
    if not billing or billing.get("unavailable"):
        return ""
    rows = billing.get("by_mode") or []
    if not rows:
        return ""
    label = {"api": "API (실청구)", "cli": "구독 CLI (실청구 0)",
             "api_fallback_cli": "폴백→구독 (실청구 0)", "(미상)": "미상 (구버전 기록)"}
    total = sum(_n(r.get("turns")) for r in rows) or 1
    body = "".join(
        f"""<tr>
      <td>{_e(label.get(r.get('llm_mode'), r.get('llm_mode')))}</td>
      <td>{_comma(r.get('turns'))}</td>
      <td>{_n(r.get('turns')) / total * 100:.1f}%</td>
      <td>{_comma(r.get('weighted_tokens'))}</td>
      <td><b>{_usd(r.get('usd'))}</b></td>
    </tr>""" for r in rows)

    billed = _f(billing.get("billed_usd"))
    free = _f(billing.get("subscription_usd"))
    unknown = _f(billing.get("unknown_usd"))
    warn = ""
    if unknown > 0:
        warn = (f'<p class="fc-caveat">미상 {_usd(unknown)} 은 경로 기록을 도입하기 전 '
                f'데이터입니다. 이후 턴부터는 정확히 갈립니다.</p>')

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>실제 과금 구분</h2>
    <p>환산 금액을 <strong>실제로 청구되는 것</strong>과 구독이라 0원인 것으로 나눕니다.
       설정값이 아니라 각 턴이 실제로 탄 경로 기준입니다.</p>
  </header>
  <div class="fc-grid" style="margin-bottom:14px">
    <div class="fc"><div class="fc-label">실청구 (API)</div>
      <div class="fc-value">{_usd(billed)}</div>
      <div class="fc-sub">이 금액만 카드에 찍힙니다</div></div>
    <div class="fc" style="border-left-color:var(--line-2)">
      <div class="fc-label">구독으로 처리</div>
      <div class="fc-value">{_usd(free)}</div>
      <div class="fc-sub">정액이라 실청구 0 — 환산치</div></div>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>경로</th><th>턴</th><th>비중</th><th>가중토큰</th><th>환산액</th></tr></thead>
    <tbody>{body}</tbody></table></div>{warn}
</section>"""


def _reuse_panel(reuse: Mapping[str, Any]) -> str:
    """재사용(RAG·직접서브)으로 API 호출을 얼마나 피했나.

    절감은 '안 쓴 돈'이라 직접 관측되지 않는다. cold 턴(재사용 없이 새로 생성)의 평균
    단가를 반사실로 놓고, 각 티어가 그보다 덜 쓴 만큼을 절감으로 잡는다. 추정임을
    숨기지 않으려고 기준선을 함께 보여 준다.
    """
    if not reuse:
        return ""
    tiers = reuse.get("by_tier") or []
    if not tiers:
        return ""
    label = {"direct_serve": "직접서브 (생성 LLM 0)", "near": "재사용 프라임 + 생성",
             "cold": "신규 생성 (기준선)", "(미상)": "미상"}
    total_turns = sum(_n(t.get("turns")) for t in tiers) or 1
    body = "".join(
        f"""<tr>
      <td>{_e(label.get(t.get('reuse_tier'), t.get('reuse_tier')))}</td>
      <td>{_comma(t.get('turns'))}</td>
      <td>{_n(t.get('turns')) / total_turns * 100:.1f}%</td>
      <td>{_usd(t.get('usd_per_turn'))}</td>
      <td>{_usd(t.get('usd'))}</td>
      <td><b>{_usd(t.get('saved_usd')) if t.get('saved_usd') else '—'}</b></td>
    </tr>""" for t in tiers)

    if not reuse.get("ok"):
        head = f"""<div class="fc-note">{_e(reuse.get('reason', ''))}</div>"""
    else:
        saved, pct = _f(reuse.get("saved_usd")), _f(reuse.get("saved_pct"))
        head = f"""<div class="fc-grid" style="margin-bottom:14px">
    <div class="fc"><div class="fc-label">절감액</div>
      <div class="fc-value">{_usd(saved)}</div>
      <div class="fc-sub">전량 신규 생성 대비 {pct:.1f}%</div></div>
    <div class="fc" style="border-left-color:var(--line-2)">
      <div class="fc-label">실제 지출</div>
      <div class="fc-value">{_usd(reuse.get('actual_usd'))}</div>
      <div class="fc-sub">재사용 없었다면 {_usd(reuse.get('counterfactual_usd'))}</div></div>
    <div class="fc" style="border-left-color:var(--line-2)">
      <div class="fc-label">기준선 (cold 턴)</div>
      <div class="fc-value">{_usd(reuse.get('baseline_usd_per_turn'))}</div>
      <div class="fc-sub">턴당 — 이 값이 반사실</div></div>
  </div>"""

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>재사용 절감 효과</h2>
    <p>RAG 재사용과 직접서브로 <strong>API 를 부르지 않은 만큼</strong>이 절감입니다.
       직접서브는 생성 LLM 호출 자체가 0입니다.</p>
  </header>
  {head}
  <div class="scroll"><table>
    <thead><tr><th>티어</th><th>턴</th><th>비중</th><th>턴당</th><th>지출</th><th>절감</th></tr></thead>
    <tbody>{body}</tbody></table></div>
  <p class="fc-caveat">절감액은 <b>추정</b>입니다 — cold 턴 평균 단가를 "재사용이 없었다면"
     의 기준으로 삼았습니다. 재사용되는 요청이 원래 더 쉬운 요청일 수 있으므로 상한에
     가깝게 읽는 편이 안전합니다.</p>
</section>"""


def _patterns_panel(patterns: Mapping[str, Any], *,
                    base_qs: Mapping[str, str] | None = None) -> str:
    """사용자가 실제로 어떻게 쓰는가 — 쿼터·수업 설계를 고치는 근거."""
    if not patterns or not patterns.get("ok"):
        return ""
    users = patterns.get("users") or []
    depth = patterns.get("session_depth") or []
    segs = patterns.get("user_segments") or []

    def dist(rows, key, unit):
        top = max((_n(r.get(key)) for r in rows), default=0) or 1
        return "".join(f"""<div class="mix-row">
          <span class="mix-name">{_e(r.get('bucket'))}{_e(unit)}</span>
          <span class="mix-track"><span class="mix-fill"
            style="width:{_bar(_n(r.get(key)) / top)}%"></span></span>
          <span class="mix-pct">{_comma(r.get(key))}</span>
          <span class="mix-raw"></span>
        </div>""" for r in rows)

    urows = "".join(f"""<tr>
      <td><span title="{_e(u.get('user_id'))}">{_e(_short_user(u.get('user_id')))}</span></td>
      <td>{_comma(u.get('turns'))}</td>
      <td>{_comma(u.get('sessions'))}</td>
      <td>{_f(u.get('turns_per_session')):.1f}</td>
      <td>{_n(u.get('design_ratio'))}%</td>
      <td>{_n(u.get('blockly_ratio'))}%</td>
      <td>{_n(u.get('cold_ratio'))}%</td>
      <td>{_usd(u.get('usd'))}</td>
    </tr>""" for u in users)
    if not urows:
        urows = '<tr><td class="empty" colspan="8">이 기간에 기록된 사용자가 없습니다</td></tr>'

    upager = _pager(total=_n(patterns.get("total_users")),
                    offset=_n(patterns.get("offset")),
                    page_size=_n(patterns.get("page_size")) or 25,
                    param="uoff", base_qs=dict(base_qs or {}), label="명")

    return f"""<section class="panel">
  <header class="panel-head">
    <h2>사용자 패턴</h2>
    <p>“얼마”가 아니라 <strong>“어떻게 쓰는가”</strong>입니다. 쿼터 상한과 수업 설계를
       고칠 때 보는 칸입니다.</p>
  </header>
  <div class="fc-grid" style="gap:24px; margin-bottom:16px">
    <div>
      <div class="stat-label" style="margin-bottom:6px">세션당 턴 수</div>
      <div class="mix">{dist(depth, 'sessions', '턴')}</div>
      <p class="fc-caveat">작품 하나를 만드는 데 몇 턴이 드는가. 1턴 세션이 많으면
         첫 응답에서 이탈하고 있다는 뜻입니다.</p>
    </div>
    <div>
      <div class="stat-label" style="margin-bottom:6px">사용자별 턴 수 분포</div>
      <div class="mix">{dist(segs, 'users', '턴')}</div>
      <p class="fc-caveat">쿼터 상한(현재 70턴)을 실제로 누가 치는지. 상한 근처가
         비어 있으면 상한이 놀고 있는 것입니다.</p>
    </div>
  </div>
  <div class="scroll"><table>
    <thead><tr>
      <th>사용자</th><th>턴</th><th>세션</th><th>세션당</th>
      <th>설계형</th><th>블록</th><th>신규생성</th><th>환산액</th>
    </tr></thead><tbody>{urows}</tbody></table></div>{upager}
  <p class="fc-caveat"><b>설계형</b>=되묻기(design) 비중, <b>블록</b>=blockly 비중,
     <b>신규생성</b>=재사용이 안 걸려 새로 만든 턴 비중. 신규생성이 높은 사용자가
     비용을 끌어올립니다.</p>
</section>"""


def _with_token(page_qs: Mapping[str, str] | None, token: str) -> dict:
    """화면 안 링크가 물고 갈 쿼리 — **토큰을 렌더러가 직접 채운다.**

    호출부에서 page_qs 를 넘기는 걸 잊으면 페이저·검색 링크에서 토큰이 사라지고,
    리포트는 fail-closed 라 그 링크가 전부 404 가 된다. 실제로 두 번 그랬다:
      2026-08-21 일자 링크(토큰 누락) · 확정본 화면의 페이저(page_qs 미전달).

    호출부마다 기억해야 하는 규칙은 언젠가 잊힌다. 여기서 못 박으면 잊을 수가 없다.
    """
    qs = dict(page_qs or {})
    if token and not qs.get("token"):
        qs["token"] = token
    return qs


def _pager(*, total: int, offset: int, page_size: int, param: str,
           base_qs: Mapping[str, str], label: str = "항목") -> str:
    """서버 페이지네이션 컨트롤.

    표를 다 뿌리지 않는 이유는 성능만이 아니다 — 수백 줄을 한 화면에 쏟으면 **읽히지
    않는다.** 지금 몇 번째를 보고 있는지도 함께 적는다.

    링크는 상대경로다(앱은 /agent 프리픽스를 모른다). 기존 쿼리는 그대로 물고 간다 —
    안 그러면 페이지를 넘길 때마다 기간·토큰이 날아간다.
    """
    if total <= page_size:
        return ""
    page_size = max(1, page_size)
    cur = offset // page_size + 1
    last = max(1, -(-total // page_size))

    def link(target_offset: int, text: str, disabled: bool) -> str:
        if disabled:
            return f'<span class="pg-off">{_e(text)}</span>'
        qs = dict(base_qs)
        qs[param] = str(max(0, target_offset))
        pairs = "&amp;".join(f"{_e(k)}={_e(v)}" for k, v in qs.items() if v not in ("", None))
        return f'<a class="pg-a" href="?{pairs}">{_e(text)}</a>'

    shown_to = min(offset + page_size, total)
    return f"""<div class="pager">
    <span class="pg-info">{_comma(offset + 1)}–{_comma(shown_to)} / {_comma(total)} {_e(label)}</span>
    <span class="pg-nav">
      {link(0, "처음", offset <= 0)}
      {link(offset - page_size, "이전", offset <= 0)}
      <span class="pg-cur">{cur} / {last}</span>
      {link(offset + page_size, "다음", shown_to >= total)}
      {link((last - 1) * page_size, "마지막", shown_to >= total)}
    </span>
  </div>"""


def _search_form(*, param: str, value: str, base_qs: Mapping[str, str],
                 placeholder: str) -> str:
    """표 안 검색. GET 이라 결과 URL 을 그대로 공유할 수 있다."""
    hid = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                  for k, v in base_qs.items() if k != param and v not in ("", None))
    clear = ""
    if value:
        pairs = "&amp;".join(f"{_e(k)}={_e(v)}" for k, v in base_qs.items()
                             if k != param and v not in ("", None))
        clear = f'<a class="pg-a" href="?{pairs}">지우기</a>'
    return f"""<form class="tsearch" method="get">{hid}
    <input type="search" name="{_e(param)}" value="{_e(value)}"
           placeholder="{_e(placeholder)}" aria-label="{_e(placeholder)}">
    <button type="submit" class="ghost">검색</button>{clear}
  </form>"""
