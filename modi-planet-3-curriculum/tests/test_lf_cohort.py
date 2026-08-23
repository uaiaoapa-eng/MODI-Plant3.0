"""#93: scripts/lf_cohort.py 재사용 코호트 측정 CLI 검증.

네트워크 금지 — urllib.request.urlopen 을 monkeypatch 해 가짜 Langfuse 응답을 주입한다.
검증 범위: ①cohortize 집계 정확성(티어/vec 코호트 평균·중앙값) ②합성 유저 제외+제외건수
③85~89 밴드 분류(84.9/85/89.9/90 경계) ④docs_restored 스코어→metadata 폴백
⑤env 누락 시 exit 1, 그리고 fetch_traces 페이지네이션/필터링(보너스).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lf_cohort  # noqa: E402


def _score(name, value=None, string_value=None):
    s = {"name": name}
    if value is not None:
        s["value"] = value
    if string_value is not None:
        s["stringValue"] = string_value
    return s


def _trace(trace_id, tier=None, vec_promoted=None, direct_served=None, direct_serve_score=None,
           docs_restored=None, gen_tokens=None, build_success=None, cost=0.01, latency=10.0,
           user_id="real-user-1", metadata=None, query="테스트 쿼리", ts="2026-07-08T00:00:00Z"):
    scores = []
    if tier is not None:
        scores.append(_score(lf_cohort.SCORE_REUSE_TIER, string_value=tier))
    if vec_promoted is not None:
        scores.append(_score(lf_cohort.SCORE_VEC_PROMOTED, value=1 if vec_promoted else 0))
    if direct_served is not None:
        scores.append(_score(lf_cohort.SCORE_DIRECT_SERVED, value=1 if direct_served else 0))
    if direct_serve_score is not None:
        scores.append(_score(lf_cohort.SCORE_DIRECT_SERVE_SCORE, value=direct_serve_score))
    if docs_restored is not None:
        scores.append(_score(lf_cohort.SCORE_DOCS_RESTORED, value=docs_restored))
    if gen_tokens is not None:
        scores.append(_score(lf_cohort.SCORE_OUTPUT_GENERATE_TOKENS, value=gen_tokens))
    if build_success is not None:
        scores.append(_score(lf_cohort.SCORE_BUILD_SUCCESS, value=1 if build_success else 0))
    return {
        "id": trace_id,
        "name": "haiku · react · quick",
        "timestamp": ts,
        "userId": user_id,
        "totalCost": cost,
        "latency": latency,
        "scores": scores,
        "metadata": metadata or {},
        "input": {"message": query},
    }


# --------------------------------------------------------------------------
# ① cohortize 집계 정확성
# --------------------------------------------------------------------------

def test_cohortize_tier_aggregation():
    traces = [
        _trace("d1", tier="direct_serve", cost=0.01, latency=10.0),
        _trace("d2", tier="direct_serve", cost=0.03, latency=20.0),
        _trace("n1", tier="near", cost=0.10, latency=30.0),
        _trace("c1", tier="cold", cost=0.20, latency=90.0),
    ]
    report = lf_cohort.cohortize(traces)
    ds = report["tiers"]["direct_serve"]
    assert ds["n"] == 2
    assert ds["cost_avg"] == pytest.approx(0.02)
    assert ds["cost_med"] == pytest.approx(0.02)
    assert ds["latency_avg"] == pytest.approx(15.0)
    assert report["tiers"]["near"]["n"] == 1
    assert report["tiers"]["cold"]["n"] == 1


def test_cohortize_vec_promoted_grouping():
    traces = [
        _trace("v1", tier="near", vec_promoted=True, cost=0.05, latency=25.0),
        _trace("v2", tier="near", vec_promoted=True, cost=0.07, latency=35.0),
        _trace("v3", tier="near", vec_promoted=False, cost=0.15, latency=60.0),
        _trace("v4", tier="cold", cost=0.20, latency=80.0),  # 게이트 미발동 — vec 그룹 미포함
    ]
    report = lf_cohort.cohortize(traces)
    assert report["vec_promoted"]["true"]["n"] == 2
    assert report["vec_promoted"]["true"]["cost_avg"] == pytest.approx(0.06)
    assert report["vec_promoted"]["false"]["n"] == 1


def test_cohortize_build_fail_reported_per_tier():
    traces = [
        _trace("b1", tier="cold", build_success=True),
        _trace("b2", tier="cold", build_success=False),
    ]
    report = lf_cohort.cohortize(traces)
    assert report["tiers"]["cold"]["build_fail_n"] == 1


# --------------------------------------------------------------------------
# ② 합성 유저 제외 + 제외 건수
# --------------------------------------------------------------------------

def test_synthetic_users_excluded_by_default_and_counted():
    traces = [
        _trace("r1", tier="near", user_id="real-user-1"),
        _trace("s1", tier="near", user_id="sim-abc"),
        _trace("s2", tier="cold", user_id="d3u-xyz"),
        _trace("s3", tier="cold", user_id="qu-1"),
    ]
    report = lf_cohort.cohortize(traces)
    assert report["n_total"] == 4
    assert report["n_synthetic_excluded"] == 3
    assert report["tiers"]["near"]["n"] == 1
    assert report["tiers"]["cold"]["n"] == 0


def test_include_synthetic_flag_keeps_all_and_reports_zero_excluded():
    traces = [
        _trace("r1", tier="near", user_id="real-user-1"),
        _trace("s1", tier="near", user_id="sim-abc"),
    ]
    report = lf_cohort.cohortize(traces, include_synthetic=True)
    assert report["n_synthetic_excluded"] == 0
    assert report["tiers"]["near"]["n"] == 2


def test_is_synthetic_prefixes():
    assert lf_cohort.is_synthetic("d3u-1")
    assert lf_cohort.is_synthetic("qu-2")
    assert lf_cohort.is_synthetic("meas-user-3")
    assert lf_cohort.is_synthetic("u1abcdef")
    assert lf_cohort.is_synthetic("sim-4")
    assert lf_cohort.is_synthetic("verify-5")
    assert not lf_cohort.is_synthetic("real-user-1")
    assert not lf_cohort.is_synthetic(None)


# --------------------------------------------------------------------------
# ③ 85~89 밴드 분류(경계값)
# --------------------------------------------------------------------------

def test_band_85_89_boundaries():
    traces = [
        _trace("q84", tier="near", direct_served=False, direct_serve_score=84.9, query="q84"),
        _trace("q85", tier="near", direct_served=False, direct_serve_score=85, query="q85"),
        _trace("q89", tier="near", direct_served=False, direct_serve_score=89.9, query="q89"),
        _trace("q90", tier="near", direct_served=False, direct_serve_score=90, query="q90"),
    ]
    report = lf_cohort.cohortize(traces)
    hist = report["satisfaction"]["histogram"]
    assert hist["60-84"] == 1  # 84.9
    assert hist["85-89"] == 2  # 85, 89.9
    assert hist["90-100"] == 1  # 90

    band_ids = {row["trace_id"] for row in report["satisfaction"]["band_85_89"]}
    assert band_ids == {"q85", "q89"}


def test_band_85_89_only_when_rejected():
    """direct_served=True(수락)면 85~89 라도 '거절 구간' 상세에서 빠진다."""
    traces = [
        _trace("acc", tier="direct_serve", direct_served=True, direct_serve_score=87,
               query="accepted"),
        _trace("rej", tier="near", direct_served=False, direct_serve_score=87, query="rejected"),
    ]
    report = lf_cohort.cohortize(traces)
    band_ids = {row["trace_id"] for row in report["satisfaction"]["band_85_89"]}
    assert band_ids == {"rej"}


def test_band_85_89_detail_fields():
    traces = [
        _trace("rej1", tier="near", direct_served=False, direct_serve_score=88, query="장애물 회피",
               cost=0.18, latency=91.0,
               metadata={"reused": {"source_title": "피하기 게임"}, "direct_served": {"delta": False}}),
    ]
    report = lf_cohort.cohortize(traces)
    row = report["satisfaction"]["band_85_89"][0]
    assert row["query"] == "장애물 회피"
    assert row["source_title"] == "피하기 게임"
    assert row["delta"] is False
    assert row["cost_paid"] == pytest.approx(0.18)
    assert row["latency"] == pytest.approx(91.0)


def test_satisfaction_excludes_unverified_turns_but_tier_includes_them():
    traces = [
        _trace("cold1", tier="cold"),  # 검증 안 돔(direct_serve_score 없음)
    ]
    report = lf_cohort.cohortize(traces)
    total_hist = sum(report["satisfaction"]["histogram"].values())
    assert total_hist == 0
    assert report["tiers"]["cold"]["n"] == 1


# --------------------------------------------------------------------------
# ④ docs_restored 스코어 → metadata 폴백
# --------------------------------------------------------------------------

def test_docs_restored_score_preferred_over_metadata():
    t = _trace("dr1", tier="direct_serve", direct_served=True, docs_restored=5,
               metadata={"direct_served": {"docs_restored": 999}})
    assert lf_cohort._docs_restored_of(t) == 5


def test_docs_restored_metadata_fallback_when_score_absent():
    t = _trace("dr2", tier="direct_serve", direct_served=True,
               metadata={"direct_served": {"docs_restored": 3}})
    assert lf_cohort._docs_restored_of(t) == 3


def test_docs_restored_none_when_both_absent():
    t = _trace("dr3", tier="direct_serve", direct_served=True)
    assert lf_cohort._docs_restored_of(t) is None


def test_docs_restored_aggregate_uses_fallback():
    traces = [
        _trace("s1", tier="direct_serve", direct_served=True, docs_restored=7),
        _trace("s2", tier="direct_serve", direct_served=True,
               metadata={"direct_served": {"docs_restored": 3}}),
        _trace("s3", tier="direct_serve", direct_served=True),  # N/A → 집계 제외
    ]
    report = lf_cohort.cohortize(traces)
    dr = report["docs_restored"]
    assert dr["served_turns"] == 3
    assert dr["restored_turns"] == 2
    assert dr["avg_docs"] == pytest.approx(5.0)  # (7+3)/2, N/A 제외


# --------------------------------------------------------------------------
# ⑤ env 누락 시 exit 1
# --------------------------------------------------------------------------

def test_main_exits_1_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    with pytest.raises(SystemExit) as exc:
        lf_cohort.main([])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "LANGFUSE_PUBLIC_KEY" in err


def test_fetch_traces_exits_when_called_directly_without_env(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    with pytest.raises(SystemExit) as exc:
        lf_cohort.fetch_traces(days=7, limit=10)
    assert exc.value.code == 1


def test_base_url_falls_back_to_langfuse_host(monkeypatch):
    """배포 env 는 LANGFUSE_HOST 를 쓴다 — BASE_URL 부재 시 폴백해야 한다."""
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
    assert lf_cohort._base_url_or_exit() == "https://lf.example.com"


def test_base_url_prefers_explicit_base_url_over_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://explicit.example.com")
    monkeypatch.setenv("LANGFUSE_HOST", "https://host.example.com")
    assert lf_cohort._base_url_or_exit() == "https://explicit.example.com"


# --------------------------------------------------------------------------
# 보너스: fetch_traces 페이지네이션 + 이름 필터 + 상세 조회 (네트워크 monkeypatch)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_traces_filters_name_and_enriches_detail(monkeypatch):
    # days=7 컷오프는 실행 시각 기준이라, 하드코딩된 절대 날짜는 실행 시점에 따라
    # 컷오프 밖으로 밀려나 flaky 해진다. 항상 범위 안에 들도록 상대 시각을 쓴다.
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    list_payload = {
        "data": [
            {"id": "t1", "name": "haiku · react · quick", "timestamp": recent_ts,
             "userId": "real-user-1"},
            {"id": "t2", "name": "도구 실행", "timestamp": recent_ts,
             "userId": "real-user-1"},
        ],
        "meta": {"totalPages": 1},
    }
    detail_payload = {
        "id": "t1", "name": "haiku · react · quick", "timestamp": recent_ts,
        "userId": "real-user-1", "totalCost": 0.011, "latency": 20.0,
        "scores": [_score(lf_cohort.SCORE_REUSE_TIER, string_value="direct_serve")],
        "metadata": {}, "input": {"message": "hi"},
    }

    def fake_urlopen(req, timeout=30):
        url = req.full_url
        if "/api/public/traces/t1" in url:
            return _FakeResponse(detail_payload)
        if "/api/public/traces?" in url:
            return _FakeResponse(list_payload)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(lf_cohort.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")

    traces = lf_cohort.fetch_traces(days=7, limit=10)
    assert len(traces) == 1
    assert traces[0]["id"] == "t1"
    assert traces[0]["totalCost"] == 0.011


def test_fetch_traces_raises_runtime_error_on_http_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(lf_cohort.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")

    with pytest.raises(RuntimeError):
        lf_cohort.fetch_traces(days=7, limit=10)


# --------------------------------------------------------------------------
# render_markdown 스모크
# --------------------------------------------------------------------------

def test_render_markdown_smoke():
    traces = [
        _trace("d1", tier="direct_serve", direct_served=True, direct_serve_score=95, docs_restored=4),
        _trace("rej", tier="near", direct_served=False, direct_serve_score=87, query="q"),
    ]
    report = lf_cohort.cohortize(traces)
    md = lf_cohort.render_markdown(report)
    assert "direct_serve" in md
    assert "85~89" in md
    assert "rej" in md
