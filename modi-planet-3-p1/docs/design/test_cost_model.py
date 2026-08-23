"""
edu-agent 비용 계산기 검증 테스트.
계산기(cost-calculator.html)의 JS 로직을 1:1로 미러링하고,
정면 계산 + 역산(inverse) 라운드트립 + 불변식(invariant)을 assert 로 검증한다.
실행: python3 test_cost_model.py
"""
import math

# ─────────────────────────────────────────────────────────
# 1. JS 모델 미러 (cost-calculator.html 과 동일해야 함)
# ─────────────────────────────────────────────────────────
N_CAT, ZIPF_S, TAU_CELL = 40, 1.0, 60
def _weights():
    r = [1.0/(i**ZIPF_S) for i in range(1, N_CAT+1)]
    t = sum(r); return [x/t for x in r]
CAT_W = _weights()

def cell_hit(n, cell_max):                 # 셀 하나의 hit
    return cell_max*(1-math.exp(-n/TAU_CELL))

def hit_at(C, cell_max_pct=90):            # 전체 hit (바닥부터 합산)
    cm = cell_max_pct/100
    return sum(w*cell_hit(C*w, cm) for w in CAT_W)

def eff(p, use):                           # 턴당 실효 비용
    cache = p['cache'] if use.get('cache') else 0
    loop  = p['loop']  if use.get('loop')  else 0
    h     = p['h']     if use.get('rag')   else 0
    s     = p['s']     if use.get('script')else 0
    Cmiss = p['gen']*(1-cache)*(1-loop)
    lk    = p['lookup'] if use.get('rag') else 0
    non_s = h*(p['adapt']+lk) + (1-h)*(Cmiss+lk)
    return s*p['scost'] + (1-s)*non_s

ALL = {'cache':1,'loop':1,'rag':1,'script':1}
def monthly(turns, p, use=ALL): return turns*eff(p, use)

def base_params(h):
    return dict(gen=0.029, adapt=0.004, scost=0.0015, lookup=0.0005,
                s=0.30, loop=0.20, cache=0.10, h=h)

# ─────────────────────────────────────────────────────────
# 2. 역산 도구: 목표값 → 입력값 (이분탐색)
# ─────────────────────────────────────────────────────────
def invert_corpus_for_hit(target_hit, cell_max_pct=90):
    lo, hi = 0.0, 1e7
    for _ in range(200):
        mid = (lo+hi)/2
        if hit_at(mid, cell_max_pct) < target_hit: lo = mid
        else: hi = mid
    return (lo+hi)/2

def invert_hit_for_monthly(turns, p, target_monthly):
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo+hi)/2
        pc = dict(p); pc['h'] = mid
        # 월비용은 hit 증가 시 감소하므로 방향 반대
        if monthly(turns, pc) > target_monthly: lo = mid
        else: hi = mid
    return (lo+hi)/2

# ─────────────────────────────────────────────────────────
# 3. 테스트
# ─────────────────────────────────────────────────────────
passed = 0
def check(name, cond, detail=""):
    global passed
    assert cond, f"❌ FAIL: {name}  {detail}"
    passed += 1
    print(f"  ✓ {name}  {detail}")

def approx(a, b, tol=1e-6): return abs(a-b) <= tol

print("── A. 구조 불변식 ──")
check("가중치 합 = 1", approx(sum(CAT_W), 1.0), f"sum={sum(CAT_W):.10f}")
check("가중치 단조감소", all(CAT_W[i] >= CAT_W[i+1] for i in range(N_CAT-1)))
check("상위15 트래픽 ~75~80%", 0.72 <= sum(CAT_W[:15]) <= 0.82, f"={sum(CAT_W[:15])*100:.1f}%")

print("\n── B. hit 모델 성질 ──")
check("hit(0)=0", approx(hit_at(0), 0.0))
check("hit 단조증가", all(hit_at(c) < hit_at(c+250) for c in range(0, 10000, 250)))
check("hit 상한(<커버상한)", hit_at(1e7, 90) <= 0.90 + 1e-9, f"limit={hit_at(1e7,90)*100:.2f}%")
check("셀 n=100 → ~0.8×상한", approx(cell_hit(100, 0.9), 0.9*(1-math.exp(-100/60)), 1e-9),
      f"cell(100)={cell_hit(100,0.9)*100:.1f}%")

