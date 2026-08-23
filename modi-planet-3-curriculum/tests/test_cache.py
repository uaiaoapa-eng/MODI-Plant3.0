"""TTLCache 단위 테스트."""
from agent.cache import TTLCache


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_set_get():
    c = TTLCache(ttl_seconds=10)
    c.set("k", [1, 2, 3])
    assert c.get("k") == [1, 2, 3]
    assert c.get("missing") is None


def test_expiry():
    clk = FakeClock()
    c = TTLCache(ttl_seconds=30, clock=clk)
    c.set("k", "v")
    clk.advance(20)
    assert c.get("k") == "v"
    clk.advance(20)  # 총 40 > 30
    assert c.get("k") is None


def test_get_or_compute_caches():
    clk = FakeClock()
    c = TTLCache(ttl_seconds=30, clock=clk)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return "computed"

    assert c.get_or_compute("k", compute) == "computed"
    assert c.get_or_compute("k", compute) == "computed"
    assert calls["n"] == 1  # 두 번째는 캐시

    clk.advance(40)  # 만료 후 재계산
    assert c.get_or_compute("k", compute) == "computed"
    assert calls["n"] == 2


def test_invalidate_and_clear():
    c = TTLCache(ttl_seconds=30)
    c.set("a", 1)
    c.set("b", 2)
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") == 2
    c.clear()
    assert c.get("b") is None
