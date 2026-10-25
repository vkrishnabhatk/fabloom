"""What keeps a public demo from spending the credits: a per-visitor rate limit and a global
daily spend cap. Both are in-process; the demo is one process on one VM."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fabloom.agent import read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.contracts.run import RunFinished

MAX_TRACKED_VISITORS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token bucket per visitor: ``capacity`` runs at once, one more every ``refill_s``."""

    def __init__(
        self, capacity: int, refill_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._capacity = capacity
        self._rate = 1.0 / refill_s
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, key: str, now: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= MAX_TRACKED_VISITORS:
                # Full buckets carry no state worth keeping; dropping them bounds memory.
                self._buckets = {
                    k: v for k, v in self._buckets.items() if self._level(v, now) < self._capacity
                }
            b = self._buckets[key] = _Bucket(float(self._capacity), now)
        return b

    def _level(self, b: _Bucket, now: float) -> float:
        return min(self._capacity, b.tokens + (now - b.updated) * self._rate)

    def allow(self, key: str) -> bool:
        now = self._clock()
        b = self._bucket(key, now)
        b.tokens, b.updated = self._level(b, now), now
        if b.tokens < 1:
            return False
        b.tokens -= 1
        return True

    def retry_after_s(self, key: str) -> int:
        b = self._buckets.get(key)
        if b is None:
            return 0
        missing = 1 - self._level(b, self._clock())
        return max(0, int(missing / self._rate) + 1)


def spent_today(runs_dir: Path, today: date) -> float:
    """Model spend of runs that finished today (UTC), from their event logs.

    A restart must not reset the cap, so the starting point is read from disk.
    """
    total = 0.0
    for events in runs_dir.glob(f"*/{EVENTS_FILE}"):
        for e in read_events(events):
            if isinstance(e, RunFinished) and e.ts.astimezone(UTC).date() == today:
                total += e.usage.cost_usd
    return total


class SpendBreaker:
    """Global daily cap. A run reserves its worst case (``RunBudget.max_usd``) before it
    starts and settles at its real cost, so concurrent runs can never jointly overshoot."""

    def __init__(
        self,
        cap_usd: float,
        spent_usd: float = 0.0,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.cap_usd = cap_usd
        self._today = today
        self._day = today()
        self._spent = spent_usd
        self._reserved = 0.0

    def _roll(self) -> None:
        if (d := self._today()) != self._day:
            self._day, self._spent = d, 0.0

    @property
    def spent_usd(self) -> float:
        self._roll()
        return self._spent

    @property
    def tripped(self) -> bool:
        self._roll()
        return self._spent >= self.cap_usd

    def reserve(self, worst_case_usd: float) -> bool:
        self._roll()
        if self._spent + self._reserved + worst_case_usd > self.cap_usd:
            return False
        self._reserved += worst_case_usd
        return True

    def settle(self, worst_case_usd: float, actual_usd: float) -> None:
        self._roll()
        self._reserved = max(0.0, self._reserved - worst_case_usd)
        self._spent += actual_usd
