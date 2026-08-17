"""The source adapter contract.

Everything platform-specific lives behind this interface. No stage downstream of
ingestion is allowed to branch on the platform name -- if a stage needs
per-platform behaviour it gets it from configuration (see
`config.models.SourceConfig`) or from the adapter, never from an `if source ==`
chain.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pulpmill.domain.story import RawStory, Story


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """A bounded request for stories from one source.

    Bounded is the point: `limit` and `max_pages` are hard ceilings that stop a
    24/7 worker from walking an entire subreddit into memory. Adapters must
    honour both.
    """

    #: Adapter-specific query descriptors, taken from `sources.<name>.queries`.
    queries: tuple[Mapping[str, Any], ...]
    #: Maximum stories to yield across all queries.
    limit: int
    #: Maximum pages to request per query.
    max_pages: int = 1
    #: Only yield stories created at or after this ISO timestamp, when the
    #: source can express it. Advisory: adapters filter best-effort.
    since: str | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("FetchRequest.limit must be positive")
        if self.max_pages <= 0:
            raise ValueError("FetchRequest.max_pages must be positive")


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """Whether an adapter can currently do useful work.

    Checked before fetching so an unusable source is reported clearly at the top
    of a run instead of throwing halfway through.
    """

    platform: str
    available: bool
    #: Human-readable explanation, always set when `available` is False.
    detail: str
    #: Set when the operator must do something (e.g. supply credentials).
    remediation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """Acquire and normalize stories for one platform.

    `fetch` yields lazily so the pipeline can stream: a story is normalized,
    deduplicated and persisted before the next one is pulled, which keeps peak
    memory proportional to one story rather than one run.
    """

    @property
    def platform(self) -> str:
        """Stable platform identifier, e.g. `"reddit"`. Persisted on every story."""
        ...

    def health(self) -> AdapterHealth:
        """Report whether this adapter can fetch right now. Must not raise."""
        ...

    def fetch(self, request: FetchRequest) -> Iterator[RawStory]:
        """Yield raw records, at most `request.limit` of them."""
        ...

    def normalize(self, raw: RawStory) -> Story | None:
        """Convert a raw record into a canonical story.

        Returns None when the record is valid but not usable as narration
        (too short, removed by moderators, link-only). Raises
        `SourceResponseError` when the payload is malformed.
        """
        ...

    def close(self) -> None:
        """Release network resources. Safe to call more than once."""
        ...
