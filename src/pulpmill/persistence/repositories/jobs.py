"""Job and failure persistence.

Two jobs here, both about surviving unattended operation:

* A `jobs` row is opened before work starts and closed after, so a process that
  is killed leaves a `RUNNING` row behind. The next run reclaims those as
  `INTERRUPTED` rather than pretending they finished.
* Failures are *persisted*, not just logged. A failure record names the source,
  story, stage, operation, exception and retry count, so a week of unattended
  running can be audited with a query instead of a log grep.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pulpmill.domain.enums import JobStatus, PipelineStage
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from pulpmill.persistence.database import Database


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


@dataclass(slots=True)
class JobRecord:
    id: str
    kind: str
    status: JobStatus
    params: Mapping[str, Any]
    stats: Mapping[str, Any]
    started_at: str
    finished_at: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """One persisted failure. Mirrors the required error-reporting fields."""

    stage: PipelineStage
    operation: str
    error_type: str
    error_message: str
    source_platform: str | None = None
    story_id: str | None = None
    retry_count: int = 0
    context: Mapping[str, Any] = field(default_factory=dict)


def row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=JobStatus(str(row["status"])),
        params=json.loads(row["params_json"]),
        stats=json.loads(row["stats_json"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        error=row["error"],
    )


class JobRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def start(self, kind: str, params: Mapping[str, Any] | None = None) -> str:
        """Open a job row and return its id. Committed immediately.

        Committing before any work means a hard kill still leaves evidence the
        run existed.
        """
        job_id = str(uuid.uuid4())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO jobs (id, kind, status, params_json, stats_json, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    JobStatus.RUNNING.value,
                    _dump(params or {}),
                    _dump({}),
                    to_iso(self._clock.now()),
                ),
            )
        return job_id

    def finish(
        self,
        job_id: str,
        *,
        status: JobStatus,
        stats: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = ?, stats_json = ?, finished_at = ?, error = ? WHERE id = ?",
                (
                    status.value,
                    _dump(stats or {}),
                    to_iso(self._clock.now()),
                    error,
                    job_id,
                ),
            )

    def update_stats(self, job_id: str, stats: Mapping[str, Any]) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET stats_json = ? WHERE id = ?", (_dump(stats), job_id)
            )

    def get(self, job_id: str) -> JobRecord | None:
        row = self._db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return row_to_job(row) if row else None

    def recent(self, limit: int = 10) -> list[JobRecord]:
        rows = self._db.query_all("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [row_to_job(row) for row in rows]

    def reclaim_stale(self, *, older_than: timedelta) -> int:
        """Mark long-abandoned RUNNING jobs as INTERRUPTED.

        Called at the start of a run. Age-gated so it never touches a job that
        another process started moments ago.
        """
        cutoff = to_iso(self._clock.now() - older_than)
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, "
                "error = COALESCE(error, 'process exited before the job finished') "
                "WHERE status = ? AND started_at < ?",
                (
                    JobStatus.INTERRUPTED.value,
                    to_iso(self._clock.now()),
                    JobStatus.RUNNING.value,
                    cutoff,
                ),
            )
            return int(cursor.rowcount or 0)

    def count_running(self) -> int:
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM jobs WHERE status = ?", (JobStatus.RUNNING.value,)
            )
            or 0
        )

    def last_successful(self, kind: str) -> JobRecord | None:
        row = self._db.query_one(
            "SELECT * FROM jobs WHERE kind = ? AND status = ? ORDER BY finished_at DESC LIMIT 1",
            (kind, JobStatus.SUCCEEDED.value),
        )
        return row_to_job(row) if row else None


class FailureRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def record(self, failure: FailureRecord, *, job_id: str | None = None) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO job_failures "
                "(job_id, story_id, source_platform, stage, operation, error_type, "
                " error_message, retry_count, context_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    failure.story_id,
                    failure.source_platform,
                    failure.stage.value,
                    failure.operation,
                    failure.error_type,
                    # Bounded: a stack-trace-laden message must not bloat the DB.
                    failure.error_message[:2000],
                    failure.retry_count,
                    _dump(failure.context),
                    to_iso(self._clock.now()),
                ),
            )

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM job_failures") or 0)

    def count_since(self, hours: float) -> int:
        cutoff = to_iso(self._clock.now() - timedelta(hours=hours))
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM job_failures WHERE occurred_at >= ?", (cutoff,)
            )
            or 0
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            "SELECT job_id, story_id, source_platform, stage, operation, error_type, "
            "error_message, retry_count, occurred_at FROM job_failures "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def by_stage(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT stage, count(*) AS total FROM job_failures GROUP BY stage ORDER BY stage"
        )
        return {str(row["stage"]): int(row["total"]) for row in rows}

    def latest_at(self) -> str | None:
        value = self._db.query_scalar("SELECT max(occurred_at) FROM job_failures")
        return None if value is None else from_iso(str(value)).isoformat()
