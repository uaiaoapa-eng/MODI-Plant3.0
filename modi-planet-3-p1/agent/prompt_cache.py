"""LLM 프롬프트/컨텍스트 캐싱 — Anthropic prompt caching(cache_control) 헬퍼.

정적이고 큰 블록(시스템 프롬프트, 도구 정의)에 cache_control=ephemeral 을 붙이면
다음 호출에서 캐시 적중(cache_read)으로 입력 토큰 비용·지연이 크게 준다.
usage 에 cache_read/cache_creation 은 이미 추적 중(agent/usage.py).

적용 범위(전략 분기):
- API 모드(anthropic SDK): 아래 헬퍼로 system/tools 에 cache_control 부착.
- CLI 구독 모드: CLI 가 프롬프트를 자체 구성하므로 SDK용 cache_control 은 무시된다.
  → 호출부에서 CLI 모드면 원본을 그대로 넘긴다(무변경, 무위험). API 전환(후속 PR) 시 활성.

순수 함수라 단위 테스트가 쉽다. 입력을 변형하지 않고 새 객체를 만들어 반환한다(불변).
"""
from __future__ import annotations

from typing import Any

_EPHEMERAL = {"type": "ephemeral"}

# 정적 프리픽스와 동적 꼬리를 가르는 내부 경계 토큰(#67 T1).
# 시스템 프롬프트 조립부(orchestrator)가 "매 턴 동일한 규칙/도구계약" 뒤·"매 턴 바뀌는
# 컨텍스트(코드·설계·재사용 블록)" 앞에 이 토큰을 넣는다. cacheable_system 이 여기서 잘라
# 정적 프리픽스에만 cache_control 을 붙이므로, 동적 꼬리가 바뀌어도 캐시 프리픽스가
# 깨지지 않아 cache_read 히트가 난다. (기존엔 프롬프트 전체를 한 블록으로 캐시해 매 턴
# 프리픽스 해시가 바뀌어 히트가 없었다 — Langfuse 실측 cache_read 14%.)
# U+E000(사설 영역)이라 정상 프롬프트 텍스트엔 등장하지 않고, null 바이트 문제도 없다.
CACHE_BOUNDARY = "CACHE_BOUNDARY"


def split_cacheable(system: str) -> tuple[str, str]:
    """시스템 프롬프트를 (정적 프리픽스, 동적 꼬리)로 가른다.

    경계 토큰이 없으면 전체가 정적(꼬리 빈 문자열). 토큰이 여러 번 있어도 첫 경계만 쓴다
    (첫 경계 이후는 모두 동적으로 본다).
    """
    if CACHE_BOUNDARY in system:
        static, dynamic = system.split(CACHE_BOUNDARY, 1)
        return static, dynamic
    return system, ""


def strip_cache_boundary(system: str) -> str:
    """경계 토큰을 제거해 원래 프롬프트로 복원 — CLI 모드/비캐시 경로용.

    CLI 는 프롬프트를 자체 구성하므로 SDK 캐시 개념이 없다. 경계 토큰이 프롬프트에
    새어 나가지 않도록 여기서 지운다.
    """
    return system.replace(CACHE_BOUNDARY, "")


def cacheable_system(system: str) -> list[dict[str, Any]]:
    """시스템 프롬프트를 cache_control 붙은 text 블록 리스트로 변환(#67 T1).

    CACHE_BOUNDARY 가 있으면 정적 프리픽스에만 cache_control 을 붙이고 동적 꼬리는
    별도 블록으로 캐시 없이 붙인다(프리픽스 캐시 히트만 노림). 경계가 없으면 종전처럼
    전체를 한 캐시 블록으로. 빈 문자열이면 빈 리스트(API 가 빈 블록을 거부하지 않도록).
    """
    if not system:
        return []
    static, dynamic = split_cacheable(system)
    blocks: list[dict[str, Any]] = []
    if static:
        blocks.append({"type": "text", "text": static, "cache_control": dict(_EPHEMERAL)})
    if dynamic:
        # 동적 꼬리는 매 턴 바뀌므로 캐시하지 않는다(붙이면 프리픽스가 깨져 캐시 무의미).
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def cacheable_tools(tools: list[dict] | None) -> list[dict] | None:
    """도구 정의 리스트의 *마지막* 항목에 cache_control 을 부착.

    Anthropic 은 마지막 캐시 지점까지의 도구 블록을 한 번에 캐시하므로, 마지막 도구
    하나에만 표시하면 도구 정의 전체가 캐시된다. 원본은 변형하지 않는다(복사본 반환).
    """
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": dict(_EPHEMERAL)}
    return out
