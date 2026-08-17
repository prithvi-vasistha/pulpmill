"""Ranking signals.

Each signal is independently testable and independently weighted. Adding one is
a new module plus an entry in `default_signals()` and a weight in config.
"""

from pulpmill.ranking.signals.base import RankingSignal, ScoringContext, clamp, saturating
from pulpmill.ranking.signals.engagement import CommentActivitySignal, EngagementSignal
from pulpmill.ranking.signals.length import LengthSignal
from pulpmill.ranking.signals.narrative import NarrativeSuitabilitySignal, SourceQualitySignal
from pulpmill.ranking.signals.novelty import NoveltySignal
from pulpmill.ranking.signals.recency import RecencySignal


def default_signals() -> tuple[RankingSignal, ...]:
    """The signal set the engine uses unless told otherwise.

    Order is irrelevant to the result -- weights decide -- but it is kept stable
    so stored explanations read consistently.
    """
    return (
        EngagementSignal(),
        RecencySignal(),
        CommentActivitySignal(),
        NarrativeSuitabilitySignal(),
        LengthSignal(),
        NoveltySignal(),
        SourceQualitySignal(),
    )


__all__ = [
    "CommentActivitySignal",
    "EngagementSignal",
    "LengthSignal",
    "NarrativeSuitabilitySignal",
    "NoveltySignal",
    "RankingSignal",
    "RecencySignal",
    "ScoringContext",
    "SourceQualitySignal",
    "clamp",
    "default_signals",
    "saturating",
]
