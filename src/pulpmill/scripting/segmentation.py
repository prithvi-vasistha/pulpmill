"""Cutting a story into sentences, and sentences into parts.

Two jobs, both deterministic:

**Sentence splitting.** Sentences are the atom the rest of production is built
on. The TTS stage synthesises one clip per sentence and concatenates, which is
what makes clip boundaries exact instead of estimated -- so a bad split shows up
later as a caption that changes mid-word.

**Part planning.** A 2 000-word story is not one short. Boundaries are placed to
balance part lengths, then snapped to paragraph breaks where one is close by,
because a part that ends mid-scene reads as a technical failure to a viewer.

This module proposes *where* to cut. It never assigns part numbers -- that is
`pulpmill.domain.series.plan_parts`, and it stays out of reach of any model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pulpmill.domain.errors import StoryTooLongError

#: Abbreviations whose trailing period does not end a sentence. Kept short and
#: specific: over-listing makes the splitter miss real sentence ends.
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "mt",
        "vs",
        "etc",
        "eg",
        "ie",
        "approx",
        "dept",
        "inc",
        "ltd",
        "vol",
        "fig",
        "a.m",
        "p.m",
        "u.s",
        "u.k",
    }
)
#: Deliberately absent: "no", "st", "co", "am", "pm". Sentences in this corpus
#: really do end "She said no." and "I told him to leave at 5 pm.", and merging
#: two sentences is a worse failure than an occasional split after "Fenwick St."

#: A sentence ends at ., ! or ? followed by optional closing quotes/brackets and
#: whitespace. Candidates are then filtered against `_ABBREVIATIONS`, so the
#: regex can stay simple and readable.
# The curly quotes are not lookalikes for ASCII ones: scraped bodies contain
# both, and a sentence ending in a typographic quote has to close here too.
_SENTENCE_END = re.compile(r'(?<=[.!?])["\')\]”’]*\s+')  # noqa: RUF001
_TRAILING_TOKEN = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")
_DECIMAL_TAIL = re.compile(r"\d\.$")
_INITIAL = re.compile(r"\b[A-Z]\.$")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence, plus where it sat in the source.

    `start`/`end` are character offsets into the text it was split from, so a
    part can always be resolved back to the exact source substring it covers.
    """

    text: str
    start: int
    end: int
    #: True when a blank line separated this sentence from the previous one.
    #: Drives both the longer TTS pause and part-boundary preference.
    paragraph_break: bool

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _ends_with_abbreviation(chunk: str) -> bool:
    """Whether a candidate break is really an abbreviation or a decimal."""
    stripped = chunk.rstrip()
    if not stripped.endswith("."):
        return False
    if _DECIMAL_TAIL.search(stripped):
        return True
    if _INITIAL.search(stripped):
        return True
    match = _TRAILING_TOKEN.search(stripped)
    if match is None:
        return False
    return match.group(1).rstrip(".").lower() in _ABBREVIATIONS


def _iter_blocks(text: str) -> list[tuple[int, str]]:
    """Blank-line-separated blocks with their offsets into `text`."""
    blocks: list[tuple[int, str]] = []
    cursor = 0
    for match in re.finditer(r"\n\s*\n", text):
        blocks.append((cursor, text[cursor : match.start()]))
        cursor = match.end()
    blocks.append((cursor, text[cursor:]))
    return blocks


def split_sentences(text: str) -> list[Sentence]:
    """Split prose into sentences, preserving paragraph structure.

    Offsets are computed from match positions rather than by searching for the
    sentence text afterwards. That matters on scraped input: a search-based
    implementation raises on any body where the same fragment appears twice, or
    where whitespace inside a merged abbreviation break differs from the source.
    """
    if not text or not text.strip():
        return []

    sentences: list[Sentence] = []
    for block_index, (block_start, block) in enumerate(_iter_blocks(text)):
        if not block.strip():
            continue

        spans: list[tuple[int, int]] = []
        cursor = 0
        for match in _SENTENCE_END.finditer(block):
            spans.append((cursor, match.start()))
            cursor = match.end()
        spans.append((cursor, len(block)))

        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if end <= start:
                continue
            if merged and _ends_with_abbreviation(block[merged[-1][0] : merged[-1][1]]):
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))

        first = True
        for start, end in merged:
            cleaned = _WHITESPACE.sub(" ", block[start:end]).strip()
            if not cleaned:
                continue
            sentences.append(
                Sentence(
                    text=cleaned,
                    start=block_start + start,
                    end=block_start + end,
                    paragraph_break=first and block_index > 0,
                )
            )
            first = False
    return sentences


def speech_durations(texts: Sequence[str], *, words_per_minute: float) -> list[float]:
    """Per-text narration estimates, in seconds.

    Takes the *spoken* form of each sentence, not the written one.
    """
    if words_per_minute <= 0:
        raise ValueError("words_per_minute must be positive")
    return [len(text.split()) / words_per_minute * 60.0 for text in texts]


