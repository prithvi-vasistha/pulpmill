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
from collections.abc import Callable, Sequence
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


#: Clause boundaries, in the order they are preferred as cut points. A run-on
#: post has no sentence punctuation to split on, but it almost always has
#: commas and conjunctions -- and cutting there is far less audible than cutting
#: mid-clause.
_CLAUSE_BOUNDARY = re.compile(
    r"(?<=[,;:])\s+|\s+(?=(?:and|but|so|because|then|however|although|while|which)\s)",
    re.IGNORECASE,
)


def count_words(text: str) -> int:
    """Default length measure: plain word count."""
    return len(text.split())


def subdivide_long_sentences(
    sentences: list[Sentence],
    *,
    max_words: int,
    measure: Callable[[str], int] = count_words,
) -> list[Sentence]:
    """Break sentences that are too long to narrate in one piece.

    Two separate things make this necessary, and both are hard limits rather
    than preferences:

    * **The synthesiser has a token ceiling.** Kokoro truncates past 510
      phonemes and then raises. Measured against the shipped voice, ~85 words is
      where that begins, so anything above it fails outright rather than
      narrating badly.
    * **A sentence is the atom part planning works with.** A 285-word run-on
      cannot be split across parts, so it forces a part longer than
      `max_seconds` no matter what the planner does.

    Real 4chan and low-effort Reddit posts are frequently written as one
    unpunctuated paragraph, so this is a normal input, not an edge case.

    Cuts prefer clause boundaries and fall back to word boundaries. Offsets stay
    correct, so a part still resolves to the exact source text it came from.

    `measure` is how a piece's length is counted. The caller passes one that
    measures the *spoken* form, because that is what the synthesiser sees --
    "AITA" is one word on the page and four in the narration, so measuring the
    page would let a piece through that the model then refuses.
    """
    if max_words < 1:
        raise ValueError("max_words must be at least 1")

    result: list[Sentence] = []
    for sentence in sentences:
        if measure(sentence.text) <= max_words:
            result.append(sentence)
            continue
        result.extend(_split_sentence(sentence, max_words=max_words, measure=measure))
    return result


def _split_sentence(
    sentence: Sentence, *, max_words: int, measure: Callable[[str], int]
) -> list[Sentence]:
    """Cut one over-long sentence into chunks that each fit the limit."""
    fragments = _clause_fragments(sentence.text)

    chunks: list[tuple[int, int]] = []
    start = 0
    words = 0
    for frag_start, frag_end in fragments:
        frag_words = measure(sentence.text[frag_start:frag_end])
        if words and words + frag_words > max_words:
            chunks.append((start, frag_start))
            start = frag_start
            words = 0
        words += frag_words
    chunks.append((start, len(sentence.text)))

    pieces: list[Sentence] = []
    for index, (chunk_start, chunk_end) in enumerate(chunks):
        text = sentence.text[chunk_start:chunk_end].strip()
        if not text:
            continue
        # A single clause can still exceed the limit; fall back to words.
        for offset, hard in _hard_split(text, max_words=max_words, measure=measure):
            pieces.append(
                Sentence(
                    text=hard,
                    start=sentence.start + chunk_start + offset,
                    end=min(sentence.start + chunk_start + offset + len(hard), sentence.end),
                    # Only the first piece inherits the paragraph break; the
                    # rest are continuations of the same thought.
                    paragraph_break=sentence.paragraph_break and index == 0 and offset == 0,
                )
            )
    return pieces or [sentence]


def _clause_fragments(text: str) -> list[tuple[int, int]]:
    """Spans between clause boundaries, covering the whole string."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _CLAUSE_BOUNDARY.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
            cursor = match.end()
    spans.append((cursor, len(text)))
    return [(start, end) for start, end in spans if end > start]


def _hard_split(
    text: str, *, max_words: int, measure: Callable[[str], int]
) -> list[tuple[int, str]]:
    """Last-resort split at word boundaries, with offsets into `text`.

    Steps in written words but checks the measured length, so an expansion-heavy
    fragment produces more, smaller pieces rather than one that still overruns.
    """
    if measure(text) <= max_words:
        return [(0, text)]

    words = text.split()
    pieces: list[tuple[int, str]] = []
    cursor = 0
    current: list[str] = []
    chunk_start = 0

    for word in words:
        position = text.find(word, cursor)
        offset = position if position >= 0 else cursor
        if not current:
            chunk_start = offset
        candidate = [*current, word]
        if current and measure(" ".join(candidate)) > max_words:
            pieces.append((chunk_start, " ".join(current)))
            current = [word]
            chunk_start = offset
        else:
            current = candidate
        cursor = offset + len(word)

    if current:
        pieces.append((chunk_start, " ".join(current)))
    return pieces


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
