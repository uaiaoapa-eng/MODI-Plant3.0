"""부하 분석 계산 회귀 테스트 — DB 없이 순수 함수만 검증.

2026-08-22 40명 동시 수업의 사후 분석이 이 계산 위에 올라간다. 여기가 틀리면
"버텼다/못 버텼다"의 판단 자체가 틀리므로, 손으로 답을 알 수 있는 입력만 쓴다.
"""
import sys
import pathlib
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import load_analysis as LA  # noqa: E402


def _t(minute: int, second: int = 0) -> datetime:
    """2026-08-22 00:00 UTC(= KST 09:00, 수업 시작) 기준 상대 시각."""
    return datetime(2026, 8, 22, 0, 0, 0) + timedelta(minutes=minute, seconds=second)


def _turn(start_min, dur_ms, **kw) -> dict:
    start = _t(start_min)
    return {"started_at": start,
            "ts": start + timedelta(milliseconds=dur_ms),
            "duration_ms": dur_ms, "status": "ok", **kw}


# ──────────────────────────────────────────────────────────────────────────────
# 분위수 — 평균이 감추는 꼬리를 드러내는가
# ──────────────────────────────────────────────────────────────────────────────

def test_percentile_endpoints():
    vals = [1, 2, 3, 4, 5]
    assert LA.percentile(vals, 0.0) == 1
    assert LA.percentile(vals, 1.0) == 5
    assert LA.percentile(vals, 0.5) == 3


def test_percentile_interpolates():
    assert LA.percentile([0, 10], 0.5) == 5.0


def test_percentile_empty_is_zero_not_crash():
    """빈 기간(턴 0건)에 리포트가 죽으면 안 된다."""
    assert LA.percentile([], 0.95) == 0.0


def test_latency_stats_excludes_unmeasured():
    """0(미측정)이 섞이면 분위수가 낙관적으로 망가진다 — 반드시 빠져야 한다."""
    st = LA.latency_stats([0, 0, 1000, 2000, 3000])
    assert st["n"] == 3
    assert st["p50"] == 2000


def test_latency_stats_tail_visible_where_mean_hides_it():
    """★ 평균만 보면 '괜찮다'로 읽히는 상황에서 p95 가 문제를 드러내야 한다.

    35명이 3초, 5명이 120초 — 평균 17초는 안심하게 만들지만 8명 중 1명이 2분을
    기다린 것이다. 그 5명이 수업을 포기한다.
    """
    vals = [3000] * 35 + [120000] * 5
    st = LA.latency_stats(vals)
    assert st["avg"] < 20000, "평균은 낮게 보인다"
    assert st["p95"] >= 120000, "p95 가 꼬리를 드러내야 한다"


# ──────────────────────────────────────────────────────────────────────────────
# 동시 접속 — 구간 겹침
# ──────────────────────────────────────────────────────────────────────────────

def test_concurrency_counts_overlap_not_starts():
    """★ 핵심: 동접은 '그 시각에 시작한 수'가 아니라 '걸쳐 있는 수'다.

    10분짜리 턴 3개가 1분 간격으로 시작하면, 시작 수는 분당 1건이지만 동시 실행은
    3건이다. 시작만 세면 부하를 3분의 1로 과소평가한다.
    """
    rows = [_turn(0, 600_000), _turn(1, 600_000), _turn(2, 600_000)]
    out = LA.concurrency_timeline(rows, bucket_seconds=60)
    assert out["peak"] == 3
    assert all(b["started"] <= 1 for b in out["buckets"])


def test_concurrency_peak_time_is_kst():
    """저장은 UTC 지만 화면 라벨은 반드시 한국 시간이어야 한다."""
    rows = [_turn(0, 60_000)]      # UTC 00:00 = KST 09:00
    out = LA.concurrency_timeline(rows, bucket_seconds=60)
    assert out["peak_at"].endswith("09:00"), out["peak_at"]


