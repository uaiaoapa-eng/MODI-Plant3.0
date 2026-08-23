"""자동화 chat 시뮬레이션 — 전체(quick)/과정(design) 두 모드를 여러 번 돌려 결과 노출·재사용 검증(#44).

기존 코퍼스(실 data/chunk_meta.json = base 청크)를 그대로 쓰고, 되먹임 등록 스토어는
temp 복사본으로 격리해 실데이터를 오염시키지 않는다(반복 실행 안전).

- 전체(quick):  바로 빌드 → generated_code 가 나와야 PASS. 같은 요청을 반복하면 2회차부터
                재사용(done.reused)이 붙는지(플라이휠) 관찰.
- 과정(design): 설계 대화 턴 → done(설계 응답)이 나와야 PASS.

LLM 은 claude CLI(로그인 구독) 사용(USE_LOCAL_CLAUDE=true). 실제 응답이라 빌드 턴은 수십초.

실행:
  make sim                                  # 두 모드 1회씩
  make sim SIM_ARGS="--mode quick --runs 3" # 전체 3회(재사용 플라이휠 관찰)
  PYTHONPATH=scripts python scripts/sim_chat_flow.py --mode both --runs 2
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _setup_isolation():
    """실 base 코퍼스는 읽기전용으로 쓰고, 등록 스토어만 temp 복사본으로 격리."""
    os.environ.setdefault("USE_LOCAL_CLAUDE", "true")
    os.environ.pop("RAG_BACKEND", None)  # 로컬(sqlite+npy) 경로 — MySQL/Redis 불필요
    sys.path.insert(0, BASE)
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    os.chdir(BASE)
    import registry_lib as R
    tmp = tempfile.mkdtemp(prefix="sim_reg_")
    real_jsonl = os.path.join(BASE, "data", "registered.jsonl")
    real_emb = os.path.join(BASE, "data", "registered_emb.npy")
    R.REG_JSONL = os.path.join(tmp, "registered.jsonl")
    R.REG_EMB = os.path.join(tmp, "registered_emb.npy")
    if os.path.exists(real_jsonl):  # 기존 등록물도 포함해 검색(기존 DB 데이터 사용)
        shutil.copy(real_jsonl, R.REG_JSONL)
        if os.path.exists(real_emb):
            shutil.copy(real_emb, R.REG_EMB)
    R._meta = R._emb = R._seen = None
    return R, tmp


# 두 모드 대표 질문(교육 도메인). 필요 시 --question 로 덮어씀.
Q_BUILD = "좋아요 버튼을 누르면 하트가 빨개지는 리액트 컴포넌트 만들어줘"
Q_DESIGN = "우주를 배경으로 별을 모으는 간단한 클릭 게임을 만들고 싶어요"


def _turn(client, session_id, message, mode, coding_type, uid):
    seq, done, err, tok = [], None, None, 0
    t0 = time.time()
    body = {"session_id": session_id, "message": message, "mode": mode, "coding_type": coding_type}
    try:
        with client.stream("POST", f"/chat?user_id={uid}", json=body) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                s = line[5:].strip() if line.startswith("data:") else line
                try:
                    ev = json.loads(s)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "token":
                    tok += 1
                    continue
                seq.append(t)
                if t == "error":
                    err = ev.get("message")
                if t == "done":
                    done = ev
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {"events": [k for k, _ in itertools.groupby(seq)], "done": done,
            "err": err, "tokens": tok, "dur": round(time.time() - t0, 1)}


def _assess(mode, res):
    """결과가 제대로 노출됐는지 자동 판정 → (ok, 요약)."""
    d = res["done"] or {}
    if res["err"]:
        return False, f"ERROR {res['err'][:60]}"
    if not res["done"]:
        return False, "done 이벤트 없음"
    reused = d.get("reused")
    primed = d.get("ontology_primed")  # #27 온톨로지 프라임 주입 관측
    onto = ((f" ONTOLOGY(concept={primed.get('concept')},prereq={primed.get('prerequisites')},"
             f"artifacts={primed.get('artifacts')})") if primed else " ONTOLOGY=none")
    gc = d.get("generated_code") or {}
    if mode == "quick":  # 전체: 코드가 나와야(또는 재사용) PASS
        ok = bool(gc) or bool(reused)
        tail = f"files={len(gc)}" + (f" REUSED(top1={reused.get('top1')})" if reused else "") + onto
        return ok, tail
    # design 과정: 설계 응답(토큰) 또는 done 이면 PASS
    ok = res["tokens"] > 0 or bool(d.get("design_doc")) or bool(gc)
    return ok, (f"phase={d.get('phase')} tokens={res['tokens']} "
                f"design_doc={'Y' if d.get('design_doc') else 'N'}" + onto)


def _search_only(R, args):
    """LLM 없이 '있는 것 가져오기' 즉시 테스트: 검색 결과 + 재사용 게이트 판정 출력."""
    import search_lib
    from agent import reuse as RU
    q = args.question or Q_BUILD
    ct = args.coding_type
    if args.seed:  # 재사용 히트를 즉시 보려면 질문 목표로 코드 1건 심기
        R.register_result("seed", "seeduser", ct,
                          code_map={"App.tsx": "// " + q}, goal=q)
        R._meta = R._emb = R._seen = None
        print(f"[seed] '{q}' 목표로 code 결과물 1건 등록\n")

    print(f"# 검색-전달 테스트(LLM 없음)  q='{q}'  coding_type={ct}")
    res = search_lib.search(q, coding_type=ct, top=5)
    print(f"  전체 결정={res.get('decision')} top1={res.get('top1_score')} "
          f"벡터={'ON' if res.get('vector',{}).get('enabled') else '폴백(부분일치)'}")
    for r in (res.get("results") or [])[:5]:
        pk = (r.get("payload") or {}).get("kind") or "-"
        print(f"    · [{r.get('decision'):8s}] {str(r.get('title',''))[:34]:34s} "
              f"src={r.get('source'):10s} kind={pk:13s} score={r.get('score')}")
    g = RU.gate(q, "implement_request", ct)
    print("\n  재사용 게이트(빌드 턴 기준):",
          f"{g['decision']} → 코드 재사용 후보 '{g['candidate'].get('title')}'" if g
          else "후보 없음 → 정상 생성(콜드/불일치)")
    print(f"\n(격리 temp: {R.REG_JSONL})")


def _remote_flow(args):
    """배포 서버(실 벡터 BGE-m3 + mysql_redis)에 HTTP 로 직접 호출 = 진짜 시뮬레이션."""
    import httpx
    base = args.base_url.rstrip("/")
    cred = args.auth or os.environ.get("EDU_AUTH", "")
    auth = httpx.BasicAuth(*cred.split(":", 1)) if ":" in cred else None
    uid = "sim-remote-tester"
    q = args.question or Q_BUILD
    print(f"# 원격 시뮬레이션 → {base}  q='{q}'  coding_type={args.coding_type}\n")

    if args.search_only:
        with httpx.Client(timeout=30, auth=auth, verify=True) as c:
            r = c.get(f"{base}/api/search",
                      params={"q": q, "coding_type": args.coding_type, "top": 5, "user_id": uid})
            r.raise_for_status()
            res = r.json()
        print(f"  전체 결정={res.get('decision')} top1={res.get('top1_score')} "
              f"벡터={'ON' if res.get('vector',{}).get('enabled') else '폴백'}")
        for x in (res.get("results") or [])[:5]:
            pk = (x.get("payload") or {}).get("kind") or "-"
            print(f"    · [{x.get('decision'):8s}] {str(x.get('title',''))[:34]:34s} "
                  f"src={x.get('source'):10s} kind={pk:13s} score={x.get('score')}")
        return

    modes = ["quick", "design"] if args.mode == "both" else [args.mode]
    for mode in modes:
        qq = args.question or (Q_BUILD if mode == "quick" else Q_DESIGN)
        for i in range(1, args.runs + 1):
            print(f"[{mode:6s} run {i}] 실행 중… → {base}/chat", flush=True)
            seq, done, tok = [], None, 0
            t0 = time.time()
            body = {"session_id": f"remote-{mode}-{i}", "message": qq,
                    "mode": mode, "coding_type": args.coding_type}
            try:
                with httpx.Client(timeout=600, auth=auth) as c:
                    with c.stream("POST", f"{base}/chat", params={"user_id": uid}, json=body) as r:
                        for line in r.iter_lines():
                            if not line:
                                continue
                            s = line[5:].strip() if line.startswith("data:") else line
                            try:
                                ev = json.loads(s)
                            except Exception:
                                continue
                            if ev.get("type") == "token":
                                tok += 1
                                continue
                            seq.append(ev.get("type"))
                            if ev.get("type") == "done":
                                done = ev
            except Exception as e:
                print(f"  ERROR {type(e).__name__}: {e}")
                continue
            d = done or {}
            gc = d.get("generated_code") or {}
            print(f"  ({round(time.time()-t0,1)}s) events: {' -> '.join(k for k,_ in itertools.groupby(seq))}")
            print(f"    files={len(gc)} reused={d.get('reused')} design_doc={'Y' if d.get('design_doc') else 'N'} tokens={tok}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["quick", "design", "both"], default="both",
                    help="quick=전체(바로빌드) · design=과정(설계) · both")
    ap.add_argument("--runs", type=int, default=1, help="모드별 반복 횟수(재사용 플라이휠 관찰)")
    ap.add_argument("--coding-type", default="react")
    ap.add_argument("--question", default="", help="지정 시 이 질문으로(모드 무관)")
    ap.add_argument("--search-only", action="store_true",
                    help="LLM 없이 검색·재사용 판정만 즉시 출력('있는 것 가져오기' 빠른 테스트)")
    ap.add_argument("--seed", action="store_true",
                    help="--search-only 전에 질문 목표로 샘플 code 결과물 1건 등록(재사용 히트 즉시 확인)")
    ap.add_argument("--base-url", default="",
                    help="배포 서버 URL(예: https://edu-agent.luxrobo.net). 지정 시 in-process 대신 "
                         "실 서버(벡터 BGE-m3+mysql_redis)에 HTTP 호출 = 진짜 시뮬레이션")
    ap.add_argument("--auth", default="", help="NPM 기본인증 user:pass (또는 env EDU_AUTH)")
    args = ap.parse_args()

    # 원격(배포 서버) 경로 — 로컬 격리/TestClient 안 씀. 실 벡터 검색으로 동작.
    if args.base_url:
        _remote_flow(args)
        return

    R, tmp = _setup_isolation()

    if args.search_only:
        _search_only(R, args)
        return
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    uid = "sim-throwaway-user"

    modes = ["quick", "design"] if args.mode == "both" else [args.mode]
    rows = []
    print(f"# 자동화 시뮬레이션  modes={modes} runs={args.runs} coding_type={args.coding_type}")
    print(f"# 기존 등록물 {R.count()}건 포함(격리 temp: {tmp})\n")

    for mode in modes:
        q = args.question or (Q_BUILD if mode == "quick" else Q_DESIGN)
        for i in range(1, args.runs + 1):
            sid = f"sim-{mode}-{i}"  # 매 run 새 세션(=새 빌드, 수정 아님)
            # 빌드 턴은 실제 LLM 생성이라 수십초 — 멈춘 걸로 보이지 않게 진행 표시.
            print(f"[{mode:6s} run {i}] 실행 중… (빌드는 수십초 소요)", flush=True)
            res = _turn(client, sid, q, mode, args.coding_type, uid)
            ok, summary = _assess(mode, res)
            rows.append((mode, i, ok, res["dur"], summary))
            print(f"[{mode:6s} run {i}] {'PASS' if ok else 'FAIL'} ({res['dur']}s)  {summary}")
            print(f"          events: {' -> '.join(res['events'])}")
            R._meta = R._emb = R._seen = None  # 다음 검색이 방금 등록분 반영

    _, meta = R.assets()
    kinds = collections.Counter((m.get("payload") or {}).get("kind") or m.get("chunk_type") for m in meta)
    passed = sum(1 for r in rows if r[2])
    print(f"\n{'='*64}")
    print(f"결과: {passed}/{len(rows)} PASS - 등록 스토어 {len(meta)}건 {dict(kinds)}")
    print("(격리 temp 사용 - 실 data/registered.jsonl 은 변경되지 않음)")
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
