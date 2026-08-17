"""Time access behind an interface.

Nothing in the pipeline calls `datetime.now()` or `time.sleep()` directly.
Everything that depends on time takes a `Clock`, which is what makes ranking
deterministic under test and backoff testable without actually waiting.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary epoch, immune to system clock changes."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for `seconds`. A non-positive value returns immediately."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


SYSTEM_CLOCK: Clock = SystemClock()


def utc_now() -> datetime:
    """Convenience for code that legitimately has no injected clock."""
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Serialise to a sortable ISO-8601 UTC string ending in `Z`.

    Sortable matters: timestamps are stored as TEXT, and lexicographic ordering
    is what lets SQLite use a plain index for time-ordered queries.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    """Parse a timestamp written by `to_iso`, or any ISO-8601 string.

    Naive inputs are assumed to be UTC; the result is always aware.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def from_epoch(seconds: float) -> datetime:
    """Convert a Unix timestamp (used by Reddit and 4chan) to aware UTC."""
    return datetime.fromtimestamp(seconds, tz=UTC)
