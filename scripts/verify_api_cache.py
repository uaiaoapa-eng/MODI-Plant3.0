"""#67 T1 검증 (API 모드 = 일반 사용자 경로).

이슈의 cache_read 14% 문제는 API 모드에서 시스템 프롬프트 전체를 한 캐시 블록으로 잡아
매 턴 동적 꼬리가 바뀌면 프리픽스가 깨진 것. 이 스크립트는 anthropic SDK 로 직접
before(전체 1블록) vs after(정적/동적 분리) 를 각각 2턴 호출해 2턴째 cache_read 를 비교한다.

정적 프리픽스는 실제 IMPLEMENT 시스템 프롬프트에서 가져오고, 동적 꼬리는 턴마다 다르게
준다(현실: 코드 컨텍스트가 매 턴 바뀜). 작은 호출이라 비용은 몇 센트.

실행: python scripts/verify_api_cache.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


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


_load_env()

import anthropic  # noqa: E402
from agent.prompt_cache import cacheable_system, cacheable_tools, CACHE_BOUNDARY, strip_cache_boundary  # noqa: E402

# Haiku 4.5 단가($/MTok) — 비용 환산용(SDK 는 cost 를 안 주므로 토큰에서 계산).
PRICE = {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25}
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def _cost(u) -> float:
    def g(k):
        return getattr(u, k, 0) or 0
    return (g("input_tokens") * PRICE["input"]
            + g("output_tokens") * PRICE["output"]
            + g("cache_read_input_tokens") * PRICE["cache_read"]
            + g("cache_creation_input_tokens") * PRICE["cache_write"]) / 1e6


def _real_static() -> str:
    os.environ["USE_LOCAL_CLAUDE"] = "true"  # 프롬프트 조립만 — 네트워크 안 씀
    from agent.orchestrator_stream import StreamOrchestrator
    from agent.models import Phase
    from agent.prompt_cache import split_cacheable
    o = StreamOrchestrator(api_key="")
    o._current_mode = "design"
    o._coding_type = "react"
    o.state.project.phase = Phase.IMPLEMENT
    static, _ = split_cacheable(o._get_system_prompt())
    return strip_cache_boundary(static)


def _impl_tools():
    """실제 IMPLEMENT 도구 정의 — 캐시 프리픽스가 Haiku 최소치(2048 tok)를 넘게 하는 핵심."""
    os.environ["USE_LOCAL_CLAUDE"] = "true"
    from agent.tools import get_tools_for_phase
    from agent.models import Phase
    return get_tools_for_phase(Phase.IMPLEMENT, "react")


def _call(client, system_blocks, tools, user):
    t = time.time()
    m = client.messages.create(model=MODEL, max_tokens=16, system=system_blocks,
                               tools=tools, messages=[{"role": "user", "content": user}])
    u = m.usage
    return {
        "dur": round(time.time() - t, 2),
        "input": getattr(u, "input_tokens", 0),
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cost": _cost(u),
    }


def _whole_block(system_str):
    """before(master): 시스템 전체를 한 cache_control 블록으로."""
    return [{"type": "text", "text": system_str, "cache_control": {"type": "ephemeral"}}]


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 — .env 확인")
        return
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    static = _real_static()
    tools = cacheable_tools(_impl_tools())  # 실제 _llm_call 처럼 도구도 캐시
    print(f"model={MODEL}  정적 프리픽스 {len(static)} chars (~{len(static)//3} tok)  도구 {len(tools)}개\n")

    # 동적 꼬리는 턴마다 다르게(현실: 코드 컨텍스트가 매 턴 바뀜).
    dynA = "\n\n## 현재 코드\n" + ("// App.tsx 버전 A\n" * 40)
    dynB = "\n\n## 현재 코드\n" + ("// App.tsx 버전 B 완전히 다름\n" * 40)

    print("=== BEFORE (master): 시스템 전체를 한 캐시 블록(+도구 캐시) ===")
    b1 = _call(client, _whole_block(static + dynA), tools, "1턴: 좋아요만")
    b2 = _call(client, _whole_block(static + dynB), tools, "2턴: 네만")  # 동적 바뀜 → 시스템 프리픽스 깨짐
    print(f"  턴1: {b1}")
    print(f"  턴2: {b2}  <- 도구만 cache_read, 정적 시스템은 재생성")

    print("\n=== AFTER (branch): 정적/동적 분리(cacheable_system, +도구 캐시) ===")
    a1 = _call(client, cacheable_system(static + CACHE_BOUNDARY + dynA), tools, "1턴: 좋아요만")
    a2 = _call(client, cacheable_system(static + CACHE_BOUNDARY + dynB), tools, "2턴: 네만")  # 정적은 캐시 유지
    print(f"  턴1: {a1}")
    print(f"  턴2: {a2}  <- cache_read 높음(정적 프리픽스 재사용)")

    print("\n=== 2턴째 비교 (동적 꼬리가 바뀐 상황 = 현실) ===")
    print(f"  cache_read : before {b2['cache_read']}  →  after {a2['cache_read']}")
    print(f"  input(무캐시): before {b2['input']}  →  after {a2['input']}")
    print(f"  cost(2턴)   : before ${b2['cost']:.5f}  →  after ${a2['cost']:.5f}")
    if b2["cost"]:
        print(f"  절감        : {(b2['cost']-a2['cost'])/b2['cost']*100:+.1f}% (2턴째 기준)")


if __name__ == "__main__":
    main()
