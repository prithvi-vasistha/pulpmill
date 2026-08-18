"""The narration script: what a voice actually says.

A script is deliberately *not* the story text. Between the two sit three
transformations that all have to happen somewhere, and this is where they land:

* **Segmentation** -- a 2 000-word story is not one short. It is cut into parts,
  and the pipeline assigns the numbering. See `pulpmill.domain.series`.
* **Speech shaping** -- "AITA" and "$40" and "3am" are read wrong by every TTS
  model. `speech_text` is the corrected form; `text` stays readable for captions
  and debugging. Keeping both is what lets a caption say "$40" while the
  narrator says "forty dollars".
* **Framing** -- a hook at the front and an outro at the back, neither of which
  exists in the source.

Provenance rides along on every script, so a rendered file still knows the URL
it came from without a database round trip.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pulpmill.domain.story import Provenance

#: Fixed namespace for deterministic script identifiers. Never change this.
SCRIPT_ID_NAMESPACE = uuid.UUID("2e7c4f81-6b0d-5a93-9f42-8c1e5d7a0b36")


def build_script_id(story_id: str, part_number: int) -> str:
    """Derive a script id from its story and part.

    Deterministic, so regenerating a script updates one row instead of
    accumulating a new one on every run.
    """
    if part_number < 1:
        raise ValueError("part_number is 1-indexed")
    return str(uuid.uuid5(SCRIPT_ID_NAMESPACE, f"{story_id}:{part_number}"))


class LineRole(StrEnum):
    """What a line is doing, which decides how it is presented.

    `HOOK` and `OUTRO` are written by the pipeline; `BODY` is the source text.
    Keeping them distinguishable means the renderer can style the hook
    differently and the validator can assert a body actually exists.
    """

    HOOK = "hook"
    BODY = "body"
    OUTRO = "outro"


@dataclass(frozen=True, slots=True)
class ScriptLine:
    """One sentence-sized unit of narration.

    Sentence granularity is not cosmetic: the TTS stage synthesises one clip per
    line and concatenates, which is what makes clip boundaries -- and therefore
    caption timings -- exact rather than estimated.
    """

    index: int
    role: LineRole
    #: Human-readable form. What captions display and what a person reviews.
    text: str
    #: What is handed to the synthesiser. Differs wherever a numeral,
    #: abbreviation or piece of platform shorthand would be mispronounced.
    speech_text: str
    #: Blank-line separation from the previous line in the source. The TTS stage
    #: turns this into a longer pause.
    paragraph_break: bool = False

    def __post_init__(self) -> None:
        if not self.speech_text.strip():
            raise ValueError(f"line {self.index} has no speakable text")


@dataclass(frozen=True, slots=True)
class NarrationScript:
    """A complete, renderable script for exactly one video."""

    id: str
    story_id: str
    part_number: int
    total_parts: int
    #: Set when this script came from a planned multi-part series.
    series_id: str | None
    part_id: str | None
    provenance: Provenance
    #: On-screen title. Shortened for display; never the narration.
    title: str
    lines: tuple[ScriptLine, ...]
    #: Which provider produced this, and under what settings. Stored so a
    #: script is attributable to the configuration that generated it.
    generator: str
    generator_version: str
    config_fingerprint: str
    created_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a script must contain at least one line")
        if not 1 <= self.part_number <= self.total_parts:
            raise ValueError(
                f"part_number {self.part_number} out of range for total_parts {self.total_parts}"
            )
        expected = list(range(len(self.lines)))
        actual = [line.index for line in self.lines]
        if actual != expected:
            raise ValueError(f"script lines must be indexed {expected}, got {actual}")
        if not any(line.role is LineRole.BODY for line in self.lines):
            raise ValueError("a script must contain at least one body line")

    @property
    def is_series(self) -> bool:
        return self.total_parts > 1

    @property
    def label(self) -> str:
        """Display label, e.g. `"Part 2/4"`. Empty for a single-part story."""
        return f"Part {self.part_number}/{self.total_parts}" if self.is_series else ""

    @property
    def speech_text(self) -> str:
        """Everything the narrator says, as one string. For hashing and review."""
        return "\n".join(line.speech_text for line in self.lines)

    @property
    def display_text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def body_lines(self) -> tuple[ScriptLine, ...]:
        return tuple(line for line in self.lines if line.role is LineRole.BODY)

    def estimated_seconds(self, *, words_per_minute: float) -> float:
        """Narration length before any audio exists, used for planning."""
        if words_per_minute <= 0:
            raise ValueError("words_per_minute must be positive")
        words = sum(len(line.speech_text.split()) for line in self.lines)
        return words / words_per_minute * 60.0


def next_part_label(part_number: int, total_parts: int) -> Sequence[int]:
    """Remaining part numbers after `part_number`. Empty on the final part."""
    return tuple(range(part_number + 1, total_parts + 1))
