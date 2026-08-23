"""가상 시뮬레이션 러너 — /api/simulate 를 시나리오 묶음으로 자동 반복 실행해 문제점·개선여지 리포트.

'시뮬레이션'의 의미: LLM 없이(비용 0) 여러 사용자 발화를 가상으로 흘려 /chat 의 온톨로지 RAG
분기·프라임을 점검하고, 다음 4축으로 인사이트를 낸다.
  1) 정확성(accuracy): 분기(code_action) 정확·프라임 주입·그래프 완전성
  2) 속도(speed)     : 호출 지연(avg/p90)
  3) 비용/턴(cost)   : 재사용(reuse/review) 가능 비율 → 전체 재생성 회피 = 출력토큰/턴 절감 여지
  4) 데이터 충분성   : 재사용률 vs 콜드(register)률 → 저장 코퍼스가 질의를 덮는지(더 필요한지)

사용:
  python scripts/simulate_batch.py https://edu-agent.luxrobo.net
  python scripts/simulate_batch.py https://edu-agent.luxrobo.net --json report.json
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

# (message, mode, coding_type, phase, has_code, expect_code_action, note)
SCENARIOS = [
    ("자동차가 벽 보고 스스로 멈추게", "quick", "blockly", "implement", False, True, "거리감지"),
    ("변수로 점수를 기억하게 해줘", "quick", "blockly", "implement", False, True, "변수"),
    ("버튼 누르면 LED 색 바뀌게", "quick", "blockly", "implement", False, True, "버튼/LED"),
    ("깜깜해지면 불이 켜지는 장치", "quick", "blockly", "implement", False, True, "밝기감지"),
    ("두더지가 랜덤한 곳에서 튀어나오게", "quick", "react", "implement", False, True, "랜덤(react)"),
    ("좋아요 버튼 누르면 하트 색 바뀌게", "quick", "react", "implement", False, True, "버튼(react)"),
    ("모터 속도를 다이얼로 조절", "quick", "blockly", "implement", False, True, "모터/다이얼"),
    ("기울기 감지해서 방향 바꾸기", "quick", "blockly", "implement", False, True, "IMU"),
    # 커버리지 프로브 — 아이들이 흔히 요청하는 하드웨어/개념. 그래프가 비면 온톨로지 갭(보강 후보).
    ("소리로 알람 울리게", "quick", "blockly", "implement", False, True, "부저/소리"),
    ("서보모터를 90도 돌려줘", "quick", "blockly", "implement", False, True, "서보"),
    ("온도가 높으면 경고하게", "quick", "blockly", "implement", False, True, "온도센서"),
    ("3초마다 깜빡이게 반복", "quick", "blockly", "implement", False, True, "타이머/반복"),
    ("초음파 센서로 거리 재기", "quick", "blockly", "implement", False, True, "초음파"),
    ("소리 크기에 반응하게", "quick", "blockly", "implement", False, True, "소리센서"),
    ("조이스틱으로 캐릭터 움직이기", "quick", "react", "implement", False, True, "조이스틱(react)"),
    ("화면에 점수를 표시하게", "quick", "react", "implement", False, True, "화면표시(react)"),
    ("색을 빨강으로 바꿔줘", "quick", "react", "implement", True, True, "수정 턴"),
    ("점수판 기능 추가해줘", "quick", "react", "implement", True, True, "기능추가→수정"),
    ("고마워", "quick", "react", "implement", True, False, "잡담"),
    ("이거 어떻게 동작해?", "quick", "react", "implement", True, False, "질문"),
    ("센서로 거리 재는 앱 만들고 싶어", "design", "blockly", "design", False, False, "설계 대화"),
    ("안녕 반가워 오늘 기분 좋다", "quick", "react", "implement", True, False, "무관 발화"),
]


def _post(base: str, path: str, payload: dict) -> tuple[dict, float]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.load(r)
    return d, (time.perf_counter() - t0) * 1000.0


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=25) as r:
        return json.load(r)


def evaluate(sc: tuple, d: dict) -> dict:
    _msg, _mode, _ct, _ph, _hc, expect_ca, note = sc
    flags: list[str] = []
    ca = bool(d.get("code_action"))
    o = d.get("ontology") or {}
    n = {k: len(o.get(k) or []) for k in ("prerequisites", "related", "modi_modules", "cards", "artifacts")}
    gate = d.get("reuse_gate") or {}
    decision = gate.get("decision") or ("register" if ca else "-")

    status = "PASS"
    if ca != expect_ca:
        flags.append(f"분기오류 code_action={ca}(기대 {expect_ca})")
        status = "FAIL"
    if ca:
        if not d.get("injected"):
            flags.append("프라임 미주입")
            status = "FAIL"
        # 수정 턴(modify_request)은 무거운 온톨로지 그래프 프라임을 의도적으로 건너뛴다
        # (#68 O3 / EDU-27 설계: 기존 산출물을 편집 유도). 따라서 그래프가 비는 게 정상이며
        # 경고 대상이 아니다. 콜드 신규 구현(implement_request)의 그래프 빔만 개념매칭 갭으로 경고.
        is_modify = d.get("intent") == "modify_request"
        graph_total = n["prerequisites"] + n["related"] + n["modi_modules"] + n["cards"]
        if is_modify:
            pass  # 수정 턴: 그래프 희소는 설계상 정상 → 과경고 억제
        elif graph_total == 0:
            flags.append("온톨로지 그래프 전부 빔(선수학습/관련/MODI/카드=0)")
            if status != "FAIL":
                status = "WARN"
        else:
            if n["modi_modules"] == 0:
                flags.append("MODI 없음")
                status = "WARN" if status == "PASS" else status
            if n["cards"] == 0:
                flags.append("카드 없음")
                status = "WARN" if status == "PASS" else status
    return {"status": status, "flags": flags, "counts": n, "intent": d.get("intent"),
            "code_action": ca, "decision": decision, "note": note}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = args[0] if args else "http://localhost:18080"
    json_out = None
    if "--json" in sys.argv:
        i = sys.argv.index("--json")
        json_out = sys.argv[i + 1] if i + 1 < len(sys.argv) else "simulate_report.json"

    print(f"# 가상 시뮬레이션 — {base}  ({len(SCENARIOS)} 시나리오)\n")

    # 데이터 충분성 신호: 표준 질의묶음의 재사용/근접/등록 분포(코퍼스가 질의를 덮는지).
    coverage = None
    try:
        coverage = _get(base, "/api/coverage?coding_type=blockly")
    except Exception as e:
        print(f"(coverage 조회 실패: {str(e)[:60]})")

    rows, lat = [], []
    tally = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
    dec = {"reuse": 0, "review": 0, "register": 0}
    code_turns = 0
    for sc in SCENARIOS:
        try:
            d, ms = _post(base, "/api/simulate", {"message": sc[0], "mode": sc[1], "coding_type": sc[2],
                                                  "phase": sc[3], "has_code": sc[4]})
            ev = evaluate(sc, d)
            lat.append(ms)
        except Exception as e:
            ev = {"status": "ERROR", "flags": [f"호출실패 {str(e)[:60]}"], "counts": {},
                  "intent": None, "code_action": None, "decision": "-", "note": sc[6]}
        tally[ev["status"]] += 1
        if ev["code_action"]:
            code_turns += 1
            if ev["decision"] in dec:
                dec[ev["decision"]] += 1
        rows.append({"message": sc[0], "latency_ms": round(lat[-1], 1) if lat and ev["status"] != "ERROR" else None, **ev})
        mark = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "ERROR": "💥"}[ev["status"]]
        c = ev["counts"]
        cnt = (f"pre={c.get('prerequisites',0)} rel={c.get('related',0)} modi={c.get('modi_modules',0)} "
               f"card={c.get('cards',0)} art={c.get('artifacts',0)} dec={ev['decision']}") if c else ""
        print(f"{mark} [{ev['status']:4}] {sc[0][:30]:30} | {str(ev['intent']):18} | {cnt}")
        for f in ev["flags"]:
            print(f"        └ {f}")

    # ── 인사이트 ──────────────────────────────────────────────
    print("\n" + "=" * 64)
    print(f"정확성: PASS {tally['PASS']}  WARN {tally['WARN']}  FAIL {tally['FAIL']}  ERROR {tally['ERROR']} / {len(SCENARIOS)}")
    if lat:
        s = sorted(lat)
        p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
        print(f"속도: avg {sum(lat)/len(lat):.0f}ms  p90 {p90:.0f}ms  (n={len(lat)})")
    reuse_eligible = dec["reuse"] + dec["review"]
    if code_turns:
        print(f"비용/턴 절감 여지: 코드턴 {code_turns}건 중 재사용가능(reuse+review) {reuse_eligible}건 "
              f"= {reuse_eligible/code_turns*100:.0f}%  (register/콜드 {dec['register']}건)")
        print("   → 재사용가능 비율이 높을수록 전체 재생성을 피해 출력토큰·턴을 줄일 수 있음.")

    # 온톨로지 커버리지 갭: 신규구현(비수정) 턴인데 그래프가 전부 빈 발화 = 개념매핑 미보유.
    # 실트래픽 없이도 "어떤 표현을 온톨로지가 못 덮는지" 를 구체 리스트로 뽑아 보강 후보를 준다.
    def _empty_graph(r: dict) -> bool:
        c = r.get("counts") or {}
        return sum(c.get(k, 0) for k in ("prerequisites", "related", "modi_modules", "cards")) == 0
    build_turns = [r for r in rows if r.get("code_action") and r.get("intent") != "modify_request"]
    gaps = [r for r in build_turns if _empty_graph(r)]
    if build_turns:
        print(f"온톨로지 커버리지: 신규구현 {len(build_turns)}턴 중 그래프 채워짐 "
              f"{len(build_turns) - len(gaps)} / 빔 {len(gaps)}")
        if gaps:
            print("   → 개념매핑 보강 후보(온톨로지가 못 덮는 발화):")
            for g in gaps:
                print(f"      • {g['message']}  ({g.get('note','')})")

    if coverage and coverage.get("ok"):
        pct = coverage.get("percent", {})
        rr = coverage.get("reuse_rate")
        print(f"데이터 충분성(표준 질의묶음 {coverage.get('total')}건): "
              f"reuse {pct.get('reuse',0):.0f}% / review {pct.get('review',0):.0f}% / register {pct.get('register',0):.0f}%"
              f"  (reuse_rate {rr}%)")
        reg = pct.get("register", 0)
        if reg >= 50:
            print("   → 콜드(register) 비율이 높음 = 저장 코퍼스가 질의를 못 덮음 → 데이터 더 필요.")
        elif reg <= 20:
            print("   → 콜드 비율이 낮음 = 코퍼스가 질의를 잘 덮음 → 현재 데이터로 대체로 충분.")
        else:
            print("   → 중간 = 자주 쓰는 개념 위주로 보강하면 재사용률이 더 오를 여지.")

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump({"base": base, "tally": tally, "latency_ms": lat, "decisions": dec,
                       "code_turns": code_turns, "coverage": coverage, "rows": rows},
                      f, ensure_ascii=False, indent=2)
        print(f"\nJSON 저장: {json_out}")


if __name__ == "__main__":
    main()
