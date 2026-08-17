"""Domain layer: models, enums, contracts and rules.

Nothing here imports from `ingestion`, `persistence`, `ranking` or `cli`. The
dependency arrow points inwards, which is what lets the ranking engine be tested
without a database and the database be swapped without touching the model.
"""

from pulpmill.domain.enums import (
    DedupLayer,
    JobStatus,
    PipelineStage,
    SeriesStatus,
    StoryStatus,
)
from pulpmill.domain.ranking import RankedStory, RankingResult, SignalScore
from pulpmill.domain.series import StoryPart, StorySeries, plan_parts
from pulpmill.domain.source import AdapterHealth, FetchRequest, SourceAdapter
from pulpmill.domain.story import Engagement, Provenance, RawStory, Story, build_story_id

__all__ = [
    "AdapterHealth",
    "DedupLayer",
    "Engagement",
    "FetchRequest",
    "JobStatus",
    "PipelineStage",
    "Provenance",
    "RankedStory",
    "RankingResult",
    "RawStory",
    "SeriesStatus",
    "SignalScore",
    "SourceAdapter",
    "Story",
    "StoryPart",
    "StorySeries",
    "StoryStatus",
    "build_story_id",
    "plan_parts",
]
