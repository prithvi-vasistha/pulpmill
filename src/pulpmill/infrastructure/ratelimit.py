"""Client-side rate limiting.

A token bucket per source, enforced before every request. This is the difference
between a system that runs for months and one that gets blocked in an hour: the
limit is respected by construction rather than hoped for.

The bucket also accepts external pressure -- `pause_until` lets an adapter that
saw a `429` or a low `X-Ratelimit-Remaining` header stall the bucket without
reaching into its internals.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock


@dataclass(slots=True)
class TokenBucket:
    """Classic token bucket over a monotonic clock.

    Thread-safe: a single bucket is shared by every request to one host, so
    bounded concurrency later cannot accidentally multiply the request rate.
    """

    rate_per_second: float
    capacity: int
    clock: Clock = SYSTEM_CLOCK

    _tokens: float = 0.0
    _last_refill: float = 0.0
    _blocked_until: float = 0.0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if self.capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._tokens = float(self.capacity)
        self._last_refill = self.clock.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        if elapsed:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
            self._last_refill = now

    def time_until_available(self) -> float:
        """Seconds to wait before a token is available. 0 means go now."""
        with self._lock:
            now = self.clock.monotonic()
            self._refill_locked(now)
            hold = max(0.0, self._blocked_until - now)
            if self._tokens >= 1.0:
                return hold
            deficit = 1.0 - self._tokens
            return max(hold, deficit / self.rate_per_second)

    def acquire(self) -> float:
        """Block until a token is available, then consume it.

        Returns the number of seconds spent waiting, for logging.
        """
        waited = 0.0
        while True:
            delay = self.time_until_available()
            if delay <= 0:
                with self._lock:
                    now = self.clock.monotonic()
                    self._refill_locked(now)
                    if self._tokens >= 1.0 and now >= self._blocked_until:
                        self._tokens -= 1.0
                        return waited
                # Lost the race against another thread; loop and recompute.
                continue
            self.clock.sleep(delay)
            waited += delay

    def pause_for(self, seconds: float) -> None:
        """Refuse to hand out tokens for `seconds`.

        Used when a server tells us to back off (429 / Retry-After) so the
        pressure applies to the whole source, not just the failed request.
        """
        if seconds <= 0:
            return
        with self._lock:
            self._blocked_until = max(self._blocked_until, self.clock.monotonic() + seconds)

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill_locked(self.clock.monotonic())
            return self._tokens
