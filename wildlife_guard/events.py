"""오탐·알림 폭주를 막는 판정 게이트 (순수 로직, 하드웨어 의존 없음).

- 연속성: 최근 `window` 프레임 중 `min_hits` 프레임 이상에서 보여야 '출현'으로 확정
- 쿨다운: 같은 동물은 `cooldown_s` 초 안에 다시 알리지 않음
"""

from __future__ import annotations

from collections import defaultdict, deque


class DetectionGate:
    def __init__(self, window: int = 5, min_hits: int = 3, cooldown_s: float = 60.0):
        if not 1 <= min_hits <= window:
            raise ValueError("1 <= min_hits <= window 이어야 합니다")
        self.window = window
        self.min_hits = min_hits
        self.cooldown_s = cooldown_s
        self._history: dict[str, deque[bool]] = defaultdict(lambda: deque(maxlen=window))
        self._last_alert: dict[str, float] = {}

    def update(self, seen_keys: set[str], now: float) -> list[str]:
        """이번 프레임에 보인 동물 key 집합을 넣으면, 지금 알려야 할 key 목록을 돌려준다."""
        for key in seen_keys:
            self._history[key]  # 처음 보는 key 도 기록 대상으로 등록
        fired: list[str] = []
        for key, hist in self._history.items():
            hist.append(key in seen_keys)
            if sum(hist) < self.min_hits:
                continue
            last = self._last_alert.get(key)
            if last is not None and now - last < self.cooldown_s:
                continue
            self._last_alert[key] = now
            fired.append(key)
        return fired
