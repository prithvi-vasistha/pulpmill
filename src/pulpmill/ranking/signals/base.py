"""The ranking signal contract.

A signal is a pure function of `ScoringContext` -> `SignalScore`. No clock, no
database, no network -- everything time- or corpus-dependent arrives in the
context. That is what makes ranking reproducible: the same story, config,
reference time and corpus always give the same number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from pulpmill.config.models import AppConfig
from pulpmill.domain.ranking import SignalScore
from pulpmill.domain.story import Story
from pulpmill.persistence.repositories.stories import NoveltyEntry


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything a signal is allowed to look at."""

    story: Story
    config: AppConfig
    #: The "now" used for every age calculation in this ranking pass. Passed in
    #: rather than read from a clock so a ranking can be recomputed exactly.
    reference_time: datetime
    #: Recently discovered stories, for novelty comparison. Bounded and
    #: deterministically ordered by the repository.
    novelty_corpus: Sequence[NoveltyEntry] = field(default_factory=tuple)

    @property
    def age_hours(self) -> float:
        """Story age in hours, floored at zero.

        Sources occasionally report timestamps a few seconds in the future;
        a negative age would make recency scoring nonsense.
        """
        delta = (self.reference_time - self.story.created_at).total_seconds()
        return max(0.0, delta / 3600.0)


@runtime_checkable
class RankingSignal(Protocol):
    @property
    def name(self) -> str:
        """Key used in weights, component scores and the stored explanation."""
        ...

    def score(self, context: ScoringContext) -> SignalScore: ...


def saturating(value: float, reference: float) -> float:
    """Map an unbounded count onto [0, 1), reaching 0.5 at `reference`.

    Used instead of a hard cap so an exceptional post keeps scoring above a
    merely good one, without any single axis running away with the total.
    """
    if reference <= 0 or value <= 0:
        return 0.0
    return value / (value + reference)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
