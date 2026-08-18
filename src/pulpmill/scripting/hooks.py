"""Titles and hooks -- the first two seconds of a video.

Source titles are written for a feed, not for narration. They carry
platform furniture ("[UPDATE]", "(long, sorry)", "Part 3"), they are sometimes
far too long for a title card, and on some boards there is no title at all.

Everything here is deterministic. A provider may *propose* a better hook; that
is `pulpmill.scripting.provider`. This is what happens when none does, and what
the pipeline falls back to when one fails.
"""

from __future__ import annotations

import re

#: Bracketed or colon-prefixed furniture at the head of a title.
_TITLE_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]{0,24}\]|\([^)]{0,24}\)|(?:update|final update|tifu|wibta)\s*[:\-])\s*",
    re.IGNORECASE,
)
#: Parenthetical asides at the tail: "(long)", "(sorry for formatting)".
_TITLE_SUFFIX = re.compile(r"\s*[\(\[][^)\]]{0,40}[\)\]]\s*$")
_PART_MARKER = re.compile(
    r"\s*(?:[-–—:]\s*)?\bpart\s+\d+(?:\s*(?:of|/)\s*\d+)?\s*$",  # noqa: RUF001 - real en/em dashes
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_SENTENCE_CASE = re.compile(r"[a-z]")


def tidy_title(title: str, *, max_chars: int = 90) -> str:
    """Strip platform furniture and shouting from a source title.

    Never returns empty: if stripping would leave nothing, the original is kept.
    A title card with the wrong text beats a title card with no text.
    """
    if not title or not title.strip():
        return ""

    working = _TITLE_PREFIX.sub("", title.strip())
    working = _PART_MARKER.sub("", working)
    working = _TITLE_SUFFIX.sub("", working)
    working = _WHITESPACE.sub(" ", working).strip(" -–—:")  # noqa: RUF001 - real dashes

    if not working:
        working = _WHITESPACE.sub(" ", title.strip())

    # An all-caps title is shouting, not an acronym, once it is this long.
    if len(working) > 12 and not _SENTENCE_CASE.search(working):
        working = working.capitalize()

    if len(working) > max_chars:
        # The ellipsis has to come out of the budget, not be added on top of
        # it -- a title card sized for `max_chars` overflows at max_chars + 3.
        budget = max(1, max_chars - 3)
        cut = working[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-")
        working = f"{cut}..." if cut else working[:max_chars]
    return working


def build_hook(*, title: str, first_sentence: str, part_number: int, total_parts: int) -> str:
    """The opening line of narration.

    For a single part, the title *is* the hook: these communities write their
    titles as questions and cliffhangers already, and a synthesised alternative
    reliably reads worse than what a human wrote to get clicks.

    Later parts announce themselves instead. A viewer who arrives at part three
    needs to know that before anything else, and a recap is not available
    without summarising -- which is a model's job, not this function's.
    """
    clean = tidy_title(title, max_chars=160)
    terminated = clean if clean.endswith(("?", ".", "!")) else f"{clean}."
    if part_number > 1:
        if not clean:
            return f"Part {part_number} of {total_parts}."
        return f"{terminated} Part {part_number}."
    if clean:
        return terminated
    # Boards without titles: open on the story itself rather than on silence.
    return first_sentence.strip()


def build_outro(
    *, part_number: int, total_parts: int, template: str, final_outro: str
) -> str | None:
    """The closing line, or None when no outro should be spoken."""
    if part_number < total_parts:
        return template.format(next_part=part_number + 1, total_parts=total_parts).strip() or None
    return final_outro.strip() or None
