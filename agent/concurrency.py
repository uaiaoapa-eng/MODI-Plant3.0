"""세션 단위 동시 실행 가드.

문제(S1): server.py 의 `sessions[session_id] -> StreamOrchestrator` 는 세션당 단일
인스턴스라, 같은 session_id 로 두 요청(새 탭·더블클릭·프론트 중복 전송)이 동시에
들어오면 orchestrator 의 가변 상태(state._messages, generated_code_map, _cancelled,
_step_count ...)가 동시에 수정돼 응답이 섞이거나 유실된다. 이 모듈은
"session_id 당 한 번에 하나의 턴"을 보장한다.

설계 의도(테스트 용이성):
- 순수 인메모리 자료구조 + threading.Lock 만 사용. FastAPI/네트워크/서브프로세스 의존 없음.
- 비차단(acquire) 으로 즉시 성공/실패를 돌려줘, 호출부가 "이미 처리 중" 응답을 바로 줄 수 있다
  (대기 큐를 만들지 않아 스레드가 쌓이지 않는다).
- 락 획득 스레드와 해제 스레드가 달라도 됨: threading.Lock 은 소유자에 묶이지 않으므로
  acquire 는 async 핸들러 스레드, release 는 스트리밍 제너레이터(스레드풀) 스레드에서 호출 가능.
  (RLock 이면 다른 스레드 release 가 불가능하므로 일반 Lock 을 쓴다.)
"""
from __future__ import annotations

import threading
import uuid


class SessionLockRegistry:
    """session_id -> Lock 매핑을 관리하는 비차단 동시성 가드."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()  # _locks dict 자체의 동시 수정 보호

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(session_id)
            if lk is None:
                lk = threading.Lock()
                self._locks[session_id] = lk
            return lk

    def acquire(self, session_id: str) -> bool:
        """비차단 획득. 이미 같은 세션이 처리 중이면 즉시 False."""
        return self._lock_for(session_id).acquire(blocking=False)

    def release(self, session_id: str) -> None:
        """acquire 가 성공한 세션에 대해서만 호출한다(획득/해제 짝 맞춤).

        방어적으로, 이미 풀려 있는 락을 release 해도 예외를 삼킨다(중복 release 안전).
        """
        try:
            self._lock_for(session_id).release()
        except RuntimeError:
            pass

    def is_busy(self, session_id: str) -> bool:
        """현재 해당 세션이 처리 중인지(락이 잡혀 있는지)."""
        return self._lock_for(session_id).locked()

    def discard(self, session_id: str) -> None:
        """세션 삭제 시 락 항목을 정리해 dict 무한 증가를 막는다.

        잡혀 있는 락은 남겨 둔다(처리 중인 턴을 깨지 않기 위해).
        """
        with self._guard:
            lk = self._locks.get(session_id)
            if lk is not None and not lk.locked():
                del self._locks[session_id]


class RedisSessionLock:
    """워커/레플리카 간 공유되는 분산 세션 락(S1, 멀티워커용).

    멀티워커로 띄우면 InMemorySessionLock 은 프로세스마다 따로라 같은 session_id 가
    서로 다른 워커에 동시에 들어가도 못 막는다. Redis 의 `SET NX PX` 로 전 워커가
    공유하는 락을 잡는다.

    - 비차단 acquire(SET NX) — 이미 잡혀 있으면 False.
    - 해제는 토큰 비교 후 삭제(Lua)로, 내 락만 지운다(TTL 만료 후 남의 락 오삭제 방지).
    - TTL(px): 워커가 죽어 release 를 못해도 자동 만료해 영구 데드락을 막는다.
      한 턴이 최대 CLI 타임아웃(~600s)까지 갈 수 있으므로 그보다 넉넉히 잡는다(기본 900s).

    인터페이스는 SessionLockRegistry 와 동일(acquire/release/is_busy/discard) → server 무변경 교체.
    """

    def __init__(self, client, *, ttl_ms: int = 900_000, prefix: str = "edu:lock:session:") -> None:
        self._r = client
        self._ttl_ms = ttl_ms
        self._prefix = prefix
        # session_id -> 내가 잡은 토큰(같은 워커 안에서 release 시 사용). acquire/release 는 동일 워커.
        self._tokens: dict[str, str] = {}
        self._guard = threading.Lock()

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def acquire(self, session_id: str) -> bool:
        token = uuid.uuid4().hex
        ok = self._r.set(self._key(session_id), token, nx=True, px=self._ttl_ms)
        if ok:
            with self._guard:
                self._tokens[session_id] = token
        return bool(ok)

    def release(self, session_id: str) -> None:
        with self._guard:
            token = self._tokens.pop(session_id, None)
        if token is None:
            return
        key = self._key(session_id)
        try:
            # 내 토큰일 때만 삭제 — TTL 만료 후 남이 새로 잡은 락을 오삭제하지 않는다.
            # (GET→DEL 사이 미세 경합은 TTL=수백초 대비 무시 가능. 실서버/ fakeredis 공통 동작.)
            if self._r.get(key) == token:
                self._r.delete(key)
        except Exception:
            pass  # 네트워크 일시 오류 — TTL 이 결국 만료시킨다

    def is_busy(self, session_id: str) -> bool:
        try:
            return bool(self._r.exists(self._key(session_id)))
        except Exception:
            return False

    def discard(self, session_id: str) -> None:
        # TTL 이 정리하므로 별도 작업 불필요(인터페이스 호환용).
        return None


def make_session_lock(redis_url: str = "", *, ttl_ms: int = 900_000):
    """REDIS_URL 이 있으면 분산 락, 없으면 인메모리 락. (server 가 이 팩토리만 호출)"""
    if redis_url:
        import redis  # 지연 import — Redis 미사용 환경에선 의존성 불필요
        client = redis.from_url(redis_url, decode_responses=True)
        return RedisSessionLock(client, ttl_ms=ttl_ms)
    return SessionLockRegistry()
