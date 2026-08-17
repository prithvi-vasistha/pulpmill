"""The ranking engine.

Deterministic by construction. Given the same story, the same configuration,
the same `ranking.version` and the same reference time, this produces the same
score -- no randomness, no wall-clock reads, no network, no model.

The engine's own contribution is small and explicit: collect signal values,
redistribute the weight of any signal a platform cannot supply, combine, and
record everything that went into the number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pulpmill.config.models import AppConfig
from pulpmill.domain.ranking import RankingResult, SignalScore
from pulpmill.domain.story import Story
from pulpmill.persistence.repositories.stories import NoveltyEntry
from pulpmill.ranking.signals import RankingSignal, ScoringContext, default_signals

#: Final scores are reported on 0-100 rather than 0-1, purely because a table of
#: two-decimal percentages is easier to eyeball than a column of 0.7143s.
SCORE_SCALE = 100.0


class RankingEngine:
    """Combines weighted signals into one auditable score."""

    def __init__(
        self,
        config: AppConfig,
        *,
        signals: Sequence[RankingSignal] | None = None,
    ) -> None:
        self._config = config
        self._signals = tuple(signals) if signals is not None else default_signals()

        weights = config.ranking.weights.as_mapping()
        unknown = {signal.name for signal in self._signals} - weights.keys()
        if unknown:
            raise ValueError("signals without a configured weight: " + ", ".join(sorted(unknown)))
        self._weights = weights

    @property
    def version(self) -> str:
        return self._config.ranking.version

    @property
    def config_fingerprint(self) -> str:
        return self._config.ranking.fingerprint()

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(signal.name for signal in self._signals)

    def rank(
        self,
        story: Story,
        *,
        reference_time: datetime,
        novelty_corpus: Sequence[NoveltyEntry] = (),
        ranked_at: datetime | None = None,
    ) -> RankingResult:
        context = ScoringContext(
            story=story,
            config=self._config,
            reference_time=reference_time,
            novelty_corpus=novelty_corpus,
        )

        scores: list[SignalScore] = [signal.score(context) for signal in self._signals]
        effective = self._effective_weights(scores)

        final = sum(score.value * effective.get(score.name, 0.0) for score in scores)
        final_score = round(final * SCORE_SCALE, 4)

        explanation: dict[str, Any] = {
            "signals": {
                score.name: {
                    "value": round(score.value, 6),
                    "available": score.available,
                    "configured_weight": self._weights.get(score.name, 0.0),
                    "effective_weight": round(effective.get(score.name, 0.0), 6),
                    "contribution": round(
                        score.value * effective.get(score.name, 0.0) * SCORE_SCALE, 4
                    ),
                    "detail": dict(score.detail),
                }
                for score in scores
            },
            "unavailable_signals": sorted(score.name for score in scores if not score.available),
            "score_scale": SCORE_SCALE,
        }

        return RankingResult(
            story_id=story.id,
            ranking_version=self.version,
            config_fingerprint=self.config_fingerprint,
            final_score=final_score,
            component_scores={score.name: round(score.value, 6) for score in scores},
            effective_weights={name: round(weight, 6) for name, weight in effective.items()},
            explanation=explanation,
            reference_time=reference_time,
            ranked_at=ranked_at if ranked_at is not None else reference_time,
        )

    def _effective_weights(self, scores: Sequence[SignalScore]) -> Mapping[str, float]:
        """Normalize configured weights across the signals that actually applied.

        Two things happen here. Configured weights need not sum to 1.0 -- only
        their ratios matter. And a signal that reported itself unavailable gets
        weight 0, with its share spread proportionally over the rest, so a
        platform that cannot report (say) a score is not silently penalised for
        it.
        """
        available = {
            score.name: self._weights.get(score.name, 0.0) for score in scores if score.available
        }
        total = sum(available.values())

        if total <= 0:
            # Every signal was unavailable or every weight was zero. Report
            # zeros rather than inventing a score from nothing.
            return {score.name: 0.0 for score in scores}

        effective = {name: weight / total for name, weight in available.items()}
        for score in scores:
            effective.setdefault(score.name, 0.0)
        return effective
