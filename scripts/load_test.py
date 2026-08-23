#!/usr/bin/env python3
"""edu-agent 동시성/부하 검증 스크립트 — 세션별 실제 LLM 응답을 확인한다.

표준 라이브러리만 사용(추가 의존 없음). 라이브 서버 + Claude 인증이 필요하므로
CI 가 아니라 운영/스테이징에서 수동 실행한다.

목적
  - 여러 세션을 동시에 띄워 각 세션이 실제로 LLM 토큰을 끝까지 받는지 검증
  - 같은 세션 동시 요청 시 #1 가드(busy 거절)가 정확히 1개만 통과시키는지 검증
  - concurrency 를 점증(ramp)해 "limit 에 걸리는 지점"을 자동 탐지(=현재 CLI 구독 한계 측정)

사용 예
  # 서로 다른 20개 세션 동시(다중 사용자 처리량/성공률)
  python scripts/load_test.py --url http://localhost:18080 --mode distinct --sessions 20

  # 같은 세션 5개 동시 → 1개만 성공, 4개 busy 여야 정상(#1 검증)
  python scripts/load_test.py --mode same-session --concurrency 5

  # 세션당 3턴 이어서(세션 상태 유지 + 연속 LLM 동작 확인)
  python scripts/load_test.py --mode sustained --sessions 10 --turns 3

  # 동시성을 5→10→20… 늘려가며 한계 탐지(성공률 < threshold 에서 정지)
  python scripts/load_test.py --mode ramp --ramp-steps 5,10,20,40 --ramp-threshold 0.9

  # 스레드 대신 개별 프로세스로(GIL/스레드풀 영향 배제)
  python scripts/load_test.py --mode distinct --sessions 20 --proc
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed


def run_one_turn(base_url: str, session_id: str, message: str, user_id: str, timeout: float) -> dict:
    """/chat 에 1턴 요청하고 SSE 를 끝까지 읽어 세션별 결과를 돌려준다.

    성공 기준: HTTP 200 + token 이벤트 1회 이상 + done 도달 + error 이벤트 없음.
    반환 outcome: success | busy | connection_refused | non_200 | timeout | error_event | no_tokens
    """
    url = f"{base_url.rstrip('/')}/chat?user_id={urllib.parse.quote(user_id)}"
    body = json.dumps({"session_id": session_id, "message": message}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    started = time.time()
    first_token_at: float | None = None
    tokens = 0
    saw_done = False
    error_msg = ""

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return _result(session_id, "non_200", tokens, started, None, f"HTTP {resp.status}")
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "token":
                    tokens += 1
                    if first_token_at is None:
                        first_token_at = time.time()
                elif etype == "error":
                    error_msg = event.get("message", "error")
                elif etype == "done":
                    saw_done = True
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "refused" in reason.lower():
            return _result(session_id, "connection_refused", tokens, started, None, reason)
        if "timed out" in reason.lower():
            return _result(session_id, "timeout", tokens, started, None, reason)
        return _result(session_id, "connection_refused", tokens, started, None, reason)
    except TimeoutError as e:
        return _result(session_id, "timeout", tokens, started, None, str(e))
    except Exception as e:  # noqa: BLE001
        return _result(session_id, "connection_refused", tokens, started, None, str(e))

    if error_msg:
        # busy 가드 메시지인지 일반 에러인지 구분(가드 검증에 사용)
        outcome = "busy" if "처리 중" in error_msg else "error_event"
        return _result(session_id, outcome, tokens, started, first_token_at, error_msg)
    if tokens == 0:
        return _result(session_id, "no_tokens", tokens, started, first_token_at, "done이지만 토큰 없음" if saw_done else "토큰/done 없음")
    return _result(session_id, "success", tokens, started, first_token_at, "")


def _result(session_id, outcome, tokens, started, first_token_at, error):
    return {
        "session_id": session_id,
        "outcome": outcome,
        "ok": outcome == "success",
        "tokens": tokens,
        "first_token_ms": round((first_token_at - started) * 1000) if first_token_at else None,
        "total_ms": round((time.time() - started) * 1000),
        "error": error,
    }


def run_sustained(base_url: str, session_id: str, message: str, user_id: str, timeout: float, turns: int) -> dict:
    """한 세션에서 turns 회 '이어서' 대화. 모든 턴 성공해야 세션 성공."""
    last = None
    for i in range(turns):
        last = run_one_turn(base_url, session_id, f"{message} (turn {i + 1})", user_id, timeout)
        if not last["ok"]:
            last["error"] = f"turn {i + 1} 실패: {last['error']}"
            return last
    return last or _result(session_id, "no_tokens", 0, time.time(), None, "no turns")


def _percentile(values: list[int], pct: float) -> int | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round((pct / 100) * (len(vals) - 1)))))
    return vals[k]


def _report(results: list[dict]) -> dict:
    by_outcome: dict[str, int] = {}
    for r in results:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    successes = [r for r in results if r["ok"]]
    total = len(results)
    success_rate = (len(successes) / total) if total else 0.0
    return {
        "total": total,
        "success": len(successes),
        "success_rate": round(success_rate, 4),
        "busy_guard_hits": by_outcome.get("busy", 0),
        "by_outcome": by_outcome,
        "first_token_p50_ms": _percentile([r["first_token_ms"] for r in successes], 50),
        "first_token_p95_ms": _percentile([r["first_token_ms"] for r in successes], 95),
        "total_p50_ms": _percentile([r["total_ms"] for r in successes], 50),
        "total_p95_ms": _percentile([r["total_ms"] for r in successes], 95),
    }


def _print_report(title: str, rep: dict, verbose_results: list[dict] | None = None):
    print(f"\n=== {title} ===")
    print(f"  success: {rep['success']}/{rep['total']} ({rep['success_rate'] * 100:.1f}%)   busy-guard hits: {rep['busy_guard_hits']}")
    fails = {k: v for k, v in rep["by_outcome"].items() if k not in ("success", "busy")}
    if fails:
        print(f"  fail breakdown: {fails}")
    print(f"  first-token  p50={rep['first_token_p50_ms']}ms  p95={rep['first_token_p95_ms']}ms")
    print(f"  total        p50={rep['total_p50_ms']}ms  p95={rep['total_p95_ms']}ms")
    if verbose_results:
        for r in verbose_results:
            mark = "✅" if r["ok"] else "❌"
            print(f"    {mark} {r['session_id']:<28} {r['outcome']:<18} tokens={r['tokens']} total={r['total_ms']}ms {r['error']}")


def _execute(tasks: list[tuple], use_proc: bool, max_workers: int) -> list[dict]:
    """tasks: (fn, args...) 목록을 스레드/프로세스 풀로 실행."""
    Pool = ProcessPoolExecutor if use_proc else ThreadPoolExecutor
    results: list[dict] = []
    with Pool(max_workers=max_workers) as ex:
        futures = [ex.submit(fn, *args) for (fn, *args) in tasks]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def main():
    ap = argparse.ArgumentParser(description="edu-agent 동시성/부하 검증")
    ap.add_argument("--url", default="http://localhost:18080")
    ap.add_argument("--mode", choices=["distinct", "same-session", "sustained", "ramp"], default="distinct")
    ap.add_argument("--sessions", type=int, default=10, help="distinct/sustained: 세션 수")
    ap.add_argument("--concurrency", type=int, default=0, help="동시 실행 수(0이면 sessions 와 동일)")
    ap.add_argument("--turns", type=int, default=1, help="sustained: 세션당 턴 수")
    ap.add_argument("--message", default="간단한 할 일 목록 앱 만들어줘")
    ap.add_argument("--timeout", type=float, default=300.0, help="요청당 SSE 타임아웃(초)")
    ap.add_argument("--proc", action="store_true", help="스레드 대신 개별 프로세스로 실행")
    ap.add_argument("--ramp-steps", default="5,10,20,40", help="ramp: 동시성 단계(쉼표구분)")
    ap.add_argument("--ramp-threshold", type=float, default=0.9, help="ramp: 이 성공률 미만이면 한계로 보고 정지")
    args = ap.parse_args()

    ts = int(time.time())
    user = f"loadtest_{ts}"

    if args.mode == "same-session":
        n = args.concurrency or 5
        sid = f"loadtest_same_{ts}"
        tasks = [(run_one_turn, args.url, sid, args.message, f"{user}_{i}", args.timeout) for i in range(n)]
        results = _execute(tasks, args.proc, n)
        rep = _report(results)
        _print_report(f"same-session (동시 {n}개, 같은 세션)", rep, results)
        ok = rep["success"] == 1 and rep["busy_guard_hits"] == n - 1
        print(f"\n#1 가드 판정: {'✅ PASS (1개만 통과, 나머지 busy)' if ok else '❌ FAIL — 1개만 통과해야 함'}")
        sys.exit(0 if ok else 1)

    if args.mode == "sustained":
        n = args.sessions
        conc = args.concurrency or n
        tasks = [(run_sustained, args.url, f"loadtest_{ts}_{i}", args.message, f"{user}_{i}", args.timeout, args.turns) for i in range(n)]
        results = _execute(tasks, args.proc, conc)
        _print_report(f"sustained ({n}세션 × {args.turns}턴, 동시 {conc})", _report(results), results)
        sys.exit(0)

    if args.mode == "ramp":
        steps = [int(s) for s in args.ramp_steps.split(",") if s.strip()]
        print(f"ramp: 동시성 단계 {steps}, 성공률 임계 {args.ramp_threshold}")
        limit_at = None
        for conc in steps:
            tasks = [(run_one_turn, args.url, f"loadtest_{ts}_{conc}_{i}", args.message, f"{user}_{i}", args.timeout) for i in range(conc)]
            rep = _report(_execute(tasks, args.proc, conc))
            _print_report(f"ramp 동시 {conc}", rep)
            if rep["success_rate"] < args.ramp_threshold:
                limit_at = conc
                print(f"\n⚠️  한계 도달: 동시 {conc} 에서 성공률 {rep['success_rate'] * 100:.1f}% < {args.ramp_threshold * 100:.0f}%")
                break
        if limit_at is None:
            print(f"\n✅ 모든 단계 통과(최대 {steps[-1]} 동시까지 성공률 ≥ {args.ramp_threshold * 100:.0f}%)")
        sys.exit(0)

    # distinct (기본)
    n = args.sessions
    conc = args.concurrency or n
    tasks = [(run_one_turn, args.url, f"loadtest_{ts}_{i}", args.message, f"{user}_{i}", args.timeout) for i in range(n)]
    results = _execute(tasks, args.proc, conc)
    _print_report(f"distinct ({n}세션 동시 {conc}, proc={args.proc})", _report(results), results)
    sys.exit(0)


if __name__ == "__main__":
    main()
