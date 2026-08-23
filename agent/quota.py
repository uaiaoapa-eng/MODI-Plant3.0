"""사용자별 토큰 사용량 카운터 — 실시간 쿼터 판정용 로컬 집계.

배경: 토큰/비용은 Langfuse 로 계측만 된다(사후 관측 — usage.py:5-7). 한 사용자가
무한정 턴을 돌려도 서버는 막지 못한다. 실시간으로 "이 사용자가 오늘 얼마나 썼는가"를
판정하려면 인프로세스(또는 워커 공유) 카운터가 필요하다.

설계(설계문서 docs/design/token-quota-and-error-structure.md §3):
- 저장소 seam: `REDIS_URL` 있으면 Redis(멀티워커 공유), 없으면 인메모리 —
  `make_session_lock`(concurrency.py:126)과 동형의 팩토리 분기 + 지연 import.
- 윈도: KST 캘린더 일 고정(자정 리셋). 서버 TZ 무관하게 zoneinfo 로 명시.
- 판정 단위: 가중 토큰 근사(출력이 비용 지배 — orchestrator_stream.py:65 실측).
  달러 비용은 계산하지 않는다(Langfuse 몫).
- subject 결정: `u:`/`s:` 접두사(회원 도입 시 `m:` 확장). 식별자는 server.py 의
  `_safe_id` 와 동형 정규식으로 정규화(순환 방지 위해 server import 없이 복제).
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

# 날짜 경계는 서버 TZ 와 무관하게 KST 로 고정한다(엣지 케이스 결정).
_KST = ZoneInfo("Asia/Seoul")

# server.py:393 `_SAFE_ID_RE` 와 동형 — server import 는 순환을 만들므로 복제한다.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_ID_LEN = 64


def _safe_id(s: str) -> str:
    """식별자 정규화: 허용 문자 외 제거 + 64자 절단(Redis 키 조작·폭증 방지)."""
    return _SAFE_ID_RE.sub("", s or "")[:_MAX_ID_LEN]


def _now_kst() -> datetime:
    return datetime.now(_KST)


def _today_kst(now: datetime | None = None) -> str:
    return (now or _now_kst()).strftime("%Y%m%d")


def _next_kst_midnight(now: datetime) -> datetime:
    """`now`(KST) 이후의 다음 자정(tz-aware) — 윈도 리셋 시각."""
    d = (now + timedelta(days=1)).date()
    return datetime(d.year, d.month, d.day, tzinfo=_KST)


@dataclass(frozen=True)
class TokenUsage:
    """토큰 사용량(종류별) + 턴 수. 토큰 4종은 usage.py 의 usage_details 키 계약과 정합.

    `turns` 는 이 누적에 포함된 대화 턴 수다(한 턴 기록 시 1). 토큰과 별개 차원의
    상한(QUOTA_DAILY_MAX_TURNS — "한 사용자당 하루 N번")에 쓰이며, `weighted()`(비용
    근사)에는 포함되지 않는다. usage_from_details 로는 채워지지 않고 기록 지점에서만 1 이 실린다.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    turns: int = 0

    def weighted(
        self,
        w_out: float = 5.0,
        w_cache_read: float = 0.1,
        w_cache_creation: float = 1.25,
    ) -> int:
        """비용 비례 가중 토큰 근사(입력 가중치는 1). 반올림은 int 절단으로 일관.

        turns 는 비용 지표가 아니므로 가중치 계산에 넣지 않는다(토큰 상한과 독립 차원)."""
        return int(
            self.input
            + self.output * w_out
            + self.cache_read * w_cache_read
            + self.cache_creation * w_cache_creation
        )

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_creation=self.cache_creation + other.cache_creation,
            turns=self.turns + other.turns,
        )


def usage_from_details(details: dict | None) -> TokenUsage:
    """agent/usage.py:usage_details() 반환 dict → TokenUsage. None/빈 dict → TokenUsage().

    키 계약(usage.py:35-39): input / output / cache_read_input_tokens /
    cache_creation_input_tokens. cache_* 는 값이 있을 때만 들어온다.
    """
    if not details:
        return TokenUsage()
    return TokenUsage(
        input=int(details.get("input", 0) or 0),
        output=int(details.get("output", 0) or 0),
        cache_read=int(details.get("cache_read_input_tokens", 0) or 0),
        cache_creation=int(details.get("cache_creation_input_tokens", 0) or 0),
    )


class QuotaStore(Protocol):
    """일(KST) 윈도 사용량 카운터 인터페이스. Redis/인메모리 어댑터가 이 시그니처를 따른다."""

    def add(self, subject: str, usage: TokenUsage) -> None: ...
    def used(self, subject: str) -> TokenUsage: ...  # 현재 KST 일 윈도 누적
    def reset_at(self) -> datetime: ...  # 다음 KST 자정(tz-aware)


