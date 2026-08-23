"""#67 검증 — CLI 모드 비용·시간·품질 before/after 벤치마크.

기존 프로젝트 기반 시나리오(빌드 → 수정 → 수정)를 StreamOrchestrator(CLI 모드)로 돌려
매 LLM(CLI) 호출의 usage(input/output/cache_read/cache_creation)·cost·지연을 기록하고
집계한다. Claude Code 가 --output-format json 으로 돌려주는 usage/total_cost_usd 를 그대로
읽으므로 실제 과금·캐시 수치를 본다.

실행:
  # after(현재 브랜치)
  python scripts/bench_cost.py --label after --out /tmp/after.json
  # before(master): 변경 파일만 stash 후 재실행
  git stash push agent/ && python scripts/bench_cost.py --label before --out /tmp/before.json; git stash pop
  # 비교
  python scripts/bench_cost.py --compare /tmp/before.json /tmp/after.json
  # 자가 점검(LLM 호출 없이 하네스 검증)
  python scripts/bench_cost.py --selftest

주의: --selftest 외에는 실제 LLM 을 호출하므로 비용·시간이 든다. 시나리오는 3턴으로 최소화.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# .env 로드(있으면) — USE_LOCAL_CLAUDE/ANTHROPIC_MODEL 등.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))


def _load_env():
    p = os.path.join(_ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


# CLI 모드 강제(검증 대상). Langfuse 는 껐다(계측 노이즈·비용 무관).
_load_env()
os.environ["USE_LOCAL_CLAUDE"] = "true"
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)


# 기존 프로젝트 시나리오: 빌드 1턴 + 수정 2턴(캐시는 턴 간 재사용을 노림).
SCENARIO = [
    {"input": "간단한 할 일 목록 앱을 만들어줘. 항목 추가하고 체크할 수 있게.", "mode": "quick", "coding_type": "react"},
    {"input": "완료한 항목은 회색 취소선으로 표시해줘.", "mode": "quick", "coding_type": "react"},
    {"input": "화면 위쪽에 남은 할 일 개수를 보여줘.", "mode": "quick", "coding_type": "react"},
]


def _u(usage, key):
    if isinstance(usage, dict):
        return usage.get(key) or 0
    return getattr(usage, key, 0) or 0


def _output_split(msg):
    """#68 O1: 이 응답의 출력 토큰을 종류별(전체재작성/부분수정/산문/기타)로 근사 배분.

    프로덕션의 _llm_call 과 동일한 attribute_output 을 써 벤치에서도 '수정 턴 전체재작성
    낭비'를 오프라인으로 볼 수 있게 한다. content 에서 tool_use 블록과 텍스트를 뽑아 넘긴다.
    """
    from agent.usage import attribute_output

    content = getattr(msg, "content", None) or []
    tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"
                 or (isinstance(b, dict) and b.get("type") == "tool_use")]
    text = "".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "")
                   for b in content if getattr(b, "type", None) == "text"
                   or (isinstance(b, dict) and b.get("type") == "text"))
    return attribute_output(_u(getattr(msg, "usage", None), "output_tokens"), text, tool_uses)


class _Recorder:
    """client.messages 를 감싸 매 호출의 usage/cost/지연을 기록."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def _record(self, msg, dur, kind):
        u = getattr(msg, "usage", None)
        split = _output_split(msg)  # #68 O1: 출력 토큰 종류별 근사
        self.calls.append({
            "kind": kind,
            "input": _u(u, "input_tokens"),
            "output": _u(u, "output_tokens"),
            "output_generate": split["generate_code"],
            "output_edit": split["edit_code"],
            "output_prose": split["prose"],
            "cache_read": _u(u, "cache_read_input_tokens"),
            "cache_creation": _u(u, "cache_creation_input_tokens"),
            "cost": getattr(msg, "cost_usd", None) or 0.0,
            "dur": round(dur, 2),
        })

    def create(self, **kw):
        t = time.time()
        msg = self._inner.create(**kw)
        self._record(msg, time.time() - t, "create")
        return msg

    def stream(self, **kw):
        rec = self
        inner_stream = self._inner.stream(**kw)
        t0 = time.time()

        class _Proxy:
            def __enter__(s):
                inner_stream.__enter__()
                return s

            def __exit__(s, *a):
                return inner_stream.__exit__(*a)

            def __iter__(s):
                return iter(inner_stream)

            def get_final_message(s):
                msg = inner_stream.get_final_message()
                rec._record(msg, time.time() - t0, "stream")
                return msg

            def __getattr__(s, name):
                return getattr(inner_stream, name)

        return _Proxy()


