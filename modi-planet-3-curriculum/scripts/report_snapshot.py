#!/usr/bin/env python3
"""하루치 리포트를 DB(usage_reports)에 굳힌다 — 밤 크론용.

라이브 조회(/api/usage/report)는 열 때마다 usage_turns 를 재집계한다. "지금 얼마"에는
맞지만 청구 근거로는 약하다 — 원본이 정리되거나 집계 로직이 바뀌면 과거 수치가 조용히
달라지기 때문이다. 그래서 자정 직후에 **전날 확정치**를 굳혀 두고, 이후엔 그걸 읽는다.

파일이 아니라 DB 인 이유:
    파일 스냅샷은 배포 rsync·볼륨 교체·디스크 정리 어디서든 조용히 사라진다. 실제로
    `--delete` rsync 하나에 통째로 날아갈 뻔했다. 리포트는 세션·사용량과 같은 급의
    운영 데이터이므로 같은 곳(MySQL)에 둔다.

실제 일은 rag-search(:8100)가 한다 — 앱·스크립트는 MySQL 에 직접 붙지 않는다는 제약을
그대로 지킨다.

사용:
    python3 scripts/report_snapshot.py                    # 어제(KST)
    python3 scripts/report_snapshot.py --day 2026-08-22
    python3 scripts/report_snapshot.py --day today        # 오늘(중간 점검)
    python3 scripts/report_snapshot.py --backfill 30      # 최근 30일 소급 생성
    python3 scripts/report_snapshot.py --no-insight       # AI 분석 없이(LLM 비용 0)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DEFAULT_UPSTREAM = os.getenv("RAG_UPSTREAM", "http://localhost:8100")


def resolve_day(spec: str) -> str:
    """'', 'yesterday' → 어제 / 'today' → 오늘 / 'YYYY-MM-DD' → 그대로.

    기준은 KST 다 — day 는 타임스탬프가 아니라 "8월 22일 수업"이라는 영업일 라벨이다.
    """
    now = datetime.now(KST)
    s = (spec or "yesterday").strip().lower()
    if s in ("", "yesterday"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if s == "today":
        return now.strftime("%Y-%m-%d")
    datetime.strptime(s, "%Y-%m-%d")   # 형식 검증 — 틀리면 여기서 죽는 게 낫다
    return s


def days_back(n: int, *, end: str = "") -> list[str]:
    """최근 n일(오래된 것부터). 소급 생성용."""
    last = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now(KST).replace(tzinfo=None)
    return [(last - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _post(upstream: str, path: str, params: dict, timeout: float) -> dict:
    url = f"{upstream.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def snapshot(upstream: str, day: str, *, llm_mode: str = "",
             with_insight: bool = True, timeout: float = 300.0) -> dict:
    """하루치를 굳힌다. AI 분석 생성은 오래 걸릴 수 있어 타임아웃을 넉넉히 둔다."""
    return _post(upstream, "/api/usage/snapshot",
                 {"day": day, "llm_mode": llm_mode,
                  "with_insight": "true" if with_insight else "false"},
                 timeout)


def main() -> int:
    ap = argparse.ArgumentParser(description="하루치 사용량 리포트를 DB 에 굳힌다")
    ap.add_argument("--day", default="yesterday",
                    help="YYYY-MM-DD | today | yesterday (기본: yesterday, KST)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="최근 N일을 한 번에 소급 생성(--day 무시)")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--llm-mode", default=os.getenv("SNAPSHOT_LLM_MODE", ""),
                    help="리포트에 남길 기본 모드 라벨(cli|api). 비우면 미지정")
    ap.add_argument("--no-insight", action="store_true",
                    help="AI 분석을 만들지 않는다(LLM 비용 0)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    try:
        days = (days_back(args.backfill) if args.backfill > 0
                else [resolve_day(args.day)])
    except ValueError:
        print(f"날짜 형식이 잘못됐습니다: {args.day!r} (YYYY-MM-DD)", file=sys.stderr)
        return 2

    failed = 0
    for day in days:
        try:
            res = snapshot(args.upstream, day, llm_mode=args.llm_mode,
                           with_insight=not args.no_insight, timeout=args.timeout)
        except Exception as e:
            # 크론은 조용히 실패하기 쉽다 — stderr + 비0 종료로 확실히 드러낸다.
            print(f"[report_snapshot] {day} 실패({args.upstream}): {e}", file=sys.stderr)
            failed += 1
            continue

        if not res.get("ok"):
            print(f"[report_snapshot] {day} 실패: {res.get('error')}", file=sys.stderr)
            failed += 1
            continue

        note = ""
        if not args.no_insight:
            note = (" · 분석 저장" if res.get("insight_stored")
                    else f" · 분석 실패({res.get('insight_error', '')[:60]})")
        print(f"[report_snapshot] {day}  턴 {res.get('turns', 0):,}  "
              f"${res.get('usd', 0):,.2f}{note}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
