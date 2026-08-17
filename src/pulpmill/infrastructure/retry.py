"""Retry policy.

Kept separate from the HTTP client so the decision logic -- *should* this be
retried, and *how long* until the next attempt -- can be tested without a
socket, and reused by any future non-HTTP external call.

Jitter comes from an injectable `Random`, so a seeded instance produces a fully
deterministic backoff sequence in tests while production stays spread out.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from pulpmill.config.models import RetryConfig
from pulpmill.infrastructure.clock import utc_now


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Whether to retry, how long to wait, and why."""

    should_retry: bool
    delay_seconds: float
    reason: str

    @classmethod
    def stop(cls, reason: str) -> RetryDecision:
        return cls(should_retry=False, delay_seconds=0.0, reason=reason)


def parse_retry_after(value: str, *, now_epoch: float | None = None) -> float | None:
    """Interpret a `Retry-After` header.

    The header is either a delay in seconds or an HTTP-date. Returns seconds to
    wait, or None if the value is unparseable. A date in the past yields 0.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    reference = now_epoch if now_epoch is not None else utc_now().timestamp()
    return max(0.0, when.timestamp() - reference)


class RetryPolicy:
    """Exponential backoff with jitter, honouring `Retry-After`."""

    def __init__(self, config: RetryConfig, *, rng: random.Random | None = None) -> None:
        self._config = config
        self._rng = rng or random.Random()

    @property
    def max_attempts(self) -> int:
        return self._config.max_attempts

    def backoff_for(self, attempt: int) -> float:
        """Base delay before attempt number `attempt` (1-indexed, uncapped by jitter)."""
        if attempt < 1:
            raise ValueError("attempt is 1-indexed")
        raw = self._config.initial_backoff_seconds * (self._config.multiplier ** (attempt - 1))
        capped = min(raw, self._config.max_backoff_seconds)
        if self._config.jitter_ratio <= 0:
            return capped
        spread = capped * self._config.jitter_ratio
        # Symmetric jitter around the capped delay, clamped at zero.
        return max(0.0, capped + self._rng.uniform(-spread, spread))

    def on_status(
        self,
        status_code: int,
        *,
        attempt: int,
        headers: Mapping[str, str] | None = None,
    ) -> RetryDecision:
        """Decide what to do about an HTTP status code.

        Only statuses listed in `retry_on_status` are retried. Everything else,
        including 401/403/404, fails immediately -- retrying an auth or
        not-found error is pure noise against the source.
        """
        if status_code not in self._config.retry_on_status:
            return RetryDecision.stop(f"status {status_code} is not retryable")
        if attempt >= self._config.max_attempts:
            return RetryDecision.stop(f"exhausted {self._config.max_attempts} attempts")

        delay = self.backoff_for(attempt)
        reason = f"retryable status {status_code}"

        if self._config.respect_retry_after and headers:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                requested = parse_retry_after(raw)
                if requested is not None:
                    if requested > self._config.max_retry_after_seconds:
                        return RetryDecision.stop(
                            f"server asked for {requested:.0f}s, above the "
                            f"{self._config.max_retry_after_seconds:.0f}s ceiling"
                        )
                    # Never undercut what the server asked for.
                    delay = max(delay, requested)
                    reason = f"retryable status {status_code}, Retry-After honoured"

        return RetryDecision(should_retry=True, delay_seconds=delay, reason=reason)

    def on_exception(self, exc: BaseException, *, attempt: int) -> RetryDecision:
        """Decide what to do about a transport-level exception.

        The caller is responsible for only passing exceptions it considers
        transient (timeouts, connection resets); this method just applies the
        attempt budget.
        """
        if attempt >= self._config.max_attempts:
            return RetryDecision.stop(f"exhausted {self._config.max_attempts} attempts")
        return RetryDecision(
            should_retry=True,
            delay_seconds=self.backoff_for(attempt),
            reason=f"transport error {type(exc).__name__}",
        )