@dataclass(frozen=True, slots=True)
class SegmentPlan:
    """How a story is divided, expressed as ranges over its sentence list."""

    #: `(start_index, end_index)` pairs, end-exclusive, covering every sentence.
    ranges: tuple[tuple[int, int], ...]
    #: Character offsets where a new part begins, for `domain.series.plan_parts`.
    boundaries: tuple[int, ...]
    estimated_seconds: tuple[float, ...]

    @property
    def total_parts(self) -> int:
        return len(self.ranges)

    @property
    def longest_seconds(self) -> float:
        return max(self.estimated_seconds)


def plan_segments(
    sentences: list[Sentence],
    *,
    durations: Sequence[float],
    target_seconds: float,
    max_seconds: float,
    max_parts: int,
    paragraph_snap_ratio: float = 0.25,
) -> SegmentPlan:
    """Divide sentences into balanced, natural-sounding parts.

    Boundaries start at evenly spaced positions -- equal parts beat a greedy
    fill, which reliably leaves a stub final part -- and are then snapped to a
    nearby paragraph break. `paragraph_snap_ratio` bounds that search as a
    fraction of the target length, so snapping improves a cut without distorting
    the balance it started from.

    `durations` is supplied rather than derived from `Sentence.word_count`
    because the two differ, sometimes by 40%: speech shaping expands "AITA" into
    four spoken words and "$1,250" into six. Planning against the page instead
    of against the narration produces parts that reliably overrun.
    """
    if not sentences:
        raise ValueError("cannot plan segments for an empty sentence list")
    if len(durations) != len(sentences):
        raise ValueError("durations must be parallel to sentences")
    if max_seconds < target_seconds:
        raise ValueError("max_seconds must be at least target_seconds")

    durations = list(durations)
    total = sum(durations)

    if total <= max_seconds:
        return SegmentPlan(
            ranges=((0, len(sentences)),),
            boundaries=(),
            estimated_seconds=(total,),
        )

    # Prefer parts at the target length. When that needs more parts than are
    # allowed, stretch them towards `max_seconds` before giving up -- a longer
    # part that finishes the story beats a shorter one that abandons it.
    wanted = max(2, math_ceil(total / target_seconds))
    if wanted > max_parts:
        wanted = max_parts
        if math_ceil(total / max_seconds) > max_parts:
            raise StoryTooLongError(
                "story cannot be told within the configured part limit",
                needed=math_ceil(total / max_seconds),
                maximum=max_parts,
                estimated_seconds=round(total),
                max_seconds_per_part=max_seconds,
            )

    cumulative: list[float] = []
    running = 0.0
    for duration in durations:
        running += duration
        cumulative.append(running)

    slice_seconds = total / wanted
    snap_window = slice_seconds * paragraph_snap_ratio

    cuts: list[int] = []
    for index in range(1, wanted):
        ideal = slice_seconds * index
        cut = _nearest_sentence_index(cumulative, ideal, after=cuts[-1] if cuts else 0)
        snapped = _snap_to_paragraph(sentences, cumulative, cut, ideal, snap_window)
        if snapped > (cuts[-1] if cuts else 0):
            cuts.append(snapped)

    ranges: list[tuple[int, int]] = []
    previous = 0
    for cut in cuts:
        if cut <= previous or cut >= len(sentences):
            continue
        ranges.append((previous, cut))
        previous = cut
    ranges.append((previous, len(sentences)))

    return SegmentPlan(
        ranges=tuple(ranges),
        boundaries=tuple(sentences[start].start for start, _ in ranges[1:]),
        estimated_seconds=tuple(sum(durations[start:stop]) for start, stop in ranges),
    )


def _nearest_sentence_index(cumulative: list[float], target: float, *, after: int) -> int:
    """Index of the sentence boundary closest to `target` seconds."""
    best = after + 1
    best_gap = float("inf")
    for index in range(after + 1, len(cumulative) + 1):
        elapsed = cumulative[index - 1]
        gap = abs(elapsed - target)
        if gap < best_gap:
            best_gap = gap
            best = index
        elif elapsed > target:
            break
    return min(best, len(cumulative))


def _snap_to_paragraph(
    sentences: list[Sentence],
    cumulative: list[float],
    cut: int,
    ideal: float,
    window: float,
) -> int:
    """Move a cut to a paragraph break, if one sits close enough to be free."""
    best = cut
    best_gap = abs(cumulative[cut - 1] - ideal) if cut else 0.0
    for index, sentence in enumerate(sentences):
        if index == 0 or not sentence.paragraph_break:
            continue
        gap = abs(cumulative[index - 1] - ideal)
        if gap <= window and gap < best_gap:
            best = index
            best_gap = gap
    return best


def math_ceil(value: float) -> int:
    """Integer ceiling without importing `math` for one call."""
    whole = int(value)
    return whole if value == whole else whole + 1
