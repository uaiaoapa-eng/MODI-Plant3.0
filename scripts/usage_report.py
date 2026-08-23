#!/usr/bin/env python3
"""사용량·비용 리포트 — 수업 하루가 끝난 뒤 "얼마 나왔나" 를 답한다.

데이터 원천은 MySQL `usage_turns` 다. `/chat` 이 턴마다 finally 에서
`_usage_writeback_upstream` 으로 이중쓰기하므로(#133), 쿼터 집행(Redis)과 무관하게
분석용 원장이 쌓인다. 이 스크립트는 rag-search 의 `/api/usage/report` 를 호출해
사람이 읽는 형태로 출력한다(앱은 MySQL 직접 접근 금지 제약을 지킨다).

비용 환산 근거:
    weighted_tokens = input×1 + output×5 + cache_read×0.1 + cache_creation×1.25
이 비율은 Haiku 4.5 단가(입력 $1 / 출력 $5 / 캐시읽기 $0.1 / 캐시쓰기 $1.25 per MTok)와
정확히 일치한다 → **weighted_tokens / 1e6 = USD**. 모델을 바꾸면 이 등식이 깨지므로
리포트가 model 가정을 함께 출력한다.

사용 예
    # 오늘(KST) 전체
    python scripts/usage_report.py

    # 특정 날짜 / 기간
    python scripts/usage_report.py --start 2026-08-22
    python scripts/usage_report.py --start 2026-08-22 --end 2026-08-22

    # 한 사용자만
    python scripts/usage_report.py --user-id abcd-1234

    # 예산 대비 소진율 함께 보기
    python scripts/usage_report.py --budget-usd 100

    # 원본 JSON (다른 도구로 넘길 때)
    python scripts/usage_report.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_UPSTREAM = os.getenv("RAG_UPSTREAM", "http://localhost:8100")


def fetch(upstream: str, **params) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
    url = f"{upstream.rstrip('/')}/api/usage/report" + (f"?{q}" if q else "")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _bar(part: float, whole: float, width: int = 28) -> str:
    if whole <= 0:
        return ""
    n = max(0, min(width, round(part / whole * width)))
    return "█" * n + "·" * (width - n)


def render(rep: dict, budget_usd: float = 0.0) -> str:
    if not rep.get("ok"):
        return f"리포트 조회 실패: {rep.get('error', '알 수 없음')}"

    t = rep["totals"]
    p = rep["period"]
    a = rep["assumptions"]
    out: list[str] = []
    add = out.append

    add("=" * 66)
    add(f"  사용량·비용 리포트   {p['start']}  ~  {p['end']}")
    add("=" * 66)

    if t["turns"] == 0:
        add("  이 기간에 기록된 턴이 없습니다.")
        add("")
        add("  확인할 것:")
        add("   - RAG_UPSTREAM 이 설정돼 있어야 usage_turns 이중쓰기가 동작한다")
        add("     (미설정이면 /chat 이 사용량을 MySQL 에 보내지 않는다)")
        add("   - 기간(KST)이 맞는지 — 기본값은 '오늘'")
        return "\n".join(out)

    add("")
    add(f"  총 비용      ${t['usd']:,.2f}   (약 {t['krw']:,}원)")
    add(f"  턴 수        {t['turns']:,}회      사용자 {t['users']:,}명      세션 {t['sessions']:,}개")
    add(f"  턴당 평균    ${t['usd_per_turn']:.4f}    사용자당 평균 {t['turns_per_user']:.1f}턴")
    if p.get("first_turn"):
        add(f"  실제 구간    {p['first_turn']}  ~  {p['last_turn']}")

    if budget_usd > 0:
        pct = t["usd"] / budget_usd * 100
        add("")
        add(f"  예산 ${budget_usd:,.0f} 대비   {pct:5.1f}%  {_bar(t['usd'], budget_usd)}")
        add(f"  남은 예산            ${max(0.0, budget_usd - t['usd']):,.2f}")

    # 토큰 구성 — 어디에 돈이 쓰였는지
    add("")
    add("─ 토큰 구성 (가중치 반영 = 실제 비용 기여도) " + "─" * 20)
    parts = [
        ("출력    ×5.0", t["output_tokens"] * 5.0, t["output_tokens"]),
        ("캐시쓰기 ×1.25", t["cache_creation_tokens"] * 1.25, t["cache_creation_tokens"]),
        ("입력    ×1.0", t["input_tokens"] * 1.0, t["input_tokens"]),
        ("캐시읽기 ×0.1", t["cache_read_tokens"] * 0.1, t["cache_read_tokens"]),
    ]
    total_w = sum(w for _, w, _ in parts) or 1
    for label, weighted, raw in sorted(parts, key=lambda x: -x[1]):
        add(f"  {label:<14} {weighted / total_w * 100:5.1f}%  {_bar(weighted, total_w)}  "
            f"원시 {raw:>12,} tok")

    if rep.get("by_kind"):
        add("")
        add("─ 모드·타입별 " + "─" * 50)
        add(f"  {'모드':<10}{'타입':<10}{'턴':>7}{'비용':>10}{'원화':>12}")
        for r in rep["by_kind"]:
            add(f"  {r['mode']:<10}{r['coding_type']:<10}{r['turns']:>7,}"
                f"{'$' + format(r['usd'], '.2f'):>10}{format(r['krw'], ',') + '원':>12}")

    if rep.get("by_hour"):
        add("")
        add("─ 시간대별 (동시 사용 규모 파악) " + "─" * 31)
        peak = max(r["turns"] for r in rep["by_hour"])
        for r in rep["by_hour"]:
            add(f"  {r['hour']}  턴 {r['turns']:>5,}  사용자 {r['users']:>4,}  "
                f"${r['usd']:>7.2f}  {_bar(r['turns'], peak, 20)}")

    if rep.get("top_users"):
        add("")
        add("─ 사용자별 상위 " + "─" * 48)
        add(f"  {'user_id':<40}{'턴':>7}{'비용':>10}{'원화':>12}")
        for r in rep["top_users"]:
            uid = (r["user_id"] or "(없음)")[:38]
            add(f"  {uid:<40}{r['turns']:>7,}{'$' + format(r['usd'], '.2f'):>10}"
                f"{format(r['krw'], ',') + '원':>12}")

    add("")
    add(f"  가정: model={a['model']}  환율 {a['krw_per_usd']:,.0f}원/$")
    add(f"        {a['note']}")
    add("  ※ 현재 CLI 구독 모드면 실제 청구는 발생하지 않는다(구독 정액).")
    add("     위 금액은 '이 사용량을 API 과금으로 환산하면' 이다.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="edu-agent 사용량·비용 리포트")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                    help=f"rag-search 주소 (기본 {DEFAULT_UPSTREAM})")
    ap.add_argument("--start", default="", help="YYYY-MM-DD (KST). 비우면 오늘")
    ap.add_argument("--end", default="", help="YYYY-MM-DD (KST). 비우면 start 와 같은 날")
    ap.add_argument("--user-id", default="", help="특정 사용자만")
    ap.add_argument("--limit-users", type=int, default=30, help="상위 사용자 표 인원수")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="예산 대비 소진율 표시")
    ap.add_argument("--json", action="store_true", help="원본 JSON 출력")
    args = ap.parse_args()

    try:
        rep = fetch(args.upstream, start=args.start, end=args.end,
                    user_id=args.user_id, limit_users=args.limit_users)
    except Exception as e:
        print(f"조회 실패({args.upstream}): {e}", file=sys.stderr)
        print("  rag-search 가 떠 있는지, RAG_UPSTREAM 주소가 맞는지 확인하세요.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(rep, budget_usd=args.budget_usd))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