print("\n── C. 역산 라운드트립 (거꾸로 계산 → 다시 정면) ──")
for tgt in [0.30, 0.50, 0.65, 0.80]:
    C = invert_corpus_for_hit(tgt)
    back = hit_at(C)
    check(f"목표 hit {tgt*100:.0f}% → 코퍼스 {C:,.0f} → 재계산 {back*100:.1f}%",
          approx(back, tgt, 1e-4), f"오차 {abs(back-tgt)*100:.4f}%p")

print("\n── D. 비용 모델 불변식 ──")
p = base_params(hit_at(1500))
turns = 500*40
check("레버 전부 0 → 턴비용 = 생성단가", approx(eff(p, {}), p['gen']),
      f"eff={eff(p,{}):.5f}, gen={p['gen']}")
# 캐시×루프 곱셈 검증
p2 = dict(p, h=0)
Cmiss = eff(p2, {'cache':1,'loop':1})            # rag off → nonScript=Cmiss
check("미스비 = 생성×(1-캐시)×(1-루프)",
      approx(Cmiss, p['gen']*(1-p['cache'])*(1-p['loop'])),
      f"Cmiss={Cmiss:.5f}")
# 레버 단조성 (각 레버 ↑ → 비용 ↓)
for lever in ['cache','loop','s']:
    lo = dict(p); lo[lever] = 0.0
    hi = dict(p); hi[lever] = min(0.5, (p[lever] or 0.2)+0.2)
    check(f"레버 '{lever}' ↑ → 비용 ↓", eff(hi, ALL) < eff(lo, ALL))
check("hit ↑ → 비용 ↓", eff(dict(p,h=0.8), ALL) < eff(dict(p,h=0.2), ALL))

print("\n── E. waterfall(누적 분해) 정합성 ──")
stages = [{}, {'cache':1}, {'cache':1,'loop':1},
          {'cache':1,'loop':1,'rag':1}, {'cache':1,'loop':1,'rag':1,'script':1}]
costs = [monthly(turns, p, u) for u in stages]
costs[0] = turns*p['gen']                          # baseline은 순수 생성
deltas = [costs[i-1]-costs[i] for i in range(1, len(costs))]
check("단계별 절감액 합 = 총 절감액",
      approx(sum(deltas), costs[0]-costs[-1], 1e-9),
      f"Σdelta={sum(deltas):.2f}, base-final={costs[0]-costs[-1]:.2f}")
check("모든 단계 비용 단조감소", all(costs[i] >= costs[i+1] for i in range(len(costs)-1)))

print("\n── F. 역산: 목표 월비용 → 필요 hit ──")
target = 250.0
need_h = invert_hit_for_monthly(turns, p, target)
got = monthly(turns, dict(p, h=need_h))
check(f"월 ${target:.0f} 달성에 필요한 hit = {need_h*100:.1f}% → 재계산 ${got:.1f}",
      approx(got, target, 0.5))

print("\n── G. 대표 시나리오 스냅샷 (회귀 방지) ──")
snap = [(0,0),(500,None),(1500,None),(4000,None),(10000,None)]
print(f"  {'코퍼스':>7} {'hit':>6} {'월(레버전부)':>12} {'baseline':>10} {'절감':>6}")
for C,_ in snap:
    h = hit_at(C); pc = base_params(h)
    m = monthly(turns, pc); b = turns*pc['gen']
    print(f"  {C:>7} {h*100:>5.0f}% {m:>11.0f}$ {b:>9.0f}$ {(1-m/b)*100:>5.0f}%")
check("코퍼스1500 hit 55~62%", 0.55 <= hit_at(1500) <= 0.62, f"={hit_at(1500)*100:.1f}%")
check("코퍼스1500 절감 60~70%", 0.60 <= (1-monthly(turns,base_params(hit_at(1500)))/(turns*0.029)) <= 0.72)

print(f"\n✅ 전체 {passed}개 검증 통과")