def test_concurrency_non_overlapping_stays_one():
    """겹치지 않는 턴은 동접 1이다 — 합계와 혼동하면 안 된다."""
    rows = [_turn(0, 30_000), _turn(10, 30_000), _turn(20, 30_000)]
    assert LA.concurrency_timeline(rows, bucket_seconds=60)["peak"] == 1


def test_concurrency_recovers_start_from_duration():
    """started_at 이 없는 구버전 행도 ts-duration 으로 복원돼 계산에 든다."""
    rows = [{"ts": _t(5), "duration_ms": 300_000, "status": "ok"},
            {"ts": _t(5), "duration_ms": 300_000, "status": "ok"}]
    out = LA.concurrency_timeline(rows)
    assert out["measured"] == 2
    assert out["peak"] == 2


def test_concurrency_skips_rows_with_no_timing():
    """시작도 소요도 모르는 행은 **추측하지 않고 뺀다** — 채우면 곡선이 조용히 부푼다."""
    rows = [{"ts": _t(0), "duration_ms": 0, "status": "ok"}]
    out = LA.concurrency_timeline(rows)
    assert out["measured"] == 0
    assert out["peak"] == 0


def test_concurrency_empty_period_is_safe():
    out = LA.concurrency_timeline([])
    assert out["peak"] == 0 and out["buckets"] == []


def test_concurrency_downgrades_resolution_on_huge_span():
    """몇 달치를 분 단위로 그리면 버킷이 수십만 개다 — 자동으로 해상도를 낮춰야 한다."""
    rows = [_turn(0, 1000), _turn(60 * 24 * 90, 1000)]   # 90일 간격
    out = LA.concurrency_timeline(rows, bucket_seconds=60)
    assert len(out["buckets"]) <= 5001, len(out["buckets"])


# ──────────────────────────────────────────────────────────────────────────────
# 동접 대비 지연 — "40명이 버티나"의 답
# ──────────────────────────────────────────────────────────────────────────────

def test_latency_by_concurrency_separates_busy_from_idle():
    """★ 한산할 때 빠르고 붐빌 때 느려지는 패턴이 구간으로 갈려 보여야 한다."""
    quiet = [_turn(i * 10, 1000) for i in range(3)]          # 겹치지 않음 → 동접 1
    busy = [_turn(100, 60_000) for _ in range(10)]           # 전부 겹침 → 동접 10
    out = {r["bucket"]: r for r in LA.latency_by_concurrency(quiet + busy)}
    assert out["1"]["p95"] < out["8-15"]["p95"]


def test_latency_by_concurrency_empty_is_safe():
    assert LA.latency_by_concurrency([]) == []


# ──────────────────────────────────────────────────────────────────────────────
# 유형별 집계 — 어떤 질문이 비싸고 느린가
# ──────────────────────────────────────────────────────────────────────────────

def test_group_stats_splits_cost_by_outcome():
    """대화만 한 턴과 코드가 나온 턴의 단가가 갈려야 한다."""
    rows = [
        {"outcome": "chat", "weighted_tokens": 1000, "duration_ms": 1000, "status": "ok"},
        {"outcome": "code", "weighted_tokens": 100000, "duration_ms": 90000, "status": "ok"},
    ]
    out = {r["key"]: r for r in LA.group_stats(rows, "outcome", LA._OUTCOME_LABEL)}
    assert out["code"]["usd_per_turn"] > out["chat"]["usd_per_turn"] * 50


def test_group_stats_counts_failures_per_type():
    rows = [{"intent": "question", "weighted_tokens": 10, "duration_ms": 5, "status": "error"},
            {"intent": "question", "weighted_tokens": 10, "duration_ms": 5, "status": "ok"}]
    out = LA.group_stats(rows, "intent", LA._INTENT_LABEL)[0]
    assert out["fail"] == 1 and out["fail_pct"] == 50.0


