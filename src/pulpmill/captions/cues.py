"""Grouping word timings into on-screen captions.

Short-form captions are not subtitles. A subtitle serves comprehension and can
sit on screen for four seconds; a caption here serves retention, changes every
half-second or so, and never holds more than a few words at once.

Grouping rules, in priority order:

1. **Never cross a pause.** A gap between words means a sentence ended and the
   narrator breathed. A caption spanning that boundary reads as broken sync even
   when the timings are perfect.
2. **Break after terminal punctuation.** Same reason, for pauses too short to
   detect from timing alone.
3. **Respect the word and character budgets.** Both, because four short words
   and four long ones need different line breaks.
4. **Never emit a cue below the minimum duration.** A caption that flashes for
   120ms is noise; it is merged into its neighbour instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pulpmill.config.models import CaptionConfig
from pulpmill.domain.media import CaptionCue, WordTiming

#: A silence longer than this between two words is treated as a hard break.
#: Below the configured sentence gap, so it triggers on real pauses without
#: firing on ordinary word spacing.
PAUSE_THRESHOLD_SECONDS = 0.18

_TERMINAL = re.compile(r"[.!?]['\"\)\]]?$")
_CLAUSE_END = re.compile(r"[,;:]['\"\)\]]?$")


def group_into_cues(timings: Sequence[WordTiming], config: CaptionConfig) -> tuple[CaptionCue, ...]:
    """Turn a word timeline into display cues."""
    if not timings:
        return ()

    groups: list[list[WordTiming]] = []
    current: list[WordTiming] = []

    for index, timing in enumerate(timings):
        prospective_chars = sum(len(item.word) + 1 for item in current) + len(timing.word)
        over_budget = current and (
            len(current) >= config.max_words_per_cue or prospective_chars > config.max_chars_per_cue
        )
        if over_budget:
            groups.append(current)
            current = []

        current.append(timing)

        if index + 1 < len(timings):
            gap = timings[index + 1].start_seconds - timing.end_seconds
            hard_break = gap >= PAUSE_THRESHOLD_SECONDS or _TERMINAL.search(timing.word)
            soft_break = _CLAUSE_END.search(timing.word) and len(current) >= 2
            if hard_break or soft_break:
                groups.append(current)
                current = []

    if current:
        groups.append(current)

    return _finalise(groups, config)


def _finalise(groups: list[list[WordTiming]], config: CaptionConfig) -> tuple[CaptionCue, ...]:
    """Merge sub-minimum cues, then build the immutable result."""
    merged: list[list[WordTiming]] = []
    for group in groups:
        if not group:
            continue
        duration = group[-1].end_seconds - group[0].start_seconds
        # The word budget is a preference; the minimum duration is a rule. A
        # merged cue may grow to twice the normal size to absorb a flashed one,
        # which is still far more readable than a 120ms flicker.
        fits = bool(merged) and len(merged[-1]) + len(group) <= config.max_words_per_cue * 2
        if duration < config.min_cue_seconds and fits:
            merged[-1].extend(group)
        else:
            merged.append(list(group))

    cues: list[CaptionCue] = []
    for index, group in enumerate(merged):
        start = group[0].start_seconds
        end = max(group[-1].end_seconds, start + 0.05)
        cues.append(
            CaptionCue(
                index=index,
                start_seconds=start,
                end_seconds=end,
                text=" ".join(item.word for item in group),
                words=tuple(group),
            )
        )
    return tuple(cues)


def cues_from_even_split(
    text: str, *, start: float, end: float, config: CaptionConfig
) -> tuple[CaptionCue, ...]:
    """Fallback for a provider that supplies no alignment at all.

    Worse than measured timings and honest about it: the whole point of
    per-sentence synthesis is to avoid needing this. It exists so a provider
    without alignment still produces a captioned video rather than none.
    """
    from pulpmill.tts.alignment import distribute_words

    return group_into_cues(distribute_words(text, start=start, end=end), config)