def run_scenario(label: str) -> dict:
    from agent.orchestrator_stream import StreamOrchestrator
    orch = StreamOrchestrator(api_key=os.getenv("ANTHROPIC_API_KEY", ""), session_id=f"bench-{label}")
    rec = _Recorder(orch.client.messages)
    orch.client.messages = rec

    turns = []
    for i, t in enumerate(SCENARIO):
        t0 = time.time()
        call_start = len(rec.calls)  # 이 턴에 속한 호출 슬라이스 시작
        files = None
        for ev in orch.chat_stream(t["input"], mode=t["mode"], coding_type=t["coding_type"]):
            if ev.get("type") == "done":
                files = ev.get("generated_code")
        # #68 O1: 턴 1은 콜드 빌드, 턴 2~ 는 수정 턴 — 수정 턴 전체재작성(output_generate)이
        # 낭비 지표다. 턴 단위로 쪼개 콜드/수정을 섞지 않고 본다.
        turn_calls = rec.calls[call_start:]
        turns.append({
            "turn": i + 1,
            "input": t["input"],
            "is_edit_turn": i > 0,
            "wall_s": round(time.time() - t0, 2),
            "output": sum(c["output"] for c in turn_calls),
            "output_generate": sum(c["output_generate"] for c in turn_calls),
            "output_edit": sum(c["output_edit"] for c in turn_calls),
            "files": sorted((files or {}).keys()),
            "file_count": len(files or {}),
        })

    return _aggregate(label, rec.calls, turns)


def _aggregate(label: str, calls: list, turns: list) -> dict:
    agg = {
        "label": label,
        "model": os.getenv("ANTHROPIC_MODEL", ""),
        "n_calls": len(calls),
        "input": sum(c["input"] for c in calls),
        "output": sum(c["output"] for c in calls),
        "output_generate": sum(c.get("output_generate", 0) for c in calls),
        "output_edit": sum(c.get("output_edit", 0) for c in calls),
        # #68 O1: 수정 턴에서 전체재작성(generate_code)으로 나간 출력 = O2 가 없애려는 낭비.
        "edit_turn_rewrite_output": sum(t.get("output_generate", 0) for t in turns if t.get("is_edit_turn")),
        "cache_read": sum(c["cache_read"] for c in calls),
        "cache_creation": sum(c["cache_creation"] for c in calls),
        "cost_usd": round(sum(c["cost"] for c in calls), 4),
        "wall_s": round(sum(t["wall_s"] for t in turns), 1),
        "turns": turns,
        "calls": calls,
    }
    inp = agg["input"] + agg["cache_read"] + agg["cache_creation"]
    agg["cache_read_ratio"] = round(agg["cache_read"] / inp, 3) if inp else 0.0
    return agg


def print_summary(a: dict):
    print(f"\n[{a['label']}] model={a['model']}")
    print(f"  calls={a['n_calls']}  wall={a['wall_s']}s  cost=${a['cost_usd']}")
    print(f"  tokens: input={a['input']} output={a['output']} "
          f"cache_read={a['cache_read']} cache_creation={a['cache_creation']} "
          f"cache_read_ratio={a['cache_read_ratio']}")
    # #68 O1: 출력 구성 + 수정 턴 전체재작성 낭비(핵심 지표).
    print(f"  output split: generate={a.get('output_generate', 0)} edit={a.get('output_edit', 0)}  "
          f"| 수정턴 전체재작성 output={a.get('edit_turn_rewrite_output', 0)}")
    for t in a["turns"]:
        tag = "수정" if t.get("is_edit_turn") else "빌드"
        extra = (f"  gen={t.get('output_generate', 0)} edit={t.get('output_edit', 0)}"
                 if "output_generate" in t else "")
        print(f"  턴{t['turn']}[{tag}]: {t['wall_s']}s, {t['file_count']}개 파일{extra}  «{t['input'][:30]}»")