class InMemoryQuotaStore:
    """단일 박스용 인메모리 카운터: {(yyyymmdd, subject): TokenUsage}.

    - 지난 일자 키는 접근(add/used) 시 lazy 삭제 → 자정 경과 시 used() 가 0 으로 리셋.
    - subject 수 상한(기본 10,000) 초과 시 LRU 제거 — 임의 문자열 대량 전송에 의한
      메모리 고갈 방지(InMemorySessionStore 의 TTL/LRU 결정과 동형).
    - clock 주입으로 실제 시간 경과 없이 일 경계(TTL)를 테스트한다.
    """

    def __init__(
        self,
        *,
        max_subjects: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max = max_subjects
        self._clock = clock
        self._data: "OrderedDict[tuple[str, str], TokenUsage]" = OrderedDict()
        self._lock = threading.Lock()

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=_KST).strftime("%Y%m%d")

    def _purge_stale(self, today: str) -> None:
        """오늘이 아닌(지난/미래 일자) 키를 제거 — lazy 정리."""
        stale = [k for k in self._data if k[0] != today]
        for k in stale:
            del self._data[k]

    def add(self, subject: str, usage: TokenUsage) -> None:
        today = self._day()
        with self._lock:
            self._purge_stale(today)
            key = (today, subject)
            cur = self._data.get(key)
            self._data[key] = (cur + usage) if cur is not None else usage
            self._data.move_to_end(key)  # 최근 접근 → LRU 뒤로
            while len(self._data) > self._max:
                self._data.popitem(last=False)  # 가장 오래된 subject 제거

    def used(self, subject: str) -> TokenUsage:
        today = self._day()
        with self._lock:
            self._purge_stale(today)
            key = (today, subject)
            cur = self._data.get(key)
            if cur is None:
                return TokenUsage()
            self._data.move_to_end(key)
            return cur

    def reset_at(self) -> datetime:
        now = datetime.fromtimestamp(self._clock(), tz=_KST)
        return _next_kst_midnight(now)


class RedisQuotaStore:
    """멀티워커 공유 카운터: HINCRBY quota:{YYYYMMDD}:{subject} {field} + EXPIRE 48h.

    - 일자가 키에 박혀 있어 자정 경과 시 새 키를 읽어 자연 리셋(옛 키는 TTL 로 소멸).
    - Redis 유실/재시작 시 카운터 소실은 사용자에게 유리한 방향 → 수용(설계문서 §5).
    - client 는 생성자 주입(fakeredis 로 무서버 테스트). decode_responses=True 전제
      (make_quota_store 및 concurrency.py:130 과 동형).
    """

    _FIELDS = ("input", "output", "cache_read", "cache_creation", "turns")
    _TTL_SECONDS = 172_800  # 48h — 자정 리셋보다 넉넉히 잡아 경계 조회 안전

    def __init__(self, client) -> None:
        self._r = client

    def _key(self, subject: str) -> str:
        return f"quota:{_today_kst()}:{subject}"

    def add(self, subject: str, usage: TokenUsage) -> None:
        values = {
            "input": usage.input,
            "output": usage.output,
            "cache_read": usage.cache_read,
            "cache_creation": usage.cache_creation,
            "turns": usage.turns,
        }
        if not any(values.values()):
            return  # 0 증분은 빈 키를 만들 필요 없음(토큰·턴 모두 0)
        key = self._key(subject)
        pipe = self._r.pipeline()
        for field, amount in values.items():
            if amount:
                pipe.hincrby(key, field, amount)
        pipe.expire(key, self._TTL_SECONDS)
        pipe.execute()

    def used(self, subject: str) -> TokenUsage:
        data = self._r.hgetall(self._key(subject)) or {}

        def g(field: str) -> int:
            # decode_responses=True 면 str 키, 아니면 bytes 키 — 양쪽 방어.
            v = data.get(field)
            if v is None:
                v = data.get(field.encode())
            return int(v or 0)

        return TokenUsage(
            input=g("input"),
            output=g("output"),
            cache_read=g("cache_read"),
            cache_creation=g("cache_creation"),
            turns=g("turns"),
        )

    def reset_at(self) -> datetime:
        return _next_kst_midnight(_now_kst())


def make_quota_store(redis_url: str = "") -> QuotaStore:
    """REDIS_URL 있으면 Redis(멀티워커 공유), 없으면 인메모리 —
    make_session_lock(concurrency.py:126)과 동형의 팩토리 seam."""
    if redis_url:
        import redis  # 지연 import — Redis 미사용 환경에선 의존성 불필요(concurrency.py:129 동형)

        client = redis.from_url(redis_url, decode_responses=True)
        return RedisQuotaStore(client)
    return InMemoryQuotaStore()


def quota_subject(user_id: str, session_id: str, scope: str = "user") -> str:
    """쿼터 집계 대상(subject) 결정.

    - scope="user"(기본): user_id 있으면 "u:{uid}", 없으면 "s:{sid}" 폴백.
    - scope="session": 항상 "s:{sid}".
    식별자는 [^A-Za-z0-9_-] 제거 + 64자 절단. 정규화 후 빈 값이면 "anon".
    (회원 인증 도입 시 "m:{member_id}" 접두사 추가 — 포맷 불변으로 확장.)
    """
    if scope == "session":
        sid = _safe_id(session_id)
        return f"s:{sid}" if sid else "anon"
    # scope == "user" (기본)
    uid = _safe_id(user_id)
    if uid:
        return f"u:{uid}"
    sid = _safe_id(session_id)
    return f"s:{sid}" if sid else "anon"
