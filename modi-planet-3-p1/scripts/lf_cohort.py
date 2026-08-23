#!/usr/bin/env python3
"""EDU-27 재사용 코호트 측정 CLI (#93).

Langfuse 공개 API에서 루트 채팅턴 트레이스를 모아 티어별(direct_serve/near/cold)·
vec승격별·문서복원별 비용/지연/토큰과 직접서브 만족도 밴드(특히 85~89 거절 구간)를
한 번의 실행으로 리포트한다.

설계문서: docs/design/reuse-cohort-measurement.md §3.2(스키마)·§5(엣지케이스)
스코어 발행 지점: agent/orchestrator_stream.py:744-770 `_emit_turn_scores()`
(이번 구현은 그 스코어 이름을 그대로 상수로 옮긴 것 — 발행측 변경 금지)

사용법(환경변수 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_BASE_URL 필수):
    python3 scripts/lf_cohort.py [--days 7] [--limit 500] [--json out.json] [--include-synthetic]

stdlib만 사용(urllib/json/statistics/argparse/datetime). httpx·pandas 금지.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# --- 발행 스코어 이름(agent/orchestrator_stream.py `_emit_turn_scores` 그대로, 변경 금지) ---
SCORE_REUSE_TIER = "재사용 티어 (reuse_tier)"                       # CATEGORICAL: direct_serve/near/cold
SCORE_VEC_PROMOTED = "재사용 vec승격 (reuse_vec_promoted)"           # BOOLEAN, 게이트 돈 턴만
SCORE_DIRECT_SERVED = "직접서브 (direct_served)"                     # BOOLEAN, 검증 돈 턴만
SCORE_DIRECT_SERVE_SCORE = "직접서브 만족도 (direct_serve_score)"     # NUMERIC, 검증 돈 턴만
SCORE_DOCS_RESTORED = "직접서브 문서복원 (docs_restored)"             # NUMERIC, 신규 턴부터
SCORE_OUTPUT_GENERATE_TOKENS = "출력 전체재작성 토큰 (output_generate_tokens)"  # NUMERIC
SCORE_BUILD_SUCCESS = "빌드 성공 (build_success)"                    # BOOLEAN, 빌드 시도 턴만

TIERS = ("direct_serve", "near", "cold")

# 합성/테스트 유저 프리픽스(userId 필드) — 설계문서 §2 검증됨.
SYNTHETIC_PREFIXES = ("d3u-", "qu-", "meas-user", "u1", "sim-", "verify-")

SATISFACTION_BUCKETS = ("0-59", "60-84", "85-89", "90-100")


def is_synthetic(user_id) -> bool:
    """합성/테스트 유저인지(userId 프리픽스 기준). None은 비합성으로 본다."""
    if not user_id:
        return False
    return any(str(user_id).startswith(p) for p in SYNTHETIC_PREFIXES)


# ---------------------------------------------------------------------------
# Langfuse API
# ---------------------------------------------------------------------------

def _env_or_exit(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(
            f"오류: 환경변수 {name} 이(가) 설정되지 않았습니다. "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL 를 설정하세요.",
            file=sys.stderr,
        )
        sys.exit(1)
    return v


def _base_url_or_exit() -> str:
    """Langfuse 베이스 URL 을 해석한다.

    배포 env(deploy/onprem.env)는 표준 Langfuse SDK 변수명 LANGFUSE_HOST 를 쓰므로
    LANGFUSE_BASE_URL 이 없으면 LANGFUSE_HOST 로 폴백한다(둘 다 없으면 exit 1).
    """
    v = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    if not v:
        print(
            "오류: 환경변수 LANGFUSE_BASE_URL(또는 LANGFUSE_HOST) 이(가) 설정되지 않았습니다. "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL 를 설정하세요.",
            file=sys.stderr,
        )
        sys.exit(1)
    return v


def _auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def _get_json(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Langfuse API 오류 {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Langfuse API 접속 실패: {url} ({e})") from e
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"Langfuse API 응답 파싱 실패: {url} ({e})") from e


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_traces(days: int, limit: int, base_url: str = None,
                  public_key: str = None, secret_key: str = None) -> list[dict]:
    """루트 채팅턴 트레이스를 페이지네이션으로 모은다.

    - GET {base}/api/public/traces?limit=50&page=N&orderBy=timestamp.desc, Basic auth
    - days 기간 밖(더 오래된 트레이스)이 나오면 중단
    - 이름에 '·' 포함(루트 채팅턴, "모델 · coding_type · mode")만 수집
    - 목록 응답에 scores 배열이 없으면 필요한 턴만 상세 GET /api/public/traces/{id} 조회
    """
    base_url = (base_url or _base_url_or_exit()).rstrip("/")
    public_key = public_key or _env_or_exit("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or _env_or_exit("LANGFUSE_SECRET_KEY")
    headers = {"Authorization": _auth_header(public_key, secret_key)}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    collected: list[dict] = []
    page = 1
    while len(collected) < limit:
        url = f"{base_url}/api/public/traces?limit=50&page={page}&orderBy=timestamp.desc"
        data = _get_json(url, headers)
        items = data.get("data") or []
        if not items:
            break

        stop = False
        for t in items:
            tdt = _parse_ts(t.get("timestamp"))
            if tdt is not None and tdt < cutoff:
                stop = True
                break
            name = t.get("name") or ""
            if "·" not in name:
                continue
            collected.append(t)
            if len(collected) >= limit:
                break
        if stop:
            break

        meta = data.get("meta") or {}
        total_pages = meta.get("totalPages")
        if total_pages is not None and page >= total_pages:
            break
        page += 1

    enriched: list[dict] = []
    for t in collected:
        # 목록 API 의 scores 는 스코어 **ID 문자열** 배열이라 이름 매칭에 못 쓴다(실측,
        # 2026-07-08 라이브 스모크에서 확인). dict 배열(상세 응답 형태)일 때만 재사용하고,
        # 그 외에는 상세 GET 으로 스코어 객체를 받는다.
        sc = t.get("scores")
        if isinstance(sc, list) and sc and all(isinstance(s, dict) for s in sc):
            enriched.append(t)
            continue
        detail = _get_json(f"{base_url}/api/public/traces/{t['id']}", headers)
        enriched.append(detail)
    return enriched


# ---------------------------------------------------------------------------
# 스코어/메타데이터 추출 헬퍼
# ---------------------------------------------------------------------------

def _find_score(trace: dict, name: str) -> dict | None:
    for s in trace.get("scores") or []:
        if s.get("name") == name:
            return s
    return None


def _tier_of(trace: dict):
    s = _find_score(trace, SCORE_REUSE_TIER)
    if s is None:
        return None
    v = s.get("stringValue")
    return v if v is not None else s.get("value")


def _bool_of(trace: dict, name: str):
    s = _find_score(trace, name)
    if s is None:
        return None
    v = s.get("value")
    if v is None:
        return None
    return bool(v)


def _num_of(trace: dict, name: str):
    s = _find_score(trace, name)
    if s is None:
        return None
    return s.get("value")


def _docs_restored_of(trace: dict):
    """docs_restored 스코어 우선, 없으면 metadata direct_served.docs_restored 폴백."""
    v = _num_of(trace, SCORE_DOCS_RESTORED)
    if v is not None:
        return v
    meta = trace.get("metadata") or {}
    ds = meta.get("direct_served") or {}
    return ds.get("docs_restored")


def _satisfaction_bucket(score: float) -> str:
    if score < 60:
        return "0-59"
    if score < 85:
        return "60-84"
    if score < 90:
        return "85-89"
    return "90-100"


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------

def _mean(vals, ndigits=6):
    return round(statistics.mean(vals), ndigits) if vals else None


def _median(vals, ndigits=6):
    return round(statistics.median(vals), ndigits) if vals else None


def _tier_stats(items: list[dict]) -> dict:
    costs = [t.get("totalCost") for t in items if t.get("totalCost") is not None]
    lats = [t.get("latency") for t in items if t.get("latency") is not None]
    gens = [_num_of(t, SCORE_OUTPUT_GENERATE_TOKENS) for t in items]
    gens = [g for g in gens if g is not None]
    build_fail_n = sum(1 for t in items if _bool_of(t, SCORE_BUILD_SUCCESS) is False)
    return {
        "n": len(items),
        "cost_avg": _mean(costs),
        "cost_med": _median(costs),
        "latency_avg": _mean(lats, ndigits=2),
        "gen_tokens_avg": _mean(gens, ndigits=1) if gens else 0.0,
        # 설계문서 §5: build_success=False 건수를 tier별로 병기(절감 착시 방지).
        "build_fail_n": build_fail_n,
    }


def cohortize(traces: list[dict], include_synthetic: bool = False) -> dict:
    """트레이스 리스트를 코호트 리포트 딕셔너리로 집계(설계문서 §3.2 스키마)."""
    n_total = len(traces)

    if include_synthetic:
        analyzed = list(traces)
        n_synthetic_excluded = 0
    else:
        analyzed = []
        n_synthetic_excluded = 0
        for t in traces:
            if is_synthetic(t.get("userId")):
                n_synthetic_excluded += 1
            else:
                analyzed.append(t)

    timestamps = [d for d in (_parse_ts(t.get("timestamp")) for t in traces) if d]
    period = {
        "from": min(timestamps).date().isoformat() if timestamps else None,
        "to": max(timestamps).date().isoformat() if timestamps else None,
    }

    # 티어별 집계
    tiers = {tier: _tier_stats([t for t in analyzed if _tier_of(t) == tier]) for tier in TIERS}

    # vec_promoted 별 집계(게이트가 실제로 돈 턴만 — 스코어 자체가 없으면 미포함)
    vec_true = [t for t in analyzed if _bool_of(t, SCORE_VEC_PROMOTED) is True]
    vec_false = [t for t in analyzed if _bool_of(t, SCORE_VEC_PROMOTED) is False]
    vec_promoted = {"true": _tier_stats(vec_true), "false": _tier_stats(vec_false)}

    # 문서복원(직접서브 accept 된 턴 대상)
    served = [t for t in analyzed if _bool_of(t, SCORE_DIRECT_SERVED) is True]
    docs_vals = []
    for t in served:
        d = _docs_restored_of(t)
        if d is not None:
            docs_vals.append(d)
    docs_restored = {
        "served_turns": len(served),
        "restored_turns": sum(1 for d in docs_vals if d and d > 0),
        "avg_docs": _mean(docs_vals, ndigits=1) if docs_vals else 0.0,
    }

    # 만족도(검증 돈 턴만 — near/cold 등 검증 안 돈 턴은 제외, tier 집계엔 이미 포함됨)
    histogram = {b: 0 for b in SATISFACTION_BUCKETS}
    band_85_89 = []
    for t in analyzed:
        score = _num_of(t, SCORE_DIRECT_SERVE_SCORE)
        if score is None:
            continue
        histogram[_satisfaction_bucket(score)] += 1
        if 85 <= score < 90 and _bool_of(t, SCORE_DIRECT_SERVED) is False:
            meta = t.get("metadata") or {}
            reused = meta.get("reused") or {}
            direct_served_meta = meta.get("direct_served") or {}
            inp = t.get("input")
            query = inp.get("message") if isinstance(inp, dict) else None
            band_85_89.append({
                "trace_id": t.get("id"),
                "query": query,
                "source_title": reused.get("source_title") or direct_served_meta.get("source_title"),
                "delta": direct_served_meta.get("delta"),
                "cost_paid": t.get("totalCost"),
                "latency": t.get("latency"),
            })

    satisfaction = {"histogram": histogram, "band_85_89": band_85_89}

    return {
        "period": period,
        "n_total": n_total,
        "n_synthetic_excluded": n_synthetic_excluded,
        "tiers": tiers,
        "vec_promoted": vec_promoted,
        "docs_restored": docs_restored,
        "satisfaction": satisfaction,
    }


# ---------------------------------------------------------------------------
# 리포트 렌더링
# ---------------------------------------------------------------------------

def _fmt(v, suffix=""):
    if v is None:
        return "N/A"
    return f"{v}{suffix}"


def render_markdown(report: dict) -> str:
    lines = []
    period = report.get("period") or {}
    lines.append("# 재사용 코호트 리포트 (lf_cohort.py)")
    lines.append("")
    lines.append(f"- 기간: {period.get('from')} ~ {period.get('to')}")
    lines.append(f"- 전체 트레이스: {report.get('n_total')}건")
    lines.append(f"- 합성 유저 제외: {report.get('n_synthetic_excluded')}건")
    lines.append("")

    lines.append("## 티어별 (direct_serve / near / cold)")
    lines.append("")
    lines.append("| tier | n | cost_avg | cost_med | latency_avg | gen_tokens_avg | build_fail_n |")
    lines.append("|---|---|---|---|---|---|---|")
    for tier in TIERS:
        s = report["tiers"].get(tier, {})
        lines.append(
            f"| {tier} | {s.get('n', 0)} | {_fmt(s.get('cost_avg'))} | {_fmt(s.get('cost_med'))} "
            f"| {_fmt(s.get('latency_avg'))} | {_fmt(s.get('gen_tokens_avg'))} | {s.get('build_fail_n', 0)} |"
        )
    lines.append("")

    lines.append("## vec 승격별 (게이트가 돈 턴만)")
    lines.append("")
    lines.append("| vec_promoted | n | cost_avg | cost_med | latency_avg | gen_tokens_avg | build_fail_n |")
    lines.append("|---|---|---|---|---|---|---|")
    for key in ("true", "false"):
        s = report["vec_promoted"].get(key, {})
        lines.append(
            f"| {key} | {s.get('n', 0)} | {_fmt(s.get('cost_avg'))} | {_fmt(s.get('cost_med'))} "
            f"| {_fmt(s.get('latency_avg'))} | {_fmt(s.get('gen_tokens_avg'))} | {s.get('build_fail_n', 0)} |"
        )
    lines.append("")

    dr = report.get("docs_restored") or {}
    lines.append("## 문서복원 (직접서브 턴 대상)")
    lines.append("")
    lines.append(f"- served_turns: {dr.get('served_turns', 0)}")
    lines.append(f"- restored_turns: {dr.get('restored_turns', 0)}")
    lines.append(f"- avg_docs: {dr.get('avg_docs', 0.0)}")
    lines.append("")

    sat = report.get("satisfaction") or {}
    hist = sat.get("histogram") or {}
    lines.append("## 직접서브 만족도 히스토그램")
    lines.append("")
    lines.append("| 0-59 | 60-84 | 85-89 | 90-100 |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| {hist.get('0-59', 0)} | {hist.get('60-84', 0)} | {hist.get('85-89', 0)} | {hist.get('90-100', 0)} |"
    )
    lines.append("")

    band = sat.get("band_85_89") or []
    lines.append(f"## 85~89 거절 구간 상세 ({len(band)}건)")
    lines.append("")
    if band:
        lines.append("| trace_id | query | source_title | delta | cost_paid | latency |")
        lines.append("|---|---|---|---|---|---|")
        for row in band:
            lines.append(
                f"| {row.get('trace_id')} | {row.get('query')} | {row.get('source_title')} "
                f"| {row.get('delta')} | {_fmt(row.get('cost_paid'))} | {_fmt(row.get('latency'))} |"
            )
    else:
        lines.append("(해당 구간 없음)")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="EDU-27 재사용 코호트 측정 CLI")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--include-synthetic", action="store_true")
    args = parser.parse_args(argv)

    # env 필수 — 없으면 exit 1 + 안내(fetch_traces 진입 전에 먼저 검증).
    _env_or_exit("LANGFUSE_PUBLIC_KEY")
    _env_or_exit("LANGFUSE_SECRET_KEY")
    _base_url_or_exit()

    try:
        traces = fetch_traces(args.days, args.limit)
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    report = cohortize(traces, include_synthetic=args.include_synthetic)
    print(render_markdown(report))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
