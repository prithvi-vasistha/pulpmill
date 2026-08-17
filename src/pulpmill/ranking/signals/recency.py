"""Recency signal."""

from __future__ import annotations

from pulpmill.domain.ranking import SignalScore
from pulpmill.ranking.signals.base import ScoringContext


class RecencySignal:
    """Exponential decay on story age.

    Half-life rather than a linear ramp, because attention to a story decays
    multiplicatively. Past `max_age_hours` the contribution is exactly zero, so
    an ancient story cannot ride a large engagement count into the candidate
    list forever.
    """

    name = "recency"

    def score(self, context: ScoringContext) -> SignalScore:
        config = context.config.ranking.recency
        age_hours = context.age_hours

        if age_hours >= config.max_age_hours:
            return SignalScore(
                name=self.name,
                value=0.0,
                detail={
                    "age_hours": round(age_hours, 3),
                    "max_age_hours": config.max_age_hours,
                    "reason": "older than max_age_hours",
                },
            )

        value = 0.5 ** (age_hours / config.half_life_hours)
        return SignalScore(
            name=self.name,
            value=max(0.0, min(1.0, value)),
            detail={
                "age_hours": round(age_hours, 3),
                "half_life_hours": config.half_life_hours,
                "max_age_hours": config.max_age_hours,
                "created_at": context.story.created_at.isoformat(),
                "reference_time": context.reference_time.isoformat(),
            },
        )
