"""RedisSessionLock 단위 테스트 — fakeredis 로 실제 서버 없이 분산 락 검증.

두 RedisSessionLock 인스턴스가 같은 fakeredis 를 공유하면 '두 워커' 를 흉내낼 수 있다.
"""
import pytest

fakeredis = pytest.importorskip("fakeredis")

from agent.concurrency import (  # noqa: E402  (fakeredis importorskip 뒤에 와야 함)
    RedisSessionLock,
    SessionLockRegistry,
    make_session_lock,
)


def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_acquire_then_second_fails_same_lock():
    lk = RedisSessionLock(_client())
    assert lk.acquire("s1") is True
    assert lk.acquire("s1") is False
    lk.release("s1")
    assert lk.acquire("s1") is True


def test_cross_worker_mutual_exclusion():
    """워커 A(lockA)와 워커 B(lockB)가 같은 Redis 공유 → 한 세션은 하나만 잡는다."""
    r = _client()
    lock_a = RedisSessionLock(r)
    lock_b = RedisSessionLock(r)
    assert lock_a.acquire("same") is True
    assert lock_b.acquire("same") is False   # 다른 워커도 못 잡음(분산 락)
    lock_a.release("same")
    assert lock_b.acquire("same") is True     # A 가 풀면 B 가 잡음


def test_is_busy_reflects_state():
    r = _client()
    lk = RedisSessionLock(r)
    assert lk.is_busy("s") is False
    lk.acquire("s")
    assert lk.is_busy("s") is True
    lk.release("s")
    assert lk.is_busy("s") is False


def test_release_only_deletes_own_token():
    """워커 A 의 release 가 (TTL 만료 후 B 가 잡은) 남의 락을 지우면 안 된다."""
    r = _client()
    lock_a = RedisSessionLock(r)
    lock_b = RedisSessionLock(r)
    assert lock_a.acquire("s") is True
    # A 의 락을 강제로 만료시키고 B 가 새로 잡은 상황을 모사
    r.delete("edu:lock:session:s")
    assert lock_b.acquire("s") is True
    lock_a.release("s")                 # A 가 뒤늦게 release — 토큰 불일치라 무시돼야
    assert lock_b.is_busy("s") is True  # B 의 락은 살아 있어야 함


def test_different_sessions_independent():
    lk = RedisSessionLock(_client())
    assert lk.acquire("a") is True
    assert lk.acquire("b") is True
    lk.release("a")
    lk.release("b")


def test_make_session_lock_factory():
    # URL 없으면 인메모리
    assert isinstance(make_session_lock(""), SessionLockRegistry)
    # URL 있으면 RedisSessionLock (실제 연결은 지연 — 객체 생성만 확인)
    lk = make_session_lock("redis://localhost:6379/0")
    assert isinstance(lk, RedisSessionLock)
