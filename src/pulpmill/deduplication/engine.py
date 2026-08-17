"""The deduplication engine.

Runs before anything expensive -- before ranking, and long before script
generation or TTS. Every layer is deterministic: the same story against the same
database always produces the same verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pulpmill.config.models import DeduplicationConfig
from pulpmill.deduplication.strategies import (
    CanonicalUrlStrategy,
    ContentHashStrategy,
    DedupStrategy,
    DuplicateVerdict,
    ExactSourceStrategy,
    SimHashStrategy,
)
from pulpmill.domain.enums import DedupLayer
from pulpmill.domain.story import Story
from pulpmill.persistence.repositories.stories import StoryRepository


class DedupOutcome(StrEnum):
    """What the engine concluded about a story."""

    #: Never seen in any form. Persist it as a new story.
    NEW = "new"
    #: The same post from the same source. Refresh it; do not create anything.
    KNOWN = "known"
    #: A different story with the same content. Persist, then link and set aside.
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class DedupResult:
    outcome: DedupOutcome
    layer: DedupLayer | None = None
    original_id: str | None = None
    detail: Mapping[str, Any] | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.outcome is DedupOutcome.DUPLICATE


class DeduplicationEngine:
    """Applies enabled strategies in order and stops at the first match."""

    def __init__(
        self,
        config: DeduplicationConfig,
        repository: StoryRepository,
        *,
        extra_strategies: Sequence[DedupStrategy] = (),
    ) -> None:
        self._config = config
        self._exact: ExactSourceStrategy | None = (
            ExactSourceStrategy(repository) if config.layers.exact_source else None
        )

        strategies: list[DedupStrategy] = []
        if config.layers.canonical_url:
            strategies.append(CanonicalUrlStrategy(repository))
        if config.layers.content_hash:
            strategies.append(ContentHashStrategy(repository))

        near = config.layers.near_duplicate
        if near.enabled:
            strategies.append(
                SimHashStrategy(
                    repository,
                    threshold=near.hamming_threshold,
                    band_count=near.band_count,
                    min_tokens=near.min_tokens,
                )
            )
        # Appended last on purpose: a future semantic layer is the most
        # expensive and should only ever see what the cheap layers let through.
        strategies.extend(extra_strategies)
        self._strategies = tuple(strategies)

    @property
    def layers(self) -> tuple[str, ...]:
        names = [DedupLayer.EXACT_SOURCE.value] if self._exact else []
        names.extend(strategy.layer.value for strategy in self._strategies)
        return tuple(names)

    def evaluate(self, story: Story) -> DedupResult:
        """Classify a normalized story against everything already stored."""
        if self._exact is not None:
            known: DuplicateVerdict | None = self._exact.check(story)
            if known is not None:
                return DedupResult(
                    outcome=DedupOutcome.KNOWN,
                    layer=known.layer,
                    original_id=known.original_id,
                    detail=known.detail,
                )

        for strategy in self._strategies:
            verdict: DuplicateVerdict | None = strategy.check(story)
            if verdict is None:
                continue
            # A layer that points at the story itself is not a duplicate -- it
            # happens when re-evaluating an already-persisted story.
            if verdict.original_id == story.id:
                continue
            return DedupResult(
                outcome=DedupOutcome.DUPLICATE,
                layer=verdict.layer,
                original_id=verdict.original_id,
                detail=verdict.detail,
            )

        return DedupResult(outcome=DedupOutcome.NEW)
