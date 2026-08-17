"""Length signal."""

from __future__ import annotations

from pulpmill.domain.ranking import SignalScore
from pulpmill.ranking.signals.base import ScoringContext


class LengthSignal:
    """Trapezoid response over word count.

    Short-form narration has a real usable band. Below `floor_words` there is
    not enough story; above `ceiling_words` it stops being a single video. The
    plateau between `ideal_min_words` and `ideal_max_words` scores 1.0, with
    linear ramps on either side, so a story just outside the band is penalised
    proportionally rather than discarded.

    Word counts above the plateau are not fatal on purpose: those are the
    stories the series splitter will eventually cut into numbered parts.
    """

    name = "length"

    def score(self, context: ScoringContext) -> SignalScore:
        config = context.config.ranking.length
        words = context.story.word_count

        if words <= config.floor_words or words >= config.ceiling_words:
            value = 0.0
            band = "below_floor" if words <= config.floor_words else "above_ceiling"
        elif words < config.ideal_min_words:
            span = config.ideal_min_words - config.floor_words
            value = (words - config.floor_words) / span
            band = "ramp_up"
        elif words <= config.ideal_max_words:
            value = 1.0
            band = "ideal"
        else:
            span = config.ceiling_words - config.ideal_max_words
            value = (config.ceiling_words - words) / span
            band = "ramp_down"

        return SignalScore(
            name=self.name,
            value=max(0.0, min(1.0, value)),
            detail={
                "word_count": words,
                "band": band,
                "floor_words": config.floor_words,
                "ideal_min_words": config.ideal_min_words,
                "ideal_max_words": config.ideal_max_words,
                "ceiling_words": config.ceiling_words,
                # ~150 words/minute is a typical narration pace; this is a
                # planning aid, not an input to the score.
                "estimated_narration_seconds": round(words / 150.0 * 60.0, 1),
            },
        )
