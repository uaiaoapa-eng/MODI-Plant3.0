"""부하 분석 — 원장(usage_turns/ops_events)에서 "얼마나 버텼나"를 뽑는다.

리포트의 나머지 부분이 답하는 질문은 "얼마 썼나"다. 이 모듈이 답하는 질문은 다르다:

    학생이 얼마나 기다렸나 · 동시에 몇 명이 붙어 있었나 · 몇 명이 튕겼나 ·
    붐빌수록 느려졌나 · 어떤 종류의 질문이 비싸고 느렸나

계산을 SQL 이 아니라 파이썬에서 하는 이유가 있다. 동시 접속은 "구간 겹침"이라
GROUP BY 로는 안 나오고(윈도우 함수로 짜면 읽기가 어렵고 MySQL 버전을 탄다),
분위수도 마찬가지다. 수업 하루치가 많아야 수천 행이라 메모리에 올려 한 번에
훑는 편이 정확하고 검증하기 쉽다. 아래 함수들은 전부 **순수 함수**라 DB 없이
단위 테스트가 된다.

시간대 규약: 들어오는 datetime 은 전부 **UTC**(store_mysql 참조). 라벨을 만들 때만
KST 로 바꾼다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

KST_OFFSET = timedelta(hours=9)

# 원장을 통째로 메모리에 올리므로 상한을 둔다. 40명 × 수십 턴이면 1천 행 남짓이라
# 여유가 크지만, 기간을 몇 달로 잡고 열었을 때 서버가 흔들리면 안 된다.
# ⚠ 잘렸으면 반드시 화면에 알린다 — 조용히 잘린 표본으로 "괜찮았다"고 읽는 것이
#   부하 분석에서 제일 위험하다.
MAX_ROWS = 50_000

# 동접 곡선의 시간 해상도. 2시간 수업을 시간 단위로 보면 두 칸이라 아무것도 안 보인다.
BUCKET_SECONDS = 60


# ─────────────────────────────────────────────────────────────────────────────
# 분위수
# ─────────────────────────────────────────────────────────────────────────────

def percentile(sorted_values: list[float], q: float) -> float:
    """이미 정렬된 값 목록에서 q 분위수(0~1). 선형 보간.

    평균을 쓰지 않는 이유: 부하 상황에서 평균은 거의 항상 거짓말을 한다. 40명 중
    35명이 3초, 5명이 120초면 평균은 17초라 "괜찮네"로 읽히지만 실제로는 8명 중
    1명이 2분을 기다린 것이다. 그 5명이 수업을 포기한다.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    q = min(max(q, 0.0), 1.0)
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


