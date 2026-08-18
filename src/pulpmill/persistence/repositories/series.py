"""Series and part persistence.

Parts are written in one transaction with their series, and the numbering is
whatever `pulpmill.domain.series.plan_parts` computed. Nothing here accepts a
part number from a caller who did not go through that function -- the schema's
CHECK constraints are the second line of defence, and this repository is the
first.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from pulpmill.domain.enums import SeriesStatus
from pulpmill.domain.series import StoryPart
from pulpmill.domain.story import Provenance
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, to_iso
from pulpmill.persistence.database import Database


def _dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_to_part(row: sqlite3.Row, provenance: Provenance) -> StoryPart:
    return StoryPart(
        id=str(row["id"]),
        series_id=str(row["series_id"]),
        story_id=str(row["story_id"]),
        part_number=int(row["part_number"]),
        total_parts=int(row["total_parts"]),
        content_start=int(row["content_start"]),
        content_end=int(row["content_end"]),
        provenance=provenance,
        metadata=json.loads(row["metadata_json"]),
    )


class SeriesRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def save(
        self,
        *,
        series_id: str,
        story_id: str,
        parts: Sequence[StoryPart],
        status: SeriesStatus = SeriesStatus.PLANNED,
    ) -> str:
        """Persist a series and its parts, replacing any previous plan.

        Idempotent by construction: `plan_parts` derives both ids from the story
        id, so re-planning an unchanged story rewrites the same rows. Parts are
        deleted and reinserted rather than merged, because a re-plan that
        produced *fewer* parts must not leave the extra ones behind.
        """
        if not parts:
            raise ValueError("a series must have at least one part")
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO story_series (id, story_id, total_parts, status, metadata_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET total_parts = excluded.total_parts, "
                "status = excluded.status, updated_at = excluded.updated_at",
                (series_id, story_id, len(parts), status.value, now, now),
            )
            connection.execute("DELETE FROM story_parts WHERE series_id = ?", (series_id,))
            for part in parts:
                connection.execute(
                    "INSERT INTO story_parts (id, series_id, story_id, part_number, total_parts, "
                    "content_start, content_end, status, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        part.id,
                        part.series_id,
                        part.story_id,
                        part.part_number,
                        part.total_parts,
                        part.content_start,
                        part.content_end,
                        SeriesStatus.PLANNED.value,
                        _dump_json(dict(part.metadata)),
                        now,
                        now,
                    ),
                )
        return series_id

    def parts_for_story(self, story_id: str, provenance: Provenance) -> list[StoryPart]:
        rows = self._db.query_all(
            "SELECT * FROM story_parts WHERE story_id = ? ORDER BY part_number ASC",
            (story_id,),
        )
        return [row_to_part(row, provenance) for row in rows]

    def set_status(self, series_id: str, status: SeriesStatus) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE story_series SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, to_iso(self._clock.now()), series_id),
            )

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM story_series") or 0)
