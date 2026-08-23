"""검색 매칭 품질 eval — search_lib.search() 의 개념 정확도 + 점수 분포.

gold 패러프레이즈 8건에 대해:
  - primary_concept(centroid 최근접) 정확도
  - top 결과 청크의 concept 정확도
  - top1 점수 분포(재사용/근접/등록 임계값 캘리브레이션 참고)

실행:  PYTHONPATH=scripts python scripts/eval_search.py                 # local 백엔드
       PYTHONPATH=scripts python scripts/eval_search.py --backend mysql_redis
       PYTHONPATH=scripts python scripts/eval_search.py --sweep         # #67 T4 TAU 스윕
       (또는 RAG_BACKEND=mysql_redis VECTOR_REDIS_URL=... 환경변수로도 지정)

온프렘 Redis 백엔드로 돌리려면 backfill(개념 centroid 포함)이 선행돼야 한다(#38).

#67 T4 — TAU 튜닝: `--sweep` 은 TAU_REUSE 후보별 **reuse precision** 곡선을 출력한다.
검색 비용은 ≈0 이라 트레이드오프는 비용이 아니라 품질이므로, 히트율이 아니라 정밀도로
TAU 를 고른다. 콜드(재사용 대상 없음) 질문을 섞어 false positive 를 드러낸다. 실운영
튜닝은 T3(Langfuse 재사용 코호트) 데이터로 gold 세트를 키운 뒤 이 스윕으로 확정한다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import search_lib  # noqa: E402
from search_lib import search  # noqa: E402

GOLD = [
    ("화면 여러 개를 복사해서 똑같이 쓰고 싶어요", "component_reuse"),
    ("로봇이 앞에 장애물 있으면 알아서 서게", "distance_sensing"),
    ("빙글빙글 돌리는 손잡이로 숫자 바꾸기", "dial_input"),
    ("스마트폰마다 게임 빠르기가 달라져요", "frame_independence"),
    ("스크롤 내려도 위 메뉴는 그대로 붙어있게", "sticky_header"),
    ("좋아요 누르면 하트 빨개지게", "button_input"),
    ("깜깜해지면 불이 켜지는 장치", "env_sensor"),
    ("숫자를 천 단위 콤마로 예쁘게", "number_format"),
]


def run() -> dict:
    pc_hit = top_hit = 0
    rows = []
    for q, gold in GOLD:
        r = search(q, top=1)
        pc = (r.get("primary_concept") or {}).get("key")
        tk = r["results"][0]["concept_key"] if r["results"] else None
        pc_ok, top_ok = pc == gold, tk == gold
        pc_hit += pc_ok
        top_hit += top_ok
        rows.append((q, gold, pc, pc_ok, tk, top_ok, r.get("top1_score"),
                     (r.get("primary_concept") or {}).get("conf")))
    return {"n": len(GOLD), "primary_concept_hits": pc_hit, "top_chunk_hits": top_hit, "rows": rows}


# ── #67 T4: TAU 오프라인 스윕 (reuse precision 곡선) ─────────────────────────────
# 핵심 원칙(이슈): 검색 비용 ≈0 → 트레이드오프는 비용이 아니라 **품질**. 그래서 히트율이
# 아니라 **정밀도(precision)** 로 TAU 를 고른다. 정밀도 = 게이트가 "reuse" 로 판정한 것 중
# 실제로 맞는(같은 개념) 비율. 잘못된 재사용은 학습자에게 엉뚱한 결과물을 준다.
#
# 콜드(gold=None) 질문을 섞어야 정밀도가 의미를 갖는다 — 재사용 대상이 없는 질문이 높은
# TAU 도 넘겨버리면 그게 false positive(잘못된 재사용)다.
SWEEP_GOLD = GOLD + [
    ("오늘 점심 뭐 먹을지 골라주는 룰렛", None),
    ("주식 실시간 시세 차트 보여줘", None),
    ("여행 경비를 친구들끼리 나눠 정산", None),
]


def _sweep_samples(gold: list, search_fn) -> list[dict]:
    """각 질문의 top1 점수와 '정답 매치 여부'를 1회 검색으로 계산(스윕은 이 표본을 재사용)."""
    samples = []
    for q, expected in gold:
        r = search_fn(q, top=1)
        score = r.get("top1_score") or 0.0
        top_concept = r["results"][0]["concept_key"] if r.get("results") else None
        samples.append({
            "q": q, "expected": expected, "score": score,
            "correct": expected is not None and top_concept == expected,  # 재사용해도 되는 매치
            "cold": expected is None,  # 재사용 대상 없음 → 넘기면 false positive
        })
    return samples


def sweep_tau(taus: list[float] | None = None, gold: list | None = None,
              search_fn=None) -> dict:
    """TAU_REUSE 후보별 precision/recall/coverage 를 계산해 곡선으로 반환.

    - precision: reuse 판정(top1≥TAU) 중 정답 매치 비율 → **이 값으로 TAU 를 고른다.**
    - recall:    재사용 가능(비콜드) 질문 중 실제 reuse 된 비율(놓친 재사용 관측용).
    - coverage:  전체 질문 중 reuse 판정 비율.
    search_fn 을 주입하면 백엔드/임베딩 없이 단위테스트 가능(기본은 실제 search).
    """
    taus = taus or [round(x / 100, 2) for x in range(40, 86, 5)]
    gold = gold if gold is not None else SWEEP_GOLD
    samples = _sweep_samples(gold, search_fn or search)
    reusable = sum(1 for x in samples if not x["cold"])
    rows = []
    for tau in taus:
        reused = [x for x in samples if x["score"] >= tau]
        tp = sum(1 for x in reused if x["correct"])
        fp = len(reused) - tp
        rows.append({
            "tau": tau, "reused": len(reused), "tp": tp, "fp": fp,
            "precision": (tp / len(reused)) if reused else None,
            "recall": (tp / reusable) if reusable else None,
            "coverage": len(reused) / len(samples) if samples else 0.0,
        })
    return {"samples": samples, "rows": rows, "n": len(samples), "reusable": reusable}


def _fmt(x) -> str:
    return "  -  " if x is None else f"{x:.2f}"


def print_sweep(res: dict) -> None:
    print(f"\nTAU 스윕 (표본 {res['n']}, 재사용가능 {res['reusable']}, 콜드 "
          f"{res['n'] - res['reusable']})  — **precision 기준으로 TAU 선택**")
    print(f"{'TAU':>5} {'reuse':>6} {'TP':>3} {'FP':>3} {'precision':>10} "
          f"{'recall':>8} {'coverage':>9}")
    print("-" * 52)
    for r in res["rows"]:
        print(f"{r['tau']:>5.2f} {r['reused']:>6} {r['tp']:>3} {r['fp']:>3} "
              f"{_fmt(r['precision']):>10} {_fmt(r['recall']):>8} {_fmt(r['coverage']):>9}")
    # 정밀도 1.0 을 유지하는 가장 낮은 TAU = 안전하게 재사용을 최대화하는 지점.
    clean = [r for r in res["rows"] if r["precision"] == 1.0 and r["reused"]]
    if clean:
        best = min(clean, key=lambda r: r["tau"])
        print(f"\n제안: precision=1.0 을 지키는 최저 TAU_REUSE ≈ {best['tau']:.2f} "
              f"(reuse {best['reused']}건, recall {_fmt(best['recall'])}). "
              f"현재 기본값 {search_lib.TAU_REUSE}.")
    else:
        print(f"\n제안: 표본에서 precision=1.0 구간 없음 — gold 세트를 늘리거나 "
              f"임베딩/백엔드를 점검하세요. 현재 기본값 {search_lib.TAU_REUSE}.")


def main() -> None:
    if "--backend" in sys.argv:  # local | mysql_redis — 모듈 상수 오버라이드(search 가 매 호출 참조)
        search_lib.RAG_BACKEND = sys.argv[sys.argv.index("--backend") + 1]
    print(f"backend={search_lib.RAG_BACKEND}")
    if "--sweep" in sys.argv:  # #67 T4: TAU precision 스윕만 돌리고 종료
        print_sweep(sweep_tau())
        return
    res = run()
    print(f"{'질문':30s} {'정답':16s} {'primary_concept':18s}  top1  conf")
    print("-" * 92)
    for q, gold, pc, pc_ok, tk, top_ok, s, conf in res["rows"]:
        mark = "✅" if pc_ok else "❌"
        print(f"{q[:28]:30s} {gold:16s} {mark}{str(pc)[:16]:17s}  {s:<5} {conf}")
    print("-" * 92)
    print(f"정확도 → primary_concept {res['primary_concept_hits']}/{res['n']}"
          f"   top청크 concept {res['top_chunk_hits']}/{res['n']}")


if __name__ == "__main__":
    main()
