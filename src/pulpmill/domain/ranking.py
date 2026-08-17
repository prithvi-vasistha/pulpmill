"""Ranking value objects.

A ranking is only useful if you can see why a story scored what it did, so every
signal returns its normalized value *and* the raw evidence behind it. All of it
is persisted alongside the final score.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pulpmill.domain.story import Story


@dataclass(frozen=True, slots=True)
class SignalScore:
    """One ranking signal's verdict on one story.

    `value` is always in [0, 1] so weights are directly comparable. `detail`
    holds the inputs that produced it -- word counts, ages, matched cues -- which
    is what makes a score auditable rather than a magic number.
    """

    name: str
    value: float
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: False when the platform cannot supply the inputs this signal needs. The
    #: engine redistributes an unavailable signal's weight across the rest
    #: instead of scoring it zero.
    available: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"signal {self.name!r} produced out-of-range value {self.value!r}")


@dataclass(frozen=True, slots=True)
class RankingResult:
    """The complete, reproducible outcome of ranking one story."""

    story_id: str
    #: Scoring-behaviour version from config. Bump on any behaviour change.
    ranking_version: str
    #: SHA-256 over the canonicalized ranking config. Together with
    #: `ranking_version` this identifies exactly how the score was produced.
    config_fingerprint: str
    #: Final score on a 0-100 scale.
    final_score: float
    component_scores: Mapping[str, float]
    #: Weights actually applied, after redistributing unavailable signals.
    effective_weights: Mapping[str, float]
    #: Per-signal evidence, keyed by signal name.
    explanation: Mapping[str, Any]
    #: The "now" used for age-dependent signals. Stored so the ranking can be
    #: recomputed identically later.
    reference_time: datetime
    ranked_at: datetime


@dataclass(frozen=True, slots=True)
class RankedStory:
    """A story joined to its ranking, as returned by candidate queries."""

    story: Story
    ranking: RankingResult
