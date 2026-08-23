"""Langfuse usage/cost 헬퍼 — CLI dict 와 SDK Usage 객체를 공용으로 처리.

orchestrator_stream / context 등 여러 곳이 import 하므로 순환 의존을 피하려고 별도 모듈로 둔다.

핵심: Langfuse 는 generation 에 붙은 `model` + `usage_details` 로 비용을 자체 계산한다.
그래서 우리가 토큰을 종류별(input/output/cache_read/cache_creation)로 정확히 넘겨야
캐시 절감까지 포함한 단계별 비용이 Langfuse 에서 그대로 보인다. (달러는 우리가 계산하지 않는다.)
"""

from __future__ import annotations


def _usage_get(usage, key: str) -> int:
    """CLI dict / SDK Usage 객체 공용 토큰 접근."""
    if isinstance(usage, dict):
        return usage.get(key) or 0
    return getattr(usage, key, 0) or 0


def usage_details(usage) -> dict | None:
    """Langfuse usage_details dict 생성.

    input/output 은 항상, cache_read/cache_creation 은 있을 때만 넣는다
    (캐시 읽기는 정가의 ~0.1× 라 Langfuse 가 따로 단가를 매겨 비용을 정확히 쪼갠다).
    토큰이 전혀 없으면 None — '0 토큰 generation' 오해 방지.
    """
    if usage is None:
        return None
    in_tok = _usage_get(usage, "input_tokens")
    out_tok = _usage_get(usage, "output_tokens")
    cache_read = _usage_get(usage, "cache_read_input_tokens")
    cache_creation = _usage_get(usage, "cache_creation_input_tokens")
    if not (in_tok or out_tok or cache_read or cache_creation):
        return None
    details = {"input": in_tok, "output": out_tok}
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    if cache_creation:
        details["cache_creation_input_tokens"] = cache_creation
    return details


def extract_usage(message) -> tuple[dict | None, dict | None]:
    """(usage_details, cost_details).

    - CLI 경로: message.usage 는 dict, message.cost_usd(=CLI total_cost_usd) 존재 → 명시 비용으로 전달
    - SDK 경로: cost_usd 없음 → cost_details=None, Langfuse 가 model 가격으로 계산
    """
    details = usage_details(getattr(message, "usage", None))
    cost = getattr(message, "cost_usd", None)
    return details, ({"total": cost} if cost else None)


def attribute_output(output_tokens: int, model_text: str, tool_uses) -> dict:
    """#68 O1: 한 LLM 호출의 출력 토큰을 생성 '종류'별로 근사 배분.

    출력 토큰은 호출 1회 총합(산문 + tool_use JSON)으로만 오고 종류별 분해는 API 가 주지
    않는다. 각 조각의 문자 길이 비율로 근사한다 — tool_use 출력의 대부분은 generate_code 의
    `code`(전체 파일 재작성)와 edit_code 의 old/new_code(부분 수정 diff)가 차지하므로,
    이 둘의 비율이 곧 "수정 턴 전체재작성 낭비"의 척도가 된다(#68 의 핵심 지표).

    반환: {"generate_code", "edit_code", "other_tool", "prose"} 토큰 근사값(합 == output_tokens).
    """
    import json

    weights = {"generate_code": 0, "edit_code": 0, "other_tool": 0,
               "prose": len(model_text or "")}
    for tu in tool_uses or []:
        name = getattr(tu, "name", None) or (tu.get("name") if isinstance(tu, dict) else None)
        inp = getattr(tu, "input", None)
        if inp is None and isinstance(tu, dict):
            inp = tu.get("input")
        inp = inp or {}
        if name == "generate_code":
            weights["generate_code"] += len(str(inp.get("code", "")))
        elif name == "edit_code":
            weights["edit_code"] += len(str(inp.get("old_code", ""))) + len(str(inp.get("new_code", "")))
        else:
            try:
                weights["other_tool"] += len(json.dumps(inp, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
    total_w = sum(weights.values())
    if not total_w or not output_tokens:
        return {k: 0 for k in weights}
    out = {k: int(output_tokens * w / total_w) for k, w in weights.items()}
    # 반올림 오차는 가중치가 가장 큰 항목에 흡수 → 합을 정확히 output_tokens 로 보존.
    drift = output_tokens - sum(out.values())
    if drift:
        out[max(weights, key=lambda k: weights[k])] += drift
    return out


def update_generation(gen, model: str, response=None, *, usage=None, cost=None, output=None, **metadata) -> None:
    """generation 핸들에 model/usage/cost/metadata/output 을 한 번에 부착.

    start_as_current_observation(as_type="generation") 가 돌려준 핸들을 받는다.
    response(우리 Message/SDK Message)를 주면 거기서 usage·cost_usd 를 뽑고,
    usage/cost 를 직접 줘도 된다(가드레일처럼 response 핸들이 없는 경로용).
    """
    if usage is None and response is not None:
        usage = getattr(response, "usage", None)
    if cost is None and response is not None:
        cost = getattr(response, "cost_usd", None)
    meta = {k: v for k, v in metadata.items() if v is not None}
    gen.update(
        model=model,
        usage_details=usage_details(usage),
        cost_details=({"total": cost} if cost else None),
        output=output,
        metadata=meta or None,
    )
