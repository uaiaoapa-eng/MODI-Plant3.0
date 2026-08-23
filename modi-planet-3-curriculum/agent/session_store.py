"""세션 저장 계층 — 인메모리(내부 서버) 구현 + 클라우드(Redis) 교체용 seam.

배경(전략 분기):
- 내부 서버(현 .95 단일 박스)에선 프로세스 메모리로 충분하다. 다만 세션이 무한
  누적되면 RAM 증가 → OOM → 재시작(S5). TTL/LRU eviction 으로 이를 막는다.
- 클라우드(멀티 인스턴스)에선 복제본 간 세션 공유가 필요해 Redis 등 외부 저장이 필수다.

설계 핵심: server.py 가 *이 인터페이스에만* 의존하게 한다. 그러면 클라우드 전환 시
같은 인터페이스의 RedisSessionStore 만 끼우면 되고(코드 재작성 없음), 내부 서버는
InMemorySessionStore 로 가볍게 간다.

테스트 용이성: clock 을 주입받아 실제 시간 경과 없이 TTL 만료를 검증한다.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, Optional, TypeVar

V = TypeVar("V")


def should_reload(cached_loaded_mtime: float | None, disk_mtime: float) -> bool:
    """멀티워커 stale 방지: 디스크 상태가 캐시된 오케스트레이터보다 최신이면 재로딩 필요.

    단일 박스 멀티워커는 projects/ 볼륨을 공유하므로 상태는 디스크로 공유된다. 다만 워커 A 가
    예전 오케스트레이터를 캐시한 채, 워커 B 가 더 최신 상태를 저장했을 수 있다(다음 턴이 A 로
    오면 stale). 디스크 mtime 이 캐시 적재 시점보다 새로우면 재로딩한다.

    - cached_loaded_mtime is None: 적재 시점 미상 → 안전하게 재로딩.
    - disk_mtime == 0: 디스크 파일 없음(신규) → 캐시가 있으면 그대로 사용(재로딩 불필요).
    """
    if cached_loaded_mtime is None:
        return True
    return disk_mtime > cached_loaded_mtime


class SessionStore(Generic[V]):
    """세션 저장 인터페이스(프로토콜). 클라우드 어댑터(Redis)는 이 시그니처를 따른다."""

    def get(self, key: str) -> Optional[V]: ...
    def set(self, key: str, value: V) -> None: ...
    def get_or_create(self, key: str, factory: Callable[[], V]) -> V: ...
    def pop(self, key: str) -> Optional[V]: ...
    def __contains__(self, key: str) -> bool: ...
    def __len__(self) -> int: ...


class InMemorySessionStore(Generic[V]):
    """TTL + LRU eviction 을 갖춘 인메모리 세션 저장소(내부 서버용).

    - ttl_seconds: 마지막 접근 후 이 시간이 지나면 만료(0 이하면 만료 없음).
    - max_size: 초과 시 가장 오래 접근 안 된 항목부터 제거(LRU).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 86400.0,
        max_size: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_size
        self._clock = clock
        self._data: dict[str, tuple[float, V]] = {}  # key -> (last_access, value)
        self._lock = threading.Lock()

    # ── 조회/저장 ──
    def get(self, key: str) -> Optional[V]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, val = item
            if self._expired(ts):
                del self._data[key]
                return None
            self._data[key] = (self._clock(), val)  # touch → LRU 갱신
            return val

    def set(self, key: str, value: V) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._evict_locked()

    def get_or_create(self, key: str, factory: Callable[[], V]) -> V:
        """있으면 반환, 없으면 factory()로 생성. factory 는 lock 밖에서 호출한다
        (무거운 생성=orchestrator 복원이 lock 을 오래 잡지 않도록). 동시 생성 시 첫 값 유지."""
        existing = self.get(key)
        if existing is not None:
            return existing
        created = factory()
        with self._lock:
            item = self._data.get(key)
            if item is not None and not self._expired(item[0]):
                return item[1]  # 다른 스레드가 먼저 생성함 → 그걸 쓰고 created 는 버림
            self._data[key] = (self._clock(), created)
            self._evict_locked()
            return created

    def pop(self, key: str) -> Optional[V]:
        with self._lock:
            item = self._data.pop(key, None)
            return item[1] if item else None

    # ── dict 유사 인터페이스(server.py drop-in) ──
    def __setitem__(self, key: str, value: V) -> None:
        self.set(key, value)

    def __getitem__(self, key: str) -> V:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._data)

    def keys(self) -> list[str]:
        with self._lock:
            self._purge_expired_locked()
            return list(self._data.keys())

    # ── 내부 ──
    def _expired(self, ts: float) -> bool:
        return self._ttl > 0 and (self._clock() - ts) > self._ttl

    def _purge_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        now = self._clock()
        dead = [k for k, (ts, _) in self._data.items() if (now - ts) > self._ttl]
        for k in dead:
            del self._data[k]

    def _evict_locked(self) -> None:
        self._purge_expired_locked()
        # LRU: max_size 초과분은 last_access 가 가장 오래된 것부터 제거.
        while len(self._data) > self._max:
            oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
            del self._data[oldest]
