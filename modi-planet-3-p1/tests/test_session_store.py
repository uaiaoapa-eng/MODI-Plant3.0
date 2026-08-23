"""InMemorySessionStore 단위 테스트 — clock 주입으로 시간 경과 없이 TTL 검증."""
import threading

from agent.session_store import InMemorySessionStore, should_reload


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_set_get_and_contains():
    s = InMemorySessionStore()
    s.set("a", 1)
    assert s.get("a") == 1
    assert "a" in s
    assert "b" not in s
    assert s.get("b") is None


def test_ttl_expiry():
    # TTL 은 '마지막 접근 후' 기준(슬라이딩 = 비활성 만료). 접근 없이 TTL 초과 시 만료.
    clk = FakeClock()
    s = InMemorySessionStore(ttl_seconds=100, clock=clk)
    s.set("a", "v")
    clk.advance(110)           # 접근 없이 110 > 100 경과
    assert s.get("a") is None  # 만료
    assert "a" not in s


def test_access_refreshes_ttl():
    clk = FakeClock()
    s = InMemorySessionStore(ttl_seconds=100, clock=clk)
    s.set("a", "v")
    clk.advance(80)
    assert s.get("a") == "v"  # touch → last_access 갱신
    clk.advance(80)           # 마지막 접근 기준 80 < 100
    assert s.get("a") == "v"  # 여전히 유효


def test_lru_eviction_on_max_size():
    clk = FakeClock()
    s = InMemorySessionStore(max_size=2, ttl_seconds=0, clock=clk)
    s.set("a", 1)
    clk.advance(1)
    s.set("b", 2)
    clk.advance(1)
    s.get("a")                      # a 를 최근 접근 → b 가 가장 오래됨
    clk.advance(1)
    s.set("c", 3)                   # 초과 → 가장 오래된 b 제거
    assert "b" not in s
    assert "a" in s and "c" in s


def test_get_or_create_creates_once():
    s = InMemorySessionStore()
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return "obj"

    assert s.get_or_create("k", factory) == "obj"
    assert s.get_or_create("k", factory) == "obj"  # 두 번째는 캐시 반환
    assert calls["n"] == 1


def test_get_or_create_single_under_concurrency():
    """동시 get_or_create → 저장되는 값은 하나(첫 승자)로 수렴."""
    s = InMemorySessionStore()
    barrier = threading.Barrier(10)
    results = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        val = s.get_or_create("k", lambda: f"v{i}")
        with lock:
            results.append(val)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1          # 모두 같은 값
    assert s.get("k") == results[0]


def test_pop_and_len():
    clk = FakeClock()
    s = InMemorySessionStore(ttl_seconds=100, clock=clk)
    s.set("a", 1)
    s.set("b", 2)
    assert len(s) == 2
    assert s.pop("a") == 1
    assert s.pop("a") is None
    assert len(s) == 1
    clk.advance(200)
    assert len(s) == 0  # len 은 만료 항목을 정리하고 셈


def test_getitem_raises_keyerror():
    s = InMemorySessionStore()
    s["x"] = 9
    assert s["x"] == 9
    try:
        _ = s["missing"]
        assert False, "KeyError 가 나야 함"
    except KeyError:
        pass


# ── should_reload (멀티워커 stale 캐시 무효화) ──

def test_should_reload_when_unknown_mtime():
    assert should_reload(None, 123.0) is True  # 적재 시점 미상 → 재로딩


def test_should_reload_when_disk_newer():
    assert should_reload(100.0, 200.0) is True   # 다른 워커가 더 최신 저장 → 재로딩


def test_no_reload_when_cache_current_or_newer():
    assert should_reload(200.0, 200.0) is False  # 동일 → 캐시 사용
    assert should_reload(300.0, 200.0) is False  # 캐시가 더 최신 → 캐시 사용


def test_no_reload_when_no_disk_file():
    assert should_reload(100.0, 0.0) is False  # 디스크 파일 없음(신규) → 캐시 유지
