"""The canonical story model and its provenance guarantees."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pulpmill.domain.enums import DedupLayer, StoryStatus

#: Fixed namespace for deterministic story identifiers. Never change this: it
#: would re-key every story already in the database.
STORY_ID_NAMESPACE = uuid.UUID("6f9d9c2e-0f3e-5a5b-9c1a-2b7f1c4e8d10")

_EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


def build_story_id(source_platform: str, source_id: str) -> str:
    """Derive a story's primary key from its origin.

    Deterministic by construction: the same upstream post always produces the
    same id, on any machine, in any run. That is what makes re-scraping a post
    an update rather than a duplicate insert.
    """
    if not source_platform:
        raise ValueError("source_platform is required to build a story id")
    if not source_id:
        raise ValueError("source_id is required to build a story id")
    return str(uuid.uuid5(STORY_ID_NAMESPACE, f"{source_platform}:{source_id}"))


@dataclass(frozen=True, slots=True)
class Engagement:
    """Platform-agnostic engagement counters.

    Every field is optional because platforms genuinely differ: Reddit has a
    score, 4chan does not. Ranking must treat `None` as "this platform does not
    report this", never as zero -- conflating the two would push every 4chan
    thread to the bottom of the engagement axis for a metric it cannot emit.
    """

    score: int | None = None
    comments: int | None = None
    reactions: int | None = None
    shares: int | None = None
    views: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "score": self.score,
            "comments": self.comments,
            "reactions": self.reactions,
            "shares": self.shares,
            "views": self.views,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Engagement:
        def as_int(key: str) -> int | None:
            value = data.get(key)
            return None if value is None else int(value)

        return cls(
            score=as_int("score"),
            comments=as_int("comments"),
            reactions=as_int("reactions"),
            shares=as_int("shares"),
            views=as_int("views"),
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """The subset of a story that must survive every downstream transformation.

    A rendered video must be traceable back through part -> story -> source ->
    original URL, for attribution, debugging and content auditing. Any stage
    that produces a derived artifact carries this forward unchanged.
    """

    source_platform: str
    source_id: str
    canonical_url: str
    author: str | None
    title: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_platform": self.source_platform,
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "author": self.author,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class RawStory:
    """An unprocessed record straight from a source adapter's `fetch`.

    Holds the source's own payload verbatim. Normalization reads from it; the
    payload itself is treated as untrusted input and is never evaluated,
    rendered as markup, or interpolated into a shell or SQL string.
    """

    source_platform: str
    source_id: str
    canonical_url: str
    fetched_at: datetime
    payload: Mapping[str, Any]
    #: How this record was acquired (endpoint, query, page) -- for debugging.
    retrieval: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_METADATA)


@dataclass(frozen=True, slots=True)
class Story:
    """The canonical normalized story.

    Immutable: every mutation returns a new instance via `evolve`. That keeps
    accidental in-place edits of provenance impossible and makes the pipeline's
    data flow easy to reason about.
    """

    id: str
    source_platform: str
    source_id: str
    canonical_url: str
    #: SHA-256 of the normalized canonical URL (dedup layer 2 key).
    url_fingerprint: str
    author: str | None
    title: str
    raw_content: str
    normalized_content: str
    #: SHA-256 of the normalized content (dedup layer 3 key).
    content_hash: str
    #: 64-bit SimHash, or None when the body was too short to fingerprint.
    simhash: int | None
    word_count: int
    language: str | None
    created_at: datetime
    discovered_at: datetime
    updated_at: datetime
    engagement: Engagement
    metadata: Mapping[str, Any]
    status: StoryStatus
    duplicate_of_id: str | None = None
    duplicate_layer: DedupLayer | None = None

    @property
    def provenance(self) -> Provenance:
        return Provenance(
            source_platform=self.source_platform,
            source_id=self.source_id,
            canonical_url=self.canonical_url,
            author=self.author,
            title=self.title,
        )

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of_id is not None

    def evolve(self, **changes: Any) -> Story:
        """Return a copy with `changes` applied.

        Provenance fields are rejected: they are set once at normalization and
        are not editable afterwards.
        """
        protected = {"id", "source_platform", "source_id", "canonical_url"}
        illegal = protected & changes.keys()
        if illegal:
            raise ValueError(f"provenance fields cannot be modified: {', '.join(sorted(illegal))}")
        return replace(self, **changes)
