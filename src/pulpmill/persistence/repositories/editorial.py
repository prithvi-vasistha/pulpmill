"""Editorial selection persistence.

A batch records *which* provider was asked and which one actually produced the
ordering. When Claude is configured but times out, `provider` stays `claude`
while `effective_provider` becomes `deterministic` and `fallback_reason`
explains why -- so a silent degradation is visible in the data, not just in a
log line that has since rotated away.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, to_iso
from pulpmill.persistence.database import Database


@dataclass(frozen=True, slots=True)
class SelectionEntry:
    story_id: str
    position: int
    rationale: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SelectionBatch:
    id: str
    provider: str
    effective_provider: str
    fallback_reason: str | None
    ranking_version: str
    config_fingerprint: str
    candidate_count: int
    created_at: str
    entries: tuple[SelectionEntry, ...]

    @property
    def used_fallback(self) -> bool:
        return self.provider != self.effective_provider


class EditorialRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def save_batch(
        self,
        *,
        provider: str,
        effective_provider: str,
        fallback_reason: str | None,
        ranking_version: str,
        config_fingerprint: str,
        candidate_count: int,
        entries: Sequence[SelectionEntry],
    ) -> str:
        """Persist a batch and its ordering atomically."""
        batch_id = str(uuid.uuid4())
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO editorial_batches "
                "(id, provider, effective_provider, fallback_reason, ranking_version, "
                " config_fingerprint, candidate_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    provider,
                    effective_provider,
                    fallback_reason,
                    ranking_version,
                    config_fingerprint,
                    candidate_count,
                    now,
                ),
            )
            for entry in entries:
                connection.execute(
                    "INSERT INTO editorial_selections "
                    "(batch_id, story_id, position, rationale, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        entry.story_id,
                        entry.position,
                        entry.rationale,
                        json.dumps(
                            dict(entry.metadata or {}),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
        return batch_id

    def get_batch(self, batch_id: str) -> SelectionBatch | None:
        row = self._db.query_one("SELECT * FROM editorial_batches WHERE id = ?", (batch_id,))
        if row is None:
            return None
        entry_rows = self._db.query_all(
            "SELECT story_id, position, rationale, metadata_json FROM editorial_selections "
            "WHERE batch_id = ? ORDER BY position ASC",
            (batch_id,),
        )
        return SelectionBatch(
            id=str(row["id"]),
            provider=str(row["provider"]),
            effective_provider=str(row["effective_provider"]),
            fallback_reason=row["fallback_reason"],
            ranking_version=str(row["ranking_version"]),
            config_fingerprint=str(row["config_fingerprint"]),
            candidate_count=int(row["candidate_count"]),
            created_at=str(row["created_at"]),
            entries=tuple(
                SelectionEntry(
                    story_id=str(entry["story_id"]),
                    position=int(entry["position"]),
                    rationale=entry["rationale"],
                    metadata=json.loads(entry["metadata_json"]),
                )
                for entry in entry_rows
            ),
        )

    def latest_batch(self) -> SelectionBatch | None:
        row = self._db.query_one(
            "SELECT id FROM editorial_batches ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        return None if row is None else self.get_batch(str(row["id"]))

    def recently_selected_titles(self, *, hours: float, limit: int = 40) -> list[str]:
        """Titles chosen in the recent past.

        Passed to an editorial provider as "already used", so it can steer away
        from repeating a topic across consecutive publications.
        """
        cutoff = to_iso(self._clock.now() - timedelta(hours=hours))
        rows = self._db.query_all(
            "SELECT s.title FROM editorial_selections e JOIN stories s ON s.id = e.story_id "
            "WHERE e.created_at >= ? ORDER BY e.created_at DESC, e.position ASC LIMIT ?",
            (cutoff, limit),
        )
        return [str(row["title"]) for row in rows]

    def selected_story_ids(self, *, hours: float) -> set[str]:
        cutoff = to_iso(self._clock.now() - timedelta(hours=hours))
        rows = self._db.query_all(
            "SELECT DISTINCT story_id FROM editorial_selections WHERE created_at >= ?",
            (cutoff,),
        )
        return {str(row["story_id"]) for row in rows}

    def count_selections(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM editorial_selections") or 0)

    def count_batches(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM editorial_batches") or 0)