def test_group_stats_labels_missing_as_unknown():
    """구버전 행(빈 값)이 라벨 없이 사라지면 합계가 안 맞는다."""
    out = LA.group_stats([{"intent": "", "weighted_tokens": 1, "status": "ok"}],
                         "intent", LA._INTENT_LABEL)
    assert out[0]["key"] == "(미상)"


# ──────────────────────────────────────────────────────────────────────────────
# 재사용 임계 곡선 — 비용을 '줄이는' 결정에 쓰는 표
# ──────────────────────────────────────────────────────────────────────────────

def test_reuse_curve_shows_headroom_below_threshold():
    """★ cold 로 빠졌지만 점수가 아깝게 낮은 턴이 몇 건인지 나와야 한다.

    top1 이 0.58 에 몰려 있으면 임계를 0.55 로 내리는 것만으로 재사용이 확 는다.
    이 판단은 top1 을 원장에 남긴 날에만 가능하다.
    """
    rows = [{"reuse_tier": "cold", "reuse_top1": 0.58, "weighted_tokens": 10000}
            for _ in range(4)]
    curve = {r["threshold"]: r for r in LA.reuse_threshold_curve(rows)}
    assert curve[0.55]["turns"] == 4
    assert 0.60 not in curve or curve[0.60]["turns"] == 0


def test_reuse_curve_ignores_non_cold_turns():
    """이미 재사용된 턴은 '더 아낄 여력'이 아니다."""
    rows = [{"reuse_tier": "direct_serve", "reuse_top1": 0.9, "weighted_tokens": 1}]
    assert LA.reuse_threshold_curve(rows) == []


def test_reuse_curve_without_top1_is_empty_not_wrong():
    """점수가 안 남은 구버전 데이터로 근거 없는 절감을 주장하면 안 된다."""
    rows = [{"reuse_tier": "cold", "reuse_top1": 0, "weighted_tokens": 100}]
    assert LA.reuse_threshold_curve(rows) == []


# ──────────────────────────────────────────────────────────────────────────────
# 시간대 — 저장 UTC / 표시 KST
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("utc,kst", [
    ("2026-08-22 00:00:00", "2026-08-22 09:00"),   # 수업 시작
    ("2026-08-21 15:00:00", "2026-08-22 00:00"),   # KST 자정
])
def test_bucket_labels_are_kst(utc, kst):
    start = datetime.strptime(utc, "%Y-%m-%d %H:%M:%S")
    rows = [{"started_at": start, "ts": start + timedelta(seconds=1),
             "duration_ms": 1000, "status": "ok"}]
    assert LA.concurrency_timeline(rows)["buckets"][0]["t"] == kst


# ──────────────────────────────────────────────────────────────────────────────
# 접속 환경 — 사람과 스크립트를 가른다
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ua,device,os_name,browser", [
    ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
     "Version/17.0 Mobile/15E148 Safari/604.1", "태블릿", "iPadOS", "Safari"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "Chrome/131.0 Safari/537.36", "PC", "Windows", "Chrome"),
    ("Mozilla/5.0 (Linux; Android 13; SM-X200) AppleWebKit/537.36 "
     "Chrome/131.0 Safari/537.36", "태블릿", "Android", "Chrome"),
    ("Mozilla/5.0 (X11; CrOS x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
     "크롬북", "ChromeOS", "Chrome"),
])
def test_parse_client_identifies_class_devices(ua, device, os_name, browser):
    c = LA.parse_client(ua)
    assert (c["device"], c["os"], c["browser"]) == (device, os_name, browser)


def test_edge_is_not_reported_as_chrome():
    """★ Edge·Whale·삼성인터넷은 UA 에 'Chrome' 을 포함한다 — 구체적인 것부터 봐야 한다."""
    ua = ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/131.0 "
          "Safari/537.36 Edg/131.0")
    assert LA.parse_client(ua)["browser"] == "Edge"