def compare(before: dict, after: dict):
    print("\n=== before vs after (CLI 모드) ===")
    print_summary(before)
    print_summary(after)

    def d(k):
        b, a = before.get(k, 0), after.get(k, 0)  # 구버전 JSON 은 신규 키가 없을 수 있음
        if not b:
            return f"{a - b:+}"
        return f"{a - b:+} ({(a - b) / b * 100:+.1f}%)"

    print("\nΔ (after - before):")
    # #68: edit_turn_rewrite_output 이 O2/O3 의 핵심 성공 지표(수정 턴 재작성 낭비 감소).
    for k in ("cost_usd", "wall_s", "output", "output_generate", "edit_turn_rewrite_output", "input", "cache_read"):
        print(f"  {k:24} {d(k)}")
    bf = sum(t["file_count"] for t in before["turns"])
    af = sum(t["file_count"] for t in after["turns"])
    print(f"  files(total)   before={bf} after={af}  (품질: 파일 생성 유지 여부)")


def selftest() -> None:
    """LLM 호출 없이 하네스(레코더/집계) 정확성 검증 — 벤치 전 실수 방지."""
    from agent.claude_client import Message, ToolUseBlock

    class _FakeInner:
        def create(self, **kw):
            return Message(content=[], stop_reason="end_turn",
                           usage={"input_tokens": 10, "output_tokens": 5,
                                  "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2},
                           cost_usd=0.01)

        def stream(self, **kw):
            class _S:
                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False

                def __iter__(s):
                    return iter([])

                def get_final_message(s):
                    # generate_code(전체재작성) tool_use 를 담아 출력 배분(#68 O1)까지 자가검증.
                    return Message(content=[ToolUseBlock(id="t1", name="generate_code",
                                                         input={"code": "x" * 400})],
                                   stop_reason="end_turn",
                                   usage={"input_tokens": 100, "output_tokens": 200,
                                          "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0},
                                   cost_usd=0.05)

            return _S()

    rec = _Recorder(_FakeInner())
    rec.create(model="m", max_tokens=10, system="s", messages=[])
    with rec.stream(model="m", max_tokens=10, system="s", messages=[]) as st:
        list(st)
        st.get_final_message()

    assert len(rec.calls) == 2, rec.calls
    # 스트림 호출의 출력 200 은 전부 generate_code(전체재작성)로 배분되어야 한다(다른 조각 없음).
    assert rec.calls[1]["output_generate"] == 200 and rec.calls[1]["output_edit"] == 0, rec.calls
    turns = [{"turn": 1, "wall_s": 1.0, "file_count": 1, "is_edit_turn": True, "output_generate": 200}]
    agg = _aggregate("selftest", rec.calls, turns)
    assert agg["output_generate"] == 200, agg
    assert agg["edit_turn_rewrite_output"] == 200, agg  # 수정 턴 전체재작성 낭비 롤업
    assert agg["input"] == 110 and agg["output"] == 205, agg
    assert agg["cache_read"] == 43 and agg["cache_creation"] == 2, agg
    assert abs(agg["cost_usd"] - 0.06) < 1e-9, agg
    # cache_read_ratio = 43 / (110 + 43 + 2)
    assert abs(agg["cache_read_ratio"] - round(43 / 155, 3)) < 1e-9, agg
    print("selftest OK — recorder/aggregation 정확. 실제 벤치 준비 완료.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.compare:
        before = json.load(open(args.compare[0]))
        after = json.load(open(args.compare[1]))
        compare(before, after)
        return

    res = run_scenario(args.label)
    print_summary(res)
    if args.out:
        json.dump(res, open(args.out, "w"), ensure_ascii=False, indent=2)
        print(f"\nsaved → {args.out}")


if __name__ == "__main__":
    main()
