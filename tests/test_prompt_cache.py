"""prompt_cache 헬퍼 단위 테스트 — 형태 검증 + 입력 불변성."""
from agent.prompt_cache import (
    cacheable_system, cacheable_tools, split_cacheable, strip_cache_boundary,
    CACHE_BOUNDARY,
)


def test_cacheable_system_wraps_with_cache_control():
    out = cacheable_system("너는 도우미야")
    assert out == [{
        "type": "text",
        "text": "너는 도우미야",
        "cache_control": {"type": "ephemeral"},
    }]


def test_split_cacheable_no_boundary_is_all_static():
    assert split_cacheable("전부 정적") == ("전부 정적", "")


def test_split_cacheable_first_boundary_only():
    s = "정적" + CACHE_BOUNDARY + "동적1" + CACHE_BOUNDARY + "동적2"
    static, dynamic = split_cacheable(s)
    assert static == "정적"
    assert dynamic == "동적1" + CACHE_BOUNDARY + "동적2"  # 첫 경계 이후는 전부 동적


def test_cacheable_system_splits_static_and_dynamic():
    """#67 T1: 경계가 있으면 정적 프리픽스만 cache_control, 동적 꼬리는 캐시 없음."""
    out = cacheable_system("정적 규칙" + CACHE_BOUNDARY + "매 턴 바뀌는 코드")
    assert len(out) == 2
    assert out[0] == {"type": "text", "text": "정적 규칙",
                      "cache_control": {"type": "ephemeral"}}
    assert out[1] == {"type": "text", "text": "매 턴 바뀌는 코드"}
    # 실제 API 로 나가는 텍스트에 경계 토큰이 새면 안 된다.
    assert all(CACHE_BOUNDARY not in b["text"] for b in out)


def test_cacheable_system_static_prefix_stable_across_dynamic_tails():
    """캐시 히트의 핵심: 동적 꼬리가 달라도 정적 프리픽스(=캐시 프리픽스)는 동일해야 한다."""
    a = cacheable_system("규칙" + CACHE_BOUNDARY + "코드 버전 A")
    b = cacheable_system("규칙" + CACHE_BOUNDARY + "완전히 다른 코드 버전 B")
    assert a[0] == b[0]  # 정적 블록 동일 → cache_read 발생


def test_cacheable_system_empty_dynamic_is_single_block():
    out = cacheable_system("정적만" + CACHE_BOUNDARY)
    assert len(out) == 1
    assert out[0]["text"] == "정적만"
    assert out[0]["cache_control"] == {"type": "ephemeral"}


def test_cacheable_system_boundary_at_start_is_uncached_only():
    out = cacheable_system(CACHE_BOUNDARY + "전부 동적")
    assert out == [{"type": "text", "text": "전부 동적"}]  # 정적 없음 → 캐시 없음


def test_strip_cache_boundary_removes_all():
    s = "a" + CACHE_BOUNDARY + "b" + CACHE_BOUNDARY + "c"
    assert strip_cache_boundary(s) == "abc"
    assert CACHE_BOUNDARY not in strip_cache_boundary(s)


def test_cacheable_system_empty_returns_empty_list():
    assert cacheable_system("") == []
    assert cacheable_system(None) == []  # 방어적


def test_cacheable_tools_marks_last_only():
    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    out = cacheable_tools(tools)
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    assert out[2]["cache_control"] == {"type": "ephemeral"}
    assert out[2]["name"] == "c"


def test_cacheable_tools_does_not_mutate_input():
    tools = [{"name": "a"}, {"name": "b"}]
    _ = cacheable_tools(tools)
    assert "cache_control" not in tools[-1]  # 원본 보존


def test_cacheable_tools_empty():
    assert cacheable_tools([]) == []
    assert cacheable_tools(None) is None
