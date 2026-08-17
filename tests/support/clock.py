"""A clock that never actually waits.

Everything time-dependent in the application takes a `Clock`, so substituting
this makes backoff, rate limiting and recency scoring fully deterministic and
instantaneous under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class ManualClock:
    """Implements the `Clock` protocol with time under the test's control."""

    def __init__(
        self,
        now: datetime | None = None,
        *,
        monotonic: float = 1000.0,
    ) -> None:
        self._now = now or datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        self._monotonic = monotonic
        #: Every sleep duration requested, in order. Lets a test assert on the
        #: backoff schedule without waiting for it.
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)
