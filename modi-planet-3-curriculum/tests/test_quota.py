"""agent/quota.py 단위 테스트.

- TokenUsage: 가중 합산·덧셈·usage_details 변환
- subject 결정: 접두사·폴백·정규화·절단
- InMemoryQuotaStore: 누적·LRU·clock 주입 KST 자정 리셋
- RedisQuotaStore: fakeredis 두 인스턴스(두 워커) 카운터 공유
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.quota import (
    InMemoryQuotaStore,
    RedisQuotaStore,
    TokenUsage,
    make_quota_store,
    quota_subject,
    usage_from_details,
)

KST = ZoneInfo("Asia/Seoul")


def _ts(y, m, d, hh=0, mm=0, ss=0):
    """KST 벽시계 → epoch 초(clock 주입용)."""
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST).timestamp()


# ── TokenUsage ────────────────────────────────────────────────────
def test_weighted_default_weights():
    u = TokenUsage(input=100, output=10, cache_read=1000, cache_creation=40)
    # 100*1 + 10*5 + 1000*0.1 + 40*1.25 = 100 + 50 + 100 + 50 = 300
    assert u.weighted() == 300


def test_weighted_int_truncation():
    # 1 + 0 + 3*0.1(=0.3) + 0 = 1.3 → int 절단 → 1
    assert TokenUsage(input=1, cache_read=3).weighted() == 1


def test_weighted_custom_weights():
    u = TokenUsage(input=10, output=10, cache_read=10, cache_creation=10)
    assert u.weighted(w_out=2.0, w_cache_read=0.5, w_cache_creation=1.0) == 10 + 20 + 5 + 10


def test_add():
    a = TokenUsage(input=1, output=2, cache_read=3, cache_creation=4)
    b = TokenUsage(input=10, output=20, cache_read=30, cache_creation=40)
    assert a + b == TokenUsage(input=11, output=22, cache_read=33, cache_creation=44)


def test_add_rejects_non_tokenusage():
    with pytest.raises(TypeError):
        _ = TokenUsage(input=1) + 5


def test_frozen():
    with pytest.raises(Exception):
        TokenUsage().input = 5  # type: ignore[misc]


# ── usage_from_details ────────────────────────────────────────────
def test_usage_from_details_none_and_empty():
    assert usage_from_details(None) == TokenUsage()
    assert usage_from_details({}) == TokenUsage()


def test_usage_from_details_key_contract():
    # usage.py:usage_details() 반환 형태 — cache_* 는 있을 때만 들어옴
    details = {
        "input": 100,
        "output": 20,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 30,
    }
    assert usage_from_details(details) == TokenUsage(
        input=100, output=20, cache_read=500, cache_creation=30
    )


def test_usage_from_details_missing_cache_keys():
    assert usage_from_details({"input": 100, "output": 20}) == TokenUsage(input=100, output=20)


# ── quota_subject ─────────────────────────────────────────────────
def test_subject_user_scope_with_uid():
    assert quota_subject("abc123", "sess999") == "u:abc123"


def test_subject_user_scope_falls_back_to_session():
    assert quota_subject("", "sess999") == "s:sess999"


def test_subject_session_scope_always_session():
    assert quota_subject("abc123", "sess999", scope="session") == "s:sess999"


def test_subject_sanitizes_illegal_chars():
    assert quota_subject("a/b c:d", "x") == "u:abcd"


def test_subject_truncates_to_64():
    long_id = "a" * 200
    sub = quota_subject(long_id, "x")
    assert sub == "u:" + "a" * 64


def test_subject_anon_when_all_empty():
    assert quota_subject("", "") == "anon"
    assert quota_subject("!!!", "@@@") == "anon"  # 정규화 후 빈 값
    assert quota_subject("", "", scope="session") == "anon"


# ── InMemoryQuotaStore ────────────────────────────────────────────
def test_inmemory_add_and_used():
    store = InMemoryQuotaStore(clock=lambda: _ts(2026, 7, 13, 10))
    assert store.used("u:x") == TokenUsage()
    store.add("u:x", TokenUsage(input=100, output=10))
    store.add("u:x", TokenUsage(input=50, output=5))
    assert store.used("u:x") == TokenUsage(input=150, output=15)


def test_inmemory_subjects_isolated():
    store = InMemoryQuotaStore(clock=lambda: _ts(2026, 7, 13))
    store.add("u:a", TokenUsage(input=10))
    store.add("u:b", TokenUsage(input=20))
    assert store.used("u:a") == TokenUsage(input=10)
    assert store.used("u:b") == TokenUsage(input=20)


def test_inmemory_kst_midnight_resets():
    now = {"t": _ts(2026, 7, 13, 23, 59)}
    store = InMemoryQuotaStore(clock=lambda: now["t"])
    store.add("u:x", TokenUsage(input=100))
    assert store.used("u:x") == TokenUsage(input=100)
    # 자정 경과(다음 날 KST) → 새 윈도, used()==0
    now["t"] = _ts(2026, 7, 14, 0, 1)
    assert store.used("u:x") == TokenUsage()


def test_inmemory_reset_at_is_next_kst_midnight():
    store = InMemoryQuotaStore(clock=lambda: _ts(2026, 7, 13, 10, 30))
    r = store.reset_at()
    assert (r.year, r.month, r.day, r.hour, r.minute) == (2026, 7, 14, 0, 0)
    assert r.utcoffset().total_seconds() == 9 * 3600  # KST = UTC+9


def test_inmemory_lru_eviction():
    store = InMemoryQuotaStore(max_subjects=3, clock=lambda: _ts(2026, 7, 13))
    for i in range(3):
        store.add(f"u:{i}", TokenUsage(input=1))
    # u:0 을 다시 접근 → 최근 사용으로 승격
    _ = store.used("u:0")
    # 새 subject 추가 → 가장 오래된 u:1 이 제거되어야 함
    store.add("u:3", TokenUsage(input=1))
    assert store.used("u:1") == TokenUsage()  # evicted
    assert store.used("u:0") == TokenUsage(input=1)  # 유지
    assert store.used("u:3") == TokenUsage(input=1)  # 유지


# ── RedisQuotaStore (fakeredis) ───────────────────────────────────
def _fake_client():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_redis_add_and_used():
    store = RedisQuotaStore(_fake_client())
    store.add("u:x", TokenUsage(input=100, output=10, cache_read=5, cache_creation=2))
    assert store.used("u:x") == TokenUsage(input=100, output=10, cache_read=5, cache_creation=2)


def test_redis_accumulates():
    store = RedisQuotaStore(_fake_client())
    store.add("u:x", TokenUsage(input=100, output=10))
    store.add("u:x", TokenUsage(input=50, output=5))
    assert store.used("u:x") == TokenUsage(input=150, output=15)


def test_redis_used_empty_is_zero():
    store = RedisQuotaStore(_fake_client())
    assert store.used("u:none") == TokenUsage()


def test_redis_add_zero_usage_is_noop():
    client = _fake_client()
    store = RedisQuotaStore(client)
    store.add("u:x", TokenUsage())
    assert store.used("u:x") == TokenUsage()


def test_redis_two_workers_share_counter():
    """두 RedisQuotaStore 인스턴스(두 워커)가 같은 fakeredis 를 공유하면 카운터가 합산된다."""
    client = _fake_client()
    worker_a = RedisQuotaStore(client)
    worker_b = RedisQuotaStore(client)
    worker_a.add("u:shared", TokenUsage(input=100, output=10))
    worker_b.add("u:shared", TokenUsage(input=200, output=20))
    # 어느 워커로 읽어도 합산된 값이 보인다
    assert worker_a.used("u:shared") == TokenUsage(input=300, output=30)
    assert worker_b.used("u:shared") == TokenUsage(input=300, output=30)


def test_redis_sets_expire():
    client = _fake_client()
    store = RedisQuotaStore(client)
    store.add("u:x", TokenUsage(input=1))
    key = store._key("u:x")
    ttl = client.ttl(key)
    assert 0 < ttl <= RedisQuotaStore._TTL_SECONDS


# ── make_quota_store 팩토리 seam ──────────────────────────────────
def test_make_quota_store_inmemory_when_no_redis_url():
    assert isinstance(make_quota_store(""), InMemoryQuotaStore)


def test_make_quota_store_redis_when_url(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import redis

    # 지연 import 되는 redis.from_url 만 fakeredis 로 대체(모듈 자체는 유지 —
    # fakeredis 가 내부적으로 real redis 를 import 하므로).
    monkeypatch.setattr(
        redis,
        "from_url",
        lambda url, decode_responses=True: fakeredis.FakeStrictRedis(
            decode_responses=decode_responses
        ),
    )
    store = make_quota_store("redis://localhost:6379/0")
    assert isinstance(store, RedisQuotaStore)
    store.add("u:x", TokenUsage(input=5))
    assert store.used("u:x") == TokenUsage(input=5)
