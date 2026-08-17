"""Repositories: the only code that writes SQL."""

from pulpmill.persistence.repositories.editorial import (
    EditorialRepository,
    SelectionBatch,
    SelectionEntry,
)
from pulpmill.persistence.repositories.jobs import (
    FailureRecord,
    FailureRepository,
    JobRecord,
    JobRepository,
)
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import (
    NoveltyEntry,
    StoryRepository,
    UpsertResult,
    row_to_story,
)

__all__ = [
    "EditorialRepository",
    "FailureRecord",
    "FailureRepository",
    "JobRecord",
    "JobRepository",
    "NoveltyEntry",
    "RankingRepository",
    "SelectionBatch",
    "SelectionEntry",
    "StoryRepository",
    "UpsertResult",
    "row_to_story",
]
