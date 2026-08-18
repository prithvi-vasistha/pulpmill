"""The script provider contract.

A provider's job is narrow on purpose. It sees a compact brief and returns
*advice*: a better hook, a tidier title, suggested places to cut. It does not
return a script, it does not number parts, and nothing it returns is trusted
until it has been validated against the story it was given.

That boundary is the point. Part numbering, part counts and the mapping from
text to parts are computed by the pipeline (`pulpmill.domain.series.plan_parts`)
and are not delegable. A model that proposes cutting after sentence 40 is making
a suggestion about pacing; it is not deciding that a story is "Part 2 of 4".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ScriptBrief:
    """The compact view of a story a provider is allowed to see."""

    story_id: str
    title: str
    source_platform: str
    community: str
    #: The story's sentences, in order. Indices in returned guidance refer to
    #: positions in this sequence.
    sentences: tuple[str, ...]
    word_count: int
    estimated_seconds: float
    target_seconds: float
    max_seconds: float
    max_parts: int

    def to_prompt_dict(self, *, max_sentences: int = 400) -> dict[str, Any]:
        return {
            "title": self.title,
            "community": self.community,
            "word_count": self.word_count,
            "estimated_seconds": round(self.estimated_seconds),
            "target_seconds_per_part": round(self.target_seconds),
            "max_seconds_per_part": round(self.max_seconds),
            "max_parts": self.max_parts,
            "sentences": [
                {"index": index, "text": text}
                for index, text in enumerate(self.sentences[:max_sentences])
            ],
        }


@dataclass(frozen=True, slots=True)
class ScriptGuidance:
    """Advice returned by a provider. Every field is optional and unenforced.

    `cut_after` holds sentence indices *after* which a new part should begin.
    The service validates them -- in range, strictly ascending, producing parts
    that fit the duration limits -- and discards the whole set if any check
    fails. Partially honouring bad advice would produce a plan nobody chose.
    """

    hook: str | None = None
    title: str | None = None
    cut_after: tuple[int, ...] = ()
    notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.hook or self.title or self.cut_after)


@runtime_checkable
class ScriptProvider(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> tuple[bool, str]:
        """Whether this provider can run, and why not if it cannot."""
        ...

    def guide(self, brief: ScriptBrief) -> ScriptGuidance:
        """Return advice for one story. Raises `ScriptError` on failure."""
        ...


def validate_cut_points(
    cuts: Sequence[int],
    *,
    sentence_count: int,
    max_parts: int,
) -> tuple[int, ...]:
    """Check proposed cut points and normalise them.

    Raises `ValueError` describing the first problem found. The caller treats
    any failure as "ignore the advice", which is why the message matters more
    than the exception type: it is what gets logged and stored.
    """
    if not cuts:
        return ()
    ordered = list(cuts)
    if ordered != sorted(set(ordered)):
        raise ValueError("cut points must be strictly ascending with no repeats")
    if ordered[0] < 0 or ordered[-1] >= sentence_count - 1:
        raise ValueError(
            f"cut points must fall inside 0..{sentence_count - 2} for {sentence_count} sentences"
        )
    if len(ordered) + 1 > max_parts:
        raise ValueError(f"{len(ordered) + 1} parts exceeds the configured maximum of {max_parts}")
    return tuple(ordered)
