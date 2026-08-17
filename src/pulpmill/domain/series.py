"""Multi-part series model.

Reserved for the rendering stages, defined now so the ingestion schema does not
have to be rewritten later. The only behaviour implemented today is
`plan_parts`, which is the rule that matters: **the pipeline computes part
numbering, never a language model.** A model may propose where to cut a story;
it may not decide that a story is "Part 2 of 4".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pulpmill.domain.enums import SeriesStatus
from pulpmill.domain.story import Provenance

#: Fixed namespace for deterministic series identifiers.
SERIES_ID_NAMESPACE = uuid.UUID("b1c07d3a-9f47-5f2e-8a6b-3d51e9c4a7f2")


def build_series_id(story_id: str, revision: int = 1) -> str:
    """Derive a series id from its story, so re-planning is idempotent."""
    return str(uuid.uuid5(SERIES_ID_NAMESPACE, f"{story_id}:{revision}"))


@dataclass(frozen=True, slots=True)
class StoryPart:
    """One narratable slice of a story.

    Carries `provenance` explicitly: a part that has travelled through script
    generation, TTS and rendering still knows the URL it came from.
    """

    id: str
    series_id: str
    story_id: str
    part_number: int
    total_parts: int
    #: Character offsets into `Story.normalized_content`.
    content_start: int
    content_end: int
    provenance: Provenance
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not 1 <= self.part_number <= self.total_parts:
            raise ValueError(
                f"part_number {self.part_number} out of range for total_parts {self.total_parts}"
            )
        if self.content_end <= self.content_start:
            raise ValueError("part content_end must be greater than content_start")

    @property
    def label(self) -> str:
        """Human-facing label, e.g. `"Part 2/4"`."""
        return f"Part {self.part_number}/{self.total_parts}"

    def text(self, normalized_content: str) -> str:
        return normalized_content[self.content_start : self.content_end]


@dataclass(frozen=True, slots=True)
class StorySeries:
    """A story split into one or more ordered parts."""

    id: str
    story_id: str
    total_parts: int
    status: SeriesStatus
    parts: tuple[StoryPart, ...]
    provenance: Provenance
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.total_parts != len(self.parts):
            raise ValueError("total_parts must equal the number of parts")
        expected = list(range(1, self.total_parts + 1))
        actual = [part.part_number for part in self.parts]
        if actual != expected:
            raise ValueError(f"parts must be numbered {expected}, got {actual}")


def plan_parts(
    *,
    story_id: str,
    provenance: Provenance,
    boundaries: Sequence[int],
    content_length: int,
    revision: int = 1,
) -> tuple[str, tuple[StoryPart, ...]]:
    """Turn a list of cut points into numbered parts.

    `boundaries` are character offsets where a new part begins, exclusive of 0
    and of `content_length`. A script generator (or a human) proposes where to
    cut; this function assigns `part_number` and `total_parts`. Deterministic:
    the same boundaries always yield the same ids and numbering.
    """
    if content_length <= 0:
        raise ValueError("content_length must be positive")

    cuts = sorted({b for b in boundaries if 0 < b < content_length})
    offsets = [0, *cuts, content_length]
    total_parts = len(offsets) - 1
    series_id = build_series_id(story_id, revision)

    parts = tuple(
        StoryPart(
            id=str(uuid.uuid5(SERIES_ID_NAMESPACE, f"{series_id}:{number}")),
            series_id=series_id,
            story_id=story_id,
            part_number=number,
            total_parts=total_parts,
            content_start=offsets[number - 1],
            content_end=offsets[number],
            provenance=provenance,
            metadata={},
        )
        for number in range(1, total_parts + 1)
    )
    return series_id, parts
