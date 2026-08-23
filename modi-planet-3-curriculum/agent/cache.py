"""범용 TTL 캐시 — 읽기 위주 응답/조회 결과 캐싱(내부 서버용, 인프로세스).

용도:
- /reference 같이 자주 안 바뀌는 목록 조회를 짧은 TTL 로 캐싱(디스크 I/O 절감).
- /health/llm 의 LLM 핑 결과 캐싱(구독 쿼터 보호).

내부 서버(단일 박스)에선 인프로세스로 충분하다. 클라우드(멀티 노드)에선 노드 간
코히런스가 필요하므로 같은 인터페이스의 분산 캐시(Redis) 구현으로 교체한다.

테스트 용이성: clock 주입으로 실제 대기 없이 만료를 검증.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, Optional, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(self, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[float, V]] = {}  # key -> (stored_at, value)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[V]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, val = item
            if self._expired(ts):
                del self._data[key]
                return None
            return val

    def set(self, key: str, value: V) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)

    def get_or_compute(self, key: str, fn: Callable[[], V]) -> V:
        """캐시에 있으면 반환, 없으면 fn()으로 계산 후 저장.

        fn 은 lock 밖에서 호출(무거운 I/O 가 lock 을 잡지 않도록). 동시 호출 시
        중복 계산은 발생할 수 있으나(읽기 캐시라 무해) 결과는 마지막 값으로 수렴.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = fn()
        self.set(key, value)
        return value

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def _expired(self, ts: float) -> bool:
        return self._ttl > 0 and (self._clock() - ts) > self._ttl
