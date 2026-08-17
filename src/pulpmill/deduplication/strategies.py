"""Deduplication strategies, cheapest first.

Each layer answers one question and the engine stops at the first match:

1. `ExactSourceStrategy`  -- have we already got this exact post?
2. `CanonicalUrlStrategy` -- does another story point at the same URL?
3. `ContentHashStrategy`  -- is the body byte-identical after flattening?
4. `SimHashStrategy`      -- is the body *nearly* identical?

Layers 2-4 are what catch cross-source duplicates: the same viral story posted
to Reddit and reposted to 4chan is one story with two URLs, and the pipeline
should render it once.

A future embedding-based layer implements this same `DedupStrategy` protocol and
is appended after layer 4. Nothing about the engine changes -- which is the
point of keeping the semantic layer isolated rather than assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pulpmill.domain.enums import DedupLayer
from pulpmill.domain.story import Story
from pulpmill.normalization.hashing import hamming_distance, simhash_bands
from pulpmill.persistence.repositories.stories import StoryRepository


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    """A match: this story duplicates `original_id`, found by `layer`."""

    layer: DedupLayer
    original_id: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DedupStrategy(Protocol):
    """One deduplication layer."""

    @property
    def layer(self) -> DedupLayer: ...

    def check(self, story: Story) -> DuplicateVerdict | None:
        """Return a verdict, or None if this layer finds no match."""
        ...


class ExactSourceStrategy:
    """Layer 1: the same `(source_platform, source_id)` we already hold.

    A match here means "already known", not "duplicate of a different story":
    story ids are derived from the source pair, so the match is the same row.
    The engine treats it differently for exactly that reason.
    """

    def __init__(self, repository: StoryRepository) -> None:
        self._repository = repository

    @property
    def layer(self) -> DedupLayer:
        return DedupLayer.EXACT_SOURCE

    def check(self, story: Story) -> DuplicateVerdict | None:
        existing = self._repository.find_by_source(story.source_platform, story.source_id)
        if existing is None:
            return None
        return DuplicateVerdict(
            layer=self.layer,
            original_id=existing.id,
            detail={
                "source_platform": story.source_platform,
                "source_id": story.source_id,
                "known_since": existing.discovered_at.isoformat(),
            },
        )


class CanonicalUrlStrategy:
    """Layer 2: another story with the same normalized URL.

    Matching is on `url_fingerprint`, so tracking parameters, `old.` vs `www.`
    hosts and trailing slashes do not create false distinctions. The stored
    `canonical_url` itself is never touched.
    """

    def __init__(self, repository: StoryRepository) -> None:
        self._repository = repository

    @property
    def layer(self) -> DedupLayer:
        return DedupLayer.CANONICAL_URL

    def check(self, story: Story) -> DuplicateVerdict | None:
        match = self._repository.find_by_url_fingerprint(story.url_fingerprint, exclude_id=story.id)
        if match is None:
            return None
        return DuplicateVerdict(
            layer=self.layer,
            original_id=match.id,
            detail={
                "url_fingerprint": story.url_fingerprint,
                "original_url": match.canonical_url,
                "original_platform": match.source_platform,
            },
        )


class ContentHashStrategy:
    """Layer 3: another story whose flattened body hashes identically.

    Cross-platform by design: this is the layer that notices a Reddit post
    copy-pasted onto 4chan.
    """

    def __init__(self, repository: StoryRepository) -> None:
        self._repository = repository

    @property
    def layer(self) -> DedupLayer:
        return DedupLayer.CONTENT_HASH

    def check(self, story: Story) -> DuplicateVerdict | None:
        match = self._repository.find_by_content_hash(story.content_hash, exclude_id=story.id)
        if match is None:
            return None
        return DuplicateVerdict(
            layer=self.layer,
            original_id=match.id,
            detail={
                "content_hash": story.content_hash,
                "original_platform": match.source_platform,
                "original_url": match.canonical_url,
            },
        )


class SimHashStrategy:
    """Layer 4: a near-identical body, by SimHash Hamming distance.

    Candidates come from the banded LSH index, so this is an indexed lookup
    rather than a scan over every story ever seen -- which is what makes it
    affordable to run on every ingested story, forever.

    Explicitly *not* semantic similarity. It catches lightly-edited reposts
    (changed names, added intro, reformatted), not two different tellings of the
    same events. That remains future work behind this same interface.
    """

    def __init__(
        self,
        repository: StoryRepository,
        *,
        threshold: int,
        band_count: int,
        min_tokens: int,
        candidate_limit: int = 50,
    ) -> None:
        self._repository = repository
        self._threshold = threshold
        self._band_count = band_count
        self._min_tokens = min_tokens
        self._candidate_limit = candidate_limit

    @property
    def layer(self) -> DedupLayer:
        return DedupLayer.NEAR_DUPLICATE

    def check(self, story: Story) -> DuplicateVerdict | None:
        if story.simhash is None:
            # Body was too short to fingerprint stably; no verdict rather than a
            # guess. `simhash_min_tokens` is applied when the story is built.
            return None

        bands = simhash_bands(story.simhash, self._band_count)
        candidates = self._repository.find_simhash_candidates(
            bands, exclude_id=story.id, limit=self._candidate_limit
        )

        best: tuple[int, Story] | None = None
        for candidate in candidates:
            if candidate.simhash is None:
                continue
            distance = hamming_distance(story.simhash, candidate.simhash)
            if distance <= self._threshold and (best is None or distance < best[0]):
                best = (distance, candidate)

        if best is None:
            return None

        distance, match = best
        return DuplicateVerdict(
            layer=self.layer,
            original_id=match.id,
            detail={
                "hamming_distance": distance,
                "threshold": self._threshold,
                "candidates_examined": len(candidates),
                "original_platform": match.source_platform,
                "original_url": match.canonical_url,
            },
        )
