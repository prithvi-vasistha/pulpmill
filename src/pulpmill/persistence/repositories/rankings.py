"""Ranking persistence.

Every score is stored with the evidence behind it -- component values, the
weights actually applied, and the reference time used for age-dependent signals.
That is what makes "why did this story score 71.4" answerable months later.

Idempotency comes from the UNIQUE constraint on
`(story_id, ranking_version, config_fingerprint)`: re-ranking with unchanged
config updates one row instead of appending a second.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from pulpmill.domain.ranking import RankedStory, RankingResult
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from pulpmill.persistence.database import Database
from pulpmill.persistence.repositories.stories import row_to_story

_STORY_COLUMNS = (
    "id",
    "source_platform",
    "source_id",
    "canonical_url",
    "url_fingerprint",
    "author",
    "title",
    "raw_content",
    "normalized_content",
    "content_hash",
    "simhash",
    "word_count",
    "language",
    "created_at",
    "discovered_at",
    "updated_at",
    "engagement_json",
    "metadata_json",
    "status",
    "duplicate_of_id",
    "duplicate_layer",
)


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_to_ranking(row: sqlite3.Row) -> RankingResult:
    return RankingResult(
        story_id=str(row["story_id"]),
        ranking_version=str(row["ranking_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        final_score=float(row["final_score"]),
        component_scores=json.loads(row["component_scores_json"]),
        effective_weights=json.loads(row["weights_json"]),
        explanation=json.loads(row["explanation_json"]),
        reference_time=from_iso(str(row["reference_time"])),
        ranked_at=from_iso(str(row["ranked_at"])),
    )


class RankingRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def save(self, result: RankingResult) -> None:
        """Insert or refresh a ranking. Safe to call repeatedly."""
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO story_rankings "
                "(story_id, ranking_version, config_fingerprint, final_score, "
                " component_scores_json, weights_json, explanation_json, reference_time, ranked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (story_id, ranking_version, config_fingerprint) DO UPDATE SET "
                "  final_score = excluded.final_score, "
                "  component_scores_json = excluded.component_scores_json, "
                "  weights_json = excluded.weights_json, "
                "  explanation_json = excluded.explanation_json, "
                "  reference_time = excluded.reference_time, "
                "  ranked_at = excluded.ranked_at",
                (
                    result.story_id,
                    result.ranking_version,
                    result.config_fingerprint,
                    result.final_score,
                    _dump(result.component_scores),
                    _dump(result.effective_weights),
                    _dump(result.explanation),
                    to_iso(result.reference_time),
                    to_iso(result.ranked_at),
                ),
            )

    def save_many(self, results: list[RankingResult]) -> int:
        """Persist a batch inside one transaction."""
        with self._db.transaction():
            for result in results:
                self.save(result)
        return len(results)

    def get(
        self, story_id: str, *, ranking_version: str, config_fingerprint: str
    ) -> RankingResult | None:
        row = self._db.query_one(
            "SELECT * FROM story_rankings WHERE story_id = ? AND ranking_version = ? "
            "AND config_fingerprint = ?",
            (story_id, ranking_version, config_fingerprint),
        )
        return row_to_ranking(row) if row else None

    def latest_for_story(self, story_id: str) -> RankingResult | None:
        row = self._db.query_one(
            "SELECT * FROM story_rankings WHERE story_id = ? ORDER BY ranked_at DESC, id DESC LIMIT 1",
            (story_id,),
        )
        return row_to_ranking(row) if row else None

    def top_candidates(
        self,
        *,
        ranking_version: str,
        config_fingerprint: str,
        limit: int,
        exclude_statuses: tuple[str, ...] = ("DUPLICATE", "REJECTED", "FAILED"),
        platform: str | None = None,
    ) -> list[RankedStory]:
        """Highest-scoring stories for one ranking configuration.

        Ties break on `story_id` so the ordering is total and reproducible --
        two runs over identical data return the same list in the same order.
        """
        params: list[Any] = [ranking_version, config_fingerprint]
        clauses = ["r.ranking_version = ?", "r.config_fingerprint = ?"]

        if exclude_statuses:
            clauses.append(f"s.status NOT IN ({', '.join('?' for _ in exclude_statuses)})")
            params.extend(exclude_statuses)
        if platform is not None:
            clauses.append("s.source_platform = ?")
            params.append(platform)
        params.append(limit)

        rows = self._db.query_all(
            f"SELECT {', '.join('s.' + column for column in _STORY_COLUMNS)}, "
            "  r.story_id AS r_story_id, r.ranking_version, r.config_fingerprint, "
            "  r.final_score, r.component_scores_json, r.weights_json, r.explanation_json, "
            "  r.reference_time, r.ranked_at "
            "FROM story_rankings r JOIN stories s ON s.id = r.story_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY r.final_score DESC, s.id ASC LIMIT ?",
            params,
        )

        candidates: list[RankedStory] = []
        for row in rows:
            ranking = RankingResult(
                story_id=str(row["r_story_id"]),
                ranking_version=str(row["ranking_version"]),
                config_fingerprint=str(row["config_fingerprint"]),
                final_score=float(row["final_score"]),
                component_scores=json.loads(row["component_scores_json"]),
                effective_weights=json.loads(row["weights_json"]),
                explanation=json.loads(row["explanation_json"]),
                reference_time=from_iso(str(row["reference_time"])),
                ranked_at=from_iso(str(row["ranked_at"])),
            )
            candidates.append(RankedStory(story=row_to_story(row), ranking=ranking))
        return candidates

    def count(self, *, ranking_version: str | None = None) -> int:
        if ranking_version is None:
            return int(self._db.query_scalar("SELECT count(*) FROM story_rankings") or 0)
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM story_rankings WHERE ranking_version = ?",
                (ranking_version,),
            )
            or 0
        )

    def count_distinct_stories(self, *, ranking_version: str, config_fingerprint: str) -> int:
        return int(
            self._db.query_scalar(
                "SELECT count(DISTINCT story_id) FROM story_rankings "
                "WHERE ranking_version = ? AND config_fingerprint = ?",
                (ranking_version, config_fingerprint),
            )
            or 0
        )
