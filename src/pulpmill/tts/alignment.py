"""Deriving word timings without a forced aligner.

Kokoro does not emit word alignment, and neither does any other fast local
synthesiser. The usual answer is to run a second model (WhisperX and friends)
over the audio afterwards, which costs another GPU model, another dependency,
and a non-deterministic result.

The approach here is different and gets most of the accuracy for none of that:

* **Synthesise one clip per sentence.** Sentence boundaries then come from the
  measured length of each clip, so they are *exact* rather than estimated. This
  is the part that matters -- a caption that changes at the wrong sentence is
  obvious, a word highlighted 80ms early is not.
* **Distribute words inside a sentence by weight.** Longer words take longer to
  say, and a word followed by a comma is followed by a pause. Weighting by
  character count plus a punctuation bonus tracks real speech closely enough for
  word highlighting.

The result is deterministic: the same text and the same clip length always
produce the same timings. `ForcedAligner` is deliberately left as a seam --
when word-level precision matters more than it does today, it slots in here
without the caption or render stages changing.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from pulpmill.domain.media import WordTiming

#: Extra weight for the pause a mark implies. Tuned by ear against Kokoro
#: output: a comma is a beat, a full stop is closer to two.
_PAUSE_WEIGHTS = {",": 1.5, ";": 2.0, ":": 1.5, ".": 2.5, "!": 2.5, "?": 2.5, "—": 1.5}
_TRAILING_PUNCT = re.compile(r"[^\w\s]+$")


def word_weight(word: str) -> float:
    """Relative time a word occupies, before scaling to the real duration."""
    base = float(len(word.strip()))
    match = _TRAILING_PUNCT.search(word)
    if match:
        for mark in match.group(0):
            base += _PAUSE_WEIGHTS.get(mark, 0.0)
    # A one-character word still takes measurable time to say.
    return max(base, 1.5)


def distribute_words(text: str, *, start: float, end: float) -> tuple[WordTiming, ...]:
    """Spread the words of one utterance across a known time span.

    `start` and `end` are measured from the synthesised clip, so the span is
    correct even when the estimate for any individual word is not.
    """
    words = text.split()
    if not words:
        return ()
    span = end - start
    if span <= 0:
        raise ValueError(f"cannot distribute {len(words)} words across a {span:.3f}s span")

    weights = [word_weight(word) for word in words]
    total = sum(weights)

    timings: list[WordTiming] = []
    elapsed = start
    for index, (word, weight) in enumerate(zip(words, weights, strict=True)):
        share = span * (weight / total)
        # The last word absorbs any rounding drift, so the final timing lands
        # exactly on `end` rather than a few milliseconds short of it.
        finish = end if index == len(words) - 1 else elapsed + share
        timings.append(
            WordTiming(
                word=word,
                start_seconds=round(elapsed, 4),
                end_seconds=round(finish, 4),
            )
        )
        elapsed = finish
    return tuple(timings)


def concatenate_timings(
    segments: list[tuple[str, float]],
    *,
    gaps: list[float] | None = None,
) -> tuple[tuple[WordTiming, ...], float]:
    """Lay out per-utterance clips on one timeline.

    `segments` is `(text, duration_seconds)` in playback order; `gaps` is the
    silence inserted *after* each segment. Returns the word timings for the
    whole track and its total duration.
    """
    if gaps is not None and len(gaps) != len(segments):
        raise ValueError("gaps must be parallel to segments")

    timings: list[WordTiming] = []
    offset = 0.0
    for index, (text, duration) in enumerate(segments):
        if duration > 0:
            timings.extend(distribute_words(text, start=offset, end=offset + duration))
        offset += duration
        offset += gaps[index] if gaps else 0.0
    return tuple(timings), offset


@runtime_checkable
class ForcedAligner(Protocol):
    """Seam for a real aligner, if word-level precision ever justifies one.

    Nothing implements this today, and nothing depends on it. It exists so that
    adding one is a new module rather than a change to the synthesis, caption
    and render stages.
    """

    @property
    def name(self) -> str: ...

    def align(self, audio_path: str, text: str) -> tuple[WordTiming, ...]: ...
