"""The editorial provider contract.

Editorial selection is a *separate stage* from ranking, and a small one. The
local ranking engine reduces thousands of stories to a handful; only that
handful is ever shown to a provider. No provider sees the scraped dataset.

Providers are interchangeable and none is required -- the deterministic provider
always works, with no API key, no network and no model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pulpmill.domain.ranking import RankedStory
from pulpmill.normalization.text import truncate

#: Words per minute used to estimate narration length for a candidate.
NARRATION_WPM = 150.0


@dataclass(frozen=True, slots=True)
class EditorialCandidate:
    """The compact view of a story that a provider is allowed to see.

    Deliberately small: an excerpt rather than the full body, plus the metadata
    an editor actually needs. Keeping this narrow bounds token cost and keeps
    the provider focused on selection rather than on reading everything.
    """

    story_id: str
    title: str
    source_platform: str
    community: str
    canonical_url: str
    word_count: int
    estimated_seconds: int
    final_score: float
    component_scores: Mapping[str, float]
    excerpt: str
    age_hours: float

    @classmethod
    def from_ranked(cls, ranked: RankedStory, *, excerpt_chars: int = 600) -> EditorialCandidate:
        story = ranked.story
        age_seconds = (ranked.ranking.reference_time - story.created_at).total_seconds()
        community = story.metadata.get("quality_key") or story.source_platform
        return cls(
            story_id=story.id,
            title=story.title,
            source_platform=story.source_platform,
            community=str(community),
            canonical_url=story.canonical_url,
            word_count=story.word_count,
            estimated_seconds=round(story.word_count / NARRATION_WPM * 60.0),
            final_score=ranked.ranking.final_score,
            component_scores=dict(ranked.ranking.component_scores),
            excerpt=truncate(story.normalized_content.replace("\n", " "), excerpt_chars),
            age_hours=max(0.0, age_seconds / 3600.0),
        )

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "title": self.title,
            "source": self.source_platform,
            "community": self.community,
            "word_count": self.word_count,
            "estimated_narration_seconds": self.estimated_seconds,
            "age_hours": round(self.age_hours, 1),
            "local_score": round(self.final_score, 2),
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class SelectedStory:
    story_id: str
    position: int
    rationale: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EditorialDecision:
    """An ordered selection plus how it was produced."""

    provider: str
    selections: tuple[SelectedStory, ...]
    notes: str = ""

    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.selections)


@runtime_checkable
class EditorialProvider(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> tuple[bool, str]:
        """Whether this provider can run, and why not if it cannot.

        Checked before the candidate set is built, so an unusable provider costs
        nothing and falls back cleanly.
        """
        ...

    def select(
        self,
        candidates: Sequence[EditorialCandidate],
        *,
        count: int,
        recently_used_titles: Sequence[str] = (),
    ) -> EditorialDecision:
        """Choose and order `count` stories. Raises `EditorialError` on failure."""
        ...
