"""Publication persistence.

The `UNIQUE (video_id, target)` constraint is load-bearing. A publish that
crashes after the platform accepted the upload but before the response was
stored must, on retry, find the existing row rather than upload a second copy.
Everything here is written so that retry path is the normal one.

`dry_run` is recorded on every row: a rehearsal must never be mistakable for a
publication, in the data or in a status report.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pulpmill.domain.publishing import PublishState
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, to_iso
from pulpmill.persistence.database import Database


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    id: str
    video_id: str
    script_id: str
    story_id: str
    target: str
    adapter: str
    state: PublishState
    dry_run: bool
    privacy: str
    remote_id: str | None
    remote_url: str | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str
    published_at: str | None
    request: Mapping[str, Any]
    response: Mapping[str, Any]


class PublicationRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def begin(
        self,
        *,
        video_id: str,
        script_id: str,
        story_id: str,
        target: str,
        adapter: str,
        privacy: str,
        dry_run: bool,
        request: Mapping[str, Any],
    ) -> str:
        """Claim an attempt, returning its id.

        Written before anything is transmitted, so a process that dies mid-upload
        leaves a `PENDING` row naming exactly which video and which platform were
        in flight.
        """
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id, attempts FROM publications WHERE video_id = ? AND target = ?",
                (video_id, target),
            ).fetchone()
            if existing is not None:
                publication_id = str(existing["id"])
                connection.execute(
                    "UPDATE publications SET state = ?, dry_run = ?, privacy = ?, "
                    "request_json = ?, attempts = ?, error = NULL, updated_at = ? WHERE id = ?",
                    (
                        PublishState.UPLOADING.value,
                        1 if dry_run else 0,
                        privacy,
                        _dump(dict(request)),
                        int(existing["attempts"]) + 1,
                        now,
                        publication_id,
                    ),
                )
                return publication_id

            publication_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO publications (id, video_id, script_id, story_id, target, adapter, "
                "state, dry_run, privacy, request_json, response_json, attempts, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 1, ?, ?)",
                (
                    publication_id,
                    video_id,
                    script_id,
                    story_id,
                    target,
                    adapter,
                    PublishState.UPLOADING.value,
                    1 if dry_run else 0,
                    privacy,
                    _dump(dict(request)),
                    now,
                    now,
                ),
            )
            return publication_id

    def complete(
        self,
        publication_id: str,
        *,
        state: PublishState,
        remote_id: str | None = None,
        remote_url: str | None = None,
        response: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = to_iso(self._clock.now())
        published = now if state is PublishState.PUBLISHED else None
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE publications SET state = ?, remote_id = ?, remote_url = ?, "
                "response_json = ?, error = ?, updated_at = ?, "
                "published_at = coalesce(?, published_at) WHERE id = ?",
                (
                    state.value,
                    remote_id,
                    remote_url,
                    _dump(dict(response or {})),
                    error,
                    now,
                    published,
                    publication_id,
                ),
            )

    def get(self, video_id: str, target: str) -> PublicationRecord | None:
        row = self._db.query_one(
            "SELECT * FROM publications WHERE video_id = ? AND target = ?", (video_id, target)
        )
        if row is None:
            return None
        return PublicationRecord(
            id=str(row["id"]),
            video_id=str(row["video_id"]),
            script_id=str(row["script_id"]),
            story_id=str(row["story_id"]),
            target=str(row["target"]),
            adapter=str(row["adapter"]),
            state=PublishState(str(row["state"])),
            dry_run=bool(row["dry_run"]),
            privacy=str(row["privacy"]),
            remote_id=row["remote_id"],
            remote_url=row["remote_url"],
            error=row["error"],
            attempts=int(row["attempts"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            published_at=row["published_at"],
            request=json.loads(row["request_json"]),
            response=json.loads(row["response_json"]),
        )

    def published_today(self, target: str, *, hours: float = 24.0) -> int:
        """Real publications in the recent window, for the local daily cap.

        Dry runs are excluded: a rehearsal does not consume quota, and counting
        one would make `--dry-run` silently reduce the real allowance.
        """
        cutoff = to_iso(self._clock.now() - timedelta(hours=hours))
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM publications WHERE target = ? AND state = ? "
                "AND dry_run = 0 AND published_at >= ?",
                (target, PublishState.PUBLISHED.value, cutoff),
            )
            or 0
        )

    def counts_by_state(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT state, count(*) AS total FROM publications GROUP BY state ORDER BY state"
        )
        return {str(row["state"]): int(row["total"]) for row in rows}

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM publications") or 0)
