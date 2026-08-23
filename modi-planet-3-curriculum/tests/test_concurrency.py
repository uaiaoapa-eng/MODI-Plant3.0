"""SessionLockRegistry 단위 테스트 — 서버/인증/네트워크 없이 순수 동작 검증."""
import threading

from agent.concurrency import SessionLockRegistry


def test_second_acquire_same_session_fails():
    reg = SessionLockRegistry()
    assert reg.acquire("s1") is True
    assert reg.acquire("s1") is False  # 이미 처리 중 → 즉시 거절
    reg.release("s1")
    assert reg.acquire("s1") is True  # 해제 후 다시 획득 가능


def test_different_sessions_are_independent():
    reg = SessionLockRegistry()
    assert reg.acquire("a") is True
    assert reg.acquire("b") is True  # 다른 세션은 막지 않음
    reg.release("a")
    reg.release("b")


def test_is_busy_transitions():
    reg = SessionLockRegistry()
    assert reg.is_busy("s") is False
    reg.acquire("s")
    assert reg.is_busy("s") is True
    reg.release("s")
    assert reg.is_busy("s") is False


def test_release_from_other_thread():
    """acquire(async 핸들러 스레드)와 release(스트림 스레드)가 달라도 동작해야 한다."""
    reg = SessionLockRegistry()
    assert reg.acquire("s") is True

    done: list[bool] = []

    def releaser():
        reg.release("s")
        done.append(True)

    t = threading.Thread(target=releaser)
    t.start()
    t.join()

    assert done == [True]
    assert reg.acquire("s") is True  # 다른 스레드의 release 후 재획득 가능


def test_double_release_is_safe():
    reg = SessionLockRegistry()
    reg.acquire("s")
    reg.release("s")
    reg.release("s")  # 중복 release — 예외 없이 무시되어야 함


def test_only_one_winner_under_concurrency():
    """20개 스레드가 동시에 같은 세션을 acquire → 정확히 1개만 성공."""
    reg = SessionLockRegistry()
    wins: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # 동시 출발
        if reg.acquire("s"):
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(wins) == 1


def test_discard_removes_free_lock_but_keeps_held():
    reg = SessionLockRegistry()
    reg.acquire("held")
    reg.discard("held")  # 잡혀 있으면 보존
    assert reg.is_busy("held") is True

    reg.acquire("free")
    reg.release("free")
    reg.discard("free")  # 풀려 있으면 제거 — 이후에도 새로 획득 가능해야 함
    assert reg.acquire("free") is True