def latency_stats(values: list[int]) -> dict:
    """지연 분포 요약. 0 이하(미측정)는 제외한다 — 섞으면 분위수가 낙관적으로 망가진다."""
    vals = sorted(v for v in values if v and v > 0)
    if not vals:
        return {"n": 0, "p50": 0, "p90": 0, "p95": 0, "max": 0, "avg": 0}
    return {
        "n": len(vals),
        "p50": round(percentile(vals, 0.50)),
        "p90": round(percentile(vals, 0.90)),
        "p95": round(percentile(vals, 0.95)),
        "max": int(vals[-1]),
        # 평균도 같이 준다 — 분위수와 나란히 놓으면 "평균만 보면 안 되는 이유"가
        # 화면에서 바로 보인다(둘이 크게 벌어질수록 꼬리가 길다는 뜻).
        "avg": round(sum(vals) / len(vals)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 동시 접속 — 구간 겹침
# ─────────────────────────────────────────────────────────────────────────────

def _as_dt(v) -> datetime | None:
    """datetime 또는 'YYYY-MM-DD HH:MM:SS' 문자열 → datetime. 아니면 None.

    ⚠ 드라이버·경로에 따라 DATETIME 이 문자열로 올라오는 경우가 있다. isinstance 만
      보면 그 행이 조용히 빠져 **동접이 0으로 나온다**(2026-08-21 실측: duration 은
      있는데 measured=0). 곡선이 비어 있어도 에러가 안 나므로 알아채기 어렵다.
    """
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not v.strip():
        return None
    txt = v.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(txt[:26], fmt)
        except ValueError:
            continue
    return None


def _interval(row: dict) -> tuple[datetime, datetime] | None:
    """턴 1건의 [시작, 종료] 구간(UTC). 판정 불가면 None.

    started_at 이 없는 구버전 행은 ts(종료)에서 duration 을 빼 복원한다. 둘 다 없으면
    그 행은 동접 계산에서 빠진다 — 추측으로 채우면 곡선이 조용히 부풀기 때문이다.
    """
    end = _as_dt(row.get("ts"))
    if end is None:
        return None
    start = _as_dt(row.get("started_at"))
    if start is None:
        dur = row.get("duration_ms") or 0
        if not dur:
            return None
        start = end - timedelta(milliseconds=int(dur))
    if start > end:
        # 시작이 종료보다 뒤 — 두 컬럼의 시간대 규약이 어긋난 경우다(한쪽만 변환됨).
        # 이때 행을 버리면 동접이 통째로 0 이 된다. 같은 컬럼(ts)에서 파생되는
        # duration 폴백은 시간대와 무관하게 항상 옳으므로 그쪽으로 복구한다.
        dur = row.get("duration_ms") or 0
        if not dur:
            return None
        start = end - timedelta(milliseconds=int(dur))
    return start, end


def concurrency_timeline(rows: list[dict], bucket_seconds: int = BUCKET_SECONDS) -> dict:
    """시간 버킷별 동시 실행 턴 수 + 피크.

    ★ 이게 별도 수집 없이 나오는 게 이 설계의 핵심이다. ts(종료)와 duration 이
      있으면 시작 시각이 정해지고, 각 버킷에 걸쳐 있는 턴을 세면 그 순간 서버가
      동시에 처리하던 수가 **정확히** 나온다. 주기적 샘플링과 달리 표본 사이로
      피크가 새지 않는다.

    반환: {"buckets": [{"t": KST 문자열, "concurrent": n, "started": n}], "peak": n,
           "peak_at": KST 문자열}
    """
    intervals = [iv for iv in (_interval(r) for r in rows) if iv]
    # 빠진 행 수를 반드시 함께 돌려준다. 곡선이 비어도 예외가 안 나기 때문에,
    # 세어 두지 않으면 "한산했다"와 "계산이 안 됐다"가 화면에서 구별되지 않는다.
    skipped = len(rows) - len(intervals)
    if not intervals:
        return {"ok": True, "buckets": [], "peak": 0, "peak_at": "",
                "measured": 0, "skipped": skipped}

    step = timedelta(seconds=bucket_seconds)
    lo = min(iv[0] for iv in intervals)
    hi = max(iv[1] for iv in intervals)
    # 버킷 경계를 step 배수로 내림 정렬 — 조회 구간이 달라도 같은 격자를 쓰게 한다.
    lo = lo - timedelta(seconds=lo.second % bucket_seconds, microseconds=lo.microsecond)

    buckets: list[dict] = []
    peak, peak_at = 0, ""
    t = lo
    # 상한: 아주 긴 기간을 분 단위로 그리면 버킷이 수십만 개가 된다. 그때는 해상도를
    # 자동으로 낮춘다(정확도보다 화면이 뜨는 게 우선).
    span = (hi - lo).total_seconds()
    if span / bucket_seconds > 5000:
        step = timedelta(seconds=max(bucket_seconds, int(span / 5000)))

    while t <= hi:
        t_end = t + step
        concurrent = sum(1 for a, b in intervals if a < t_end and b >= t)
        started = sum(1 for a, _ in intervals if t <= a < t_end)
        label = (t + KST_OFFSET).strftime("%Y-%m-%d %H:%M")
        buckets.append({"t": label, "concurrent": concurrent, "started": started})
        if concurrent > peak:
            peak, peak_at = concurrent, label
        t = t_end

    return {"ok": True, "buckets": buckets, "peak": peak, "peak_at": peak_at,
            "measured": len(intervals), "skipped": skipped}


def latency_by_concurrency(rows: list[dict]) -> list[dict]:
    """동시 실행 N 일 때의 응답시간 분포.

    ★ "40명이 버티나"의 진짜 답이 여기 있다. 전체 p95 하나로는 알 수 없다 —
      한산할 때 빠르고 붐빌 때만 느려지는지, 아니면 원래 느린지가 안 갈린다.
      동접이 오를수록 p95 가 꺾이는 지점이 곧 수용 한계다.

    각 턴에 대해 "그 턴이 도는 동안의 최대 동시 실행 수"를 붙여 구간별로 묶는다.
    """
    pairs = []
    intervals = []
    for r in rows:
        iv = _interval(r)
        if iv:
            intervals.append(iv)
            pairs.append((r, iv))
    if not pairs:
        return []

    out: dict[str, list[int]] = {}
    for r, (a, b) in pairs:
        # 이 턴과 겹친 턴 수 = 자기 자신 포함 동시 실행 수의 상한.
        overlap = sum(1 for x, y in intervals if x < b and y > a)
        bucket = _conc_bucket(overlap)
        dur = r.get("duration_ms") or 0
        if dur > 0:
            out.setdefault(bucket, []).append(int(dur))

    order = ["1", "2-3", "4-7", "8-15", "16-30", "31+"]
    rows_out = []
    for b in order:
        vals = out.get(b)
        if not vals:
            continue
        st = latency_stats(vals)
        rows_out.append({"bucket": b, "turns": st["n"], "p50": st["p50"],
                         "p95": st["p95"], "max": st["max"]})
    return rows_out


def _conc_bucket(n: int) -> str:
    if n <= 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 7:
        return "4-7"
    if n <= 15:
        return "8-15"
    if n <= 30:
        return "16-30"
    return "31+"


# ─────────────────────────────────────────────────────────────────────────────
# 유형별 집계
# ─────────────────────────────────────────────────────────────────────────────

_INTENT_LABEL = {
    "question": "질문",
    "chat": "잡담·인사",
    "modify_request": "수정 요청",
    "implement_request": "구현 요청",
    "clarify_request": "되묻기",
    "phase_change": "단계 전환",
    "continue_pending_action": "이어하기",
}
_OUTCOME_LABEL = {
    "code": "코드(소프트웨어)",
    "blockly": "블록(하드웨어)",
    "doc": "설계 문서",
    "chat": "대화만",
    "none": "산출물 없음",
}


def group_stats(rows: list[dict], key: str, labels: dict[str, str],
                usd_per_weighted_mtok: float = 1.0) -> list[dict]:
    """어떤 유형의 질문이 비싸고 느린가 — 턴 수·비용·지연을 한 줄에 묶는다.

    같은 "질문"이어도 코드가 나온 턴과 대화만 한 턴은 비용이 자릿수로 갈린다.
    유형별 단가를 알아야 "무엇을 재사용으로 돌려야 이득인지"가 정해진다.
    """
    buckets: dict[str, dict] = {}
    for r in rows:
        k = (r.get(key) or "").strip() or "(미상)"
        b = buckets.setdefault(k, {"turns": 0, "weighted": 0, "durs": [], "fail": 0})
        b["turns"] += 1
        b["weighted"] += int(r.get("weighted_tokens") or 0)
        d = r.get("duration_ms") or 0
        if d > 0:
            b["durs"].append(int(d))
        if (r.get("status") or "ok") != "ok":
            b["fail"] += 1

    out = []
    for k, b in buckets.items():
        st = latency_stats(b["durs"])
        usd = round(b["weighted"] / 1_000_000 * usd_per_weighted_mtok, 4)
        out.append({
            "key": k,
            "label": labels.get(k, k),
            "turns": b["turns"],
            "weighted_tokens": b["weighted"],
            "usd": usd,
            "usd_per_turn": round(usd / b["turns"], 5) if b["turns"] else 0.0,
            "p50": st["p50"], "p95": st["p95"],
            "fail": b["fail"],
            "fail_pct": round(b["fail"] / b["turns"] * 100, 1) if b["turns"] else 0.0,
        })
    out.sort(key=lambda r: r["weighted_tokens"], reverse=True)
    return out


def reuse_threshold_curve(rows: list[dict]) -> list[dict]:
    """재사용 임계값을 내리면 몇 턴이 더 재사용될까 — 절감 여력의 직접 추정.

    ★ 비용을 **줄이는** 결정에 쓰는 유일한 표다. reuse_tier 는 "그래서 얼마나
      아꼈나"(과거)를 알려 주고, 이 곡선은 "임계를 어디까지 내리면 얼마나 더
      아낄 수 있나"(미래)를 알려 준다. top1 점수를 원장에 남긴 날에만 계산된다.

    cold 로 처리된 턴들의 top1 분포를 임계값 후보별로 누적한다.
    """
    cold = [r for r in rows
            if (r.get("reuse_tier") or "") == "cold" and (r.get("reuse_top1") or 0) > 0]
    if not cold:
        return []
    total_cold = len([r for r in rows if (r.get("reuse_tier") or "") == "cold"])
    out = []
    for th in (0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40):
        hits = [r for r in cold if float(r.get("reuse_top1") or 0) >= th]
        if not hits:
            continue
        weighted = sum(int(r.get("weighted_tokens") or 0) for r in hits)
        out.append({
            "threshold": th,
            "turns": len(hits),
            "pct_of_cold": round(len(hits) / total_cold * 100, 1) if total_cold else 0.0,
            "weighted_tokens": weighted,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 접속 환경 — 어떤 기기·브라우저로 들어왔나
# ─────────────────────────────────────────────────────────────────────────────

def parse_client(ua: str) -> dict:
    """User-Agent → {browser, os, device, is_bot}. 외부 라이브러리 없이 필요한 만큼만.

    완전한 UA 파싱은 끝이 없다. 여기서 답해야 할 질문은 셋뿐이다:
      ① 사람인가 스크립트인가 (부하 테스트 계정이 학생 수에 섞이면 안 된다)
      ② 태블릿인가 PC 인가 (수업 환경이 갈린다)
      ③ 어떤 브라우저인가 (특정 브라우저에서만 깨지는지)

    순서가 중요하다 — Edge/Chrome/Safari 는 서로의 문자열을 포함하므로
    **구체적인 것부터** 본다. 그러지 않으면 Edge 가 전부 Chrome 으로 잡힌다.
    """
    u = (ua or "").strip()
    if not u:
        return {"browser": "(미상)", "os": "(미상)", "device": "(미상)", "is_bot": False}

    low = u.lower()
    if any(k in low for k in ("curl/", "python-requests", "httpx/", "wget/",
                              "bot", "spider", "headlesschrome")):
        return {"browser": "스크립트", "os": "-", "device": "스크립트", "is_bot": True}

    # 브라우저 — 구체적인 것부터
    browser = "(기타)"
    for key, label in (("edg/", "Edge"), ("opr/", "Opera"), ("whale", "Whale"),
                       ("samsungbrowser", "삼성 인터넷"), ("firefox/", "Firefox"),
                       ("crios/", "Chrome(iOS)"), ("fxios/", "Firefox(iOS)"),
                       ("chrome/", "Chrome"), ("safari/", "Safari")):
        if key in low:
            browser = label
            break

    # OS · 기기 — iPad 는 iPadOS 13+ 부터 데스크톱 Safari 로 위장하므로
    # Macintosh + 터치 힌트를 함께 본다(완벽하진 않으나 수업 환경 구분엔 충분).
    if "android" in low:
        os_name = "Android"
        device = "휴대폰" if "mobile" in low else "태블릿"
    elif "ipad" in low:
        os_name, device = "iPadOS", "태블릿"
    elif "iphone" in low:
        os_name, device = "iOS", "휴대폰"
    elif "windows" in low:
        os_name, device = "Windows", "PC"
    elif "cros" in low:
        os_name, device = "ChromeOS", "크롬북"
    elif "mac os x" in low or "macintosh" in low:
        os_name, device = "macOS", "PC"
    elif "linux" in low:
        os_name, device = "Linux", "PC"
    else:
        os_name, device = "(기타)", "(기타)"

    return {"browser": browser, "os": os_name, "device": device, "is_bot": False}


def client_breakdown(rows: list[dict]) -> dict:
    """접속 환경 집계 — 기기/브라우저별 턴·사용자·지연·실패.

    ★ humans / scripts 를 나눠 돌려주는 게 핵심이다. 부하 스크립트가 섞이면
      '사용자 46명'처럼 실제와 동떨어진 숫자가 나온다(2026-08-21: 46명 중 45명이
      테스트 계정이었다). 사람 수를 따로 세어야 수업 규모를 믿을 수 있다.
    """
    buckets: dict[tuple, dict] = {}
    humans, scripts = set(), set()
    nets: dict[str, set] = {}

    for r in rows:
        c = parse_client(r.get("user_agent") or "")
        uid = r.get("user_id") or ""
        (scripts if c["is_bot"] else humans).add(uid)
        key = (c["device"], c["os"], c["browser"])
        b = buckets.setdefault(key, {"turns": 0, "users": set(), "durs": [], "fail": 0})
        b["turns"] += 1
        b["users"].add(uid)
        d = r.get("duration_ms") or 0
        if d > 0:
            b["durs"].append(int(d))
        if (r.get("status") or "ok") != "ok":
            b["fail"] += 1
        ip = (r.get("client_ip") or "").strip()
        if ip and not c["is_bot"]:
            nets.setdefault(ip, set()).add(uid)

    out = []
    for (device, os_name, browser), b in buckets.items():
        st = latency_stats(b["durs"])
        out.append({"device": device, "os": os_name, "browser": browser,
                    "turns": b["turns"], "users": len(b["users"]),
                    "p50": st["p50"], "p95": st["p95"],
                    "fail": b["fail"]})
    out.sort(key=lambda r: r["turns"], reverse=True)

    networks = sorted(({"ip": ip, "users": len(us)} for ip, us in nets.items()),
                      key=lambda r: r["users"], reverse=True)
    return {"by_client": out,
            "human_users": len(humans - {""}),
            "script_users": len(scripts - {""}),
            "networks": networks[:10]}


# ─────────────────────────────────────────────────────────────────────────────
# 산점도용 점 — 요약 통계가 감추는 것을 드러낸다
# ─────────────────────────────────────────────────────────────────────────────

MAX_POINTS = 1500


def turn_points(rows: list[dict]) -> dict:
    """턴 하나하나를 (시각, 소요) 점으로.

    왜 p50/p95 로 부족한가: 분위수는 **분포의 모양**을 감춘다. 같은 p95 라도
      · 전 구간 고르게 느림
      · 평소 빠른데 특정 10분만 폭발
      · 특정 학생만 계속 느림
    은 완전히 다른 상황이고 대응도 다르다. 점으로 찍으면 군집과 이상치가 즉시 보인다.

    반환 좌표는 **분 단위 상대 시각**이라 화면이 시간축을 그대로 쓸 수 있다.
    """
    pts = []
    lo = None
    for r in rows:
        iv = _interval(r)
        if not iv:
            continue
        start, _ = iv
        lo = start if lo is None else min(lo, start)

    if lo is None:
        return {"points": [], "span_min": 0, "t0": "", "truncated": False}

    for r in rows:
        iv = _interval(r)
        if not iv:
            continue
        start, _ = iv
        dur = int(r.get("duration_ms") or 0)
        if dur <= 0:
            continue
        pts.append({
            "m": round((start - lo).total_seconds() / 60.0, 2),   # 시작(분)
            "d": dur,                                             # 소요(ms)
            "w": int(r.get("weighted_tokens") or 0),              # 크기 = 비용 기여
            "s": (r.get("status") or "ok"),
            "o": (r.get("outcome") or ""),
            "u": (r.get("user_id") or ""),
            "sid": (r.get("session_id") or ""),
        })
    pts.sort(key=lambda p: p["m"])
    truncated = len(pts) > MAX_POINTS
    if truncated:
        # 앞뒤를 고르게 남긴다 — 앞만 자르면 수업 후반이 통째로 사라진다.
        step = len(pts) / MAX_POINTS
        pts = [pts[int(i * step)] for i in range(MAX_POINTS)]
    span = pts[-1]["m"] if pts else 0
    return {"points": pts, "span_min": span,
            "t0": (lo + KST_OFFSET).strftime("%Y-%m-%d %H:%M"),
            "truncated": truncated}


# ─────────────────────────────────────────────────────────────────────────────
# 서버 리소스 — 상한에 닿기 전에 보이게
# ─────────────────────────────────────────────────────────────────────────────

MEM_LIMIT_MB = 1024      # docker-compose 의 mem_limit 과 맞춘다


def resource_usage(rows: list[dict], *, limit_mb: int = MEM_LIMIT_MB) -> dict:
    """레플리카별 메모리 사용 — 평균·최대·상한 대비.

    ★ 상한(1g)에 닿으면 그 컨테이너만 재시작되고 나머지가 서빙한다. 설계상 안전하지만,
      **재시작된 뒤에 아는 건 늦다.** 대회 중에 손을 쓰려면 다가가는 게 보여야 한다.

    턴 기록에 실린 값이라 별도 수집기가 필요 없고, 어느 레플리카가 언제 무거웠는지가
    응답시간·실패와 같은 시간축에 놓인다.
    """
    per: dict[str, list[int]] = {}
    for r in rows:
        m = int(r.get("mem_mb") or 0)
        if m <= 0:
            continue
        per.setdefault((r.get("replica") or "").strip() or "(미상)", []).append(m)
    if not per:
        return {"ok": True, "replicas": [], "limit_mb": limit_mb, "measured": 0,
                "peak_pct": 0}

    out, peak_pct = [], 0
    for name, vals in sorted(per.items()):
        vals.sort()
        mx = vals[-1]
        pct = round(mx / limit_mb * 100) if limit_mb else 0
        peak_pct = max(peak_pct, pct)
        out.append({"replica": name, "turns": len(vals),
                    "avg_mb": round(sum(vals) / len(vals)),
                    "p95_mb": round(percentile(vals, 0.95)),
                    "max_mb": mx, "pct_of_limit": pct})
    out.sort(key=lambda r: r["max_mb"], reverse=True)
    return {"ok": True, "replicas": out, "limit_mb": limit_mb,
            "measured": sum(len(v) for v in per.values()), "peak_pct": peak_pct}