@pytest.mark.parametrize("ua", ["curl/8.7.1", "python-requests/2.31", "HeadlessChrome/131"])
def test_scripts_are_flagged_as_bots(ua):
    """★ 2026-08-21: '사용자 46명'이 찍혔는데 45명이 부하 테스트 계정이었다.

    사람 수를 못 믿으면 수업 규모를 못 읽는다.
    """
    assert LA.parse_client(ua)["is_bot"] is True


def test_client_breakdown_separates_humans_from_scripts():
    rows = ([{"user_agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) Safari/604.1",
              "user_id": f"stu{i}", "duration_ms": 1000, "status": "ok"} for i in range(3)]
            + [{"user_agent": "curl/8.7.1", "user_id": "bot", "duration_ms": 10,
                "status": "ok"}])
    out = LA.client_breakdown(rows)
    assert out["human_users"] == 3
    assert out["script_users"] == 1


def test_client_breakdown_survives_missing_user_agent():
    """구버전 행(UA 미기록)이 있어도 집계가 죽으면 안 된다."""
    out = LA.client_breakdown([{"user_id": "u", "duration_ms": 1, "status": "ok"}])
    assert out["by_client"] and out["by_client"][0]["turns"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 서버 리소스 — 상한에 닿기 전에 보여야 한다
# ──────────────────────────────────────────────────────────────────────────────

def test_resource_usage_reports_pct_of_limit():
    """★ MB 숫자만으로는 '많은지'를 모른다 — 상한 대비로 봐야 판단이 된다."""
    rows = [{"replica": "edu-agent-1", "mem_mb": 512}]
    out = LA.resource_usage(rows, limit_mb=1024)
    assert out["replicas"][0]["pct_of_limit"] == 50


def test_resource_usage_surfaces_the_worst_replica_first():
    """한 대만 무거운 상황이 흔하다 — 그 한 대가 표 맨 위에 와야 한다."""
    rows = ([{"replica": "a", "mem_mb": 300}] * 5
            + [{"replica": "b", "mem_mb": 900}])
    out = LA.resource_usage(rows)
    assert out["replicas"][0]["replica"] == "b"
    assert out["peak_pct"] >= 87


def test_resource_usage_ignores_unrecorded_turns():
    """구버전 행(mem_mb 없음)이 0MB 로 섞이면 평균이 거짓으로 낮아진다."""
    rows = [{"replica": "a", "mem_mb": 0}, {"replica": "a", "mem_mb": 400}]
    out = LA.resource_usage(rows)
    assert out["replicas"][0]["turns"] == 1
    assert out["replicas"][0]["avg_mb"] == 400


def test_resource_usage_empty_is_safe():
    out = LA.resource_usage([])
    assert out["replicas"] == [] and out["peak_pct"] == 0


def test_mem_limit_matches_compose():
    """★ 상한 대비 % 가 의미를 가지려면 compose 의 mem_limit 과 같아야 한다.

    compose 만 바꾸고 여기를 안 바꾸면 화면의 % 가 조용히 틀린다.
    """
    import pathlib
    import re as _re
    # PyYAML 없이 정규식으로 읽는다 — 이 검사는 **건너뛰면 안 된다**.
    # importorskip 을 쓰면 의존성이 없는 환경(CI)에서 조용히 통과해, 정작 값이
    # 어긋났을 때 아무도 모른다. 한 줄짜리 스칼라라 정규식으로 충분하다.
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    m = _re.search(r"^\s*mem_limit:\s*(\d+)\s*([gGmM])\s*$", text, _re.M)
    assert m, "docker-compose.yml 에 mem_limit 이 없다 — 상한 없는 컨테이너는 호스트를 죽인다"
    mb = int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    assert LA.MEM_LIMIT_MB == mb, (
        f"compose={m.group(0).strip()} 인데 load_analysis={LA.MEM_LIMIT_MB}MB "
        "— 화면의 '상한 대비 %' 가 조용히 틀린다")
