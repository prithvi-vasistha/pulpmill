"""Story persistence.

Idempotency lives here. `upsert` is keyed on `(source_platform, source_id)`, so
scraping the same post twice updates one row instead of creating two -- and it
never resets a story's status or `discovered_at`, so re-scraping a post that has
already been ranked does not drag it backwards through the pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pulpmill.domain.enums import DedupLayer, PipelineStage, StoryStatus
from pulpmill.domain.errors import StoryNotFoundError
from pulpmill.domain.state import ensure_transition
from pulpmill.domain.story import Engagement, Story
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from pulpmill.infrastructure.logging import get_logger
from pulpmill.normalization.hashing import simhash_bands, simhash_from_hex, simhash_to_hex
from pulpmill.persistence.database import Database

_log = get_logger("persistence.stories")

_COLUMNS = (
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

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM stories"


def _dump_json(value: Mapping[str, Any]) -> str:
    # sort_keys keeps stored JSON byte-stable, so a re-upsert of unchanged data
    # produces an identical row rather than a spurious diff.
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_to_story(row: sqlite3.Row) -> Story:
    """Rebuild a `Story` from a database row."""
    simhash_hex = row["simhash"]
    duplicate_layer = row["duplicate_layer"]
    return Story(
        id=str(row["id"]),
        source_platform=str(row["source_platform"]),
        source_id=str(row["source_id"]),
        canonical_url=str(row["canonical_url"]),
        url_fingerprint=str(row["url_fingerprint"]),
        author=row["author"],
        title=str(row["title"]),
        raw_content=str(row["raw_content"]),
        normalized_content=str(row["normalized_content"]),
        content_hash=str(row["content_hash"]),
        simhash=simhash_from_hex(simhash_hex) if simhash_hex else None,
        word_count=int(row["word_count"]),
        language=row["language"],
        created_at=from_iso(str(row["created_at"])),
        discovered_at=from_iso(str(row["discovered_at"])),
        updated_at=from_iso(str(row["updated_at"])),
        engagement=Engagement.from_dict(json.loads(row["engagement_json"])),
        metadata=json.loads(row["metadata_json"]),
        status=StoryStatus(str(row["status"])),
        duplicate_of_id=row["duplicate_of_id"],
        duplicate_layer=DedupLayer(duplicate_layer) if duplicate_layer else None,
    )


@dataclass(frozen=True, slots=True)
class UpsertResult:
    story: Story
    created: bool
    #: True when an existing row's content or engagement actually changed.
    updated: bool


@dataclass(frozen=True, slots=True)
class NoveltyEntry:
    """A bounded projection of a story, used by the novelty signal.

    Only the title and a configurable prefix of the body are loaded -- the whole
    point is to keep the lookback corpus small enough to hold in memory on a
    machine that is also rendering video.
    """

    story_id: str
    title: str
    content_prefix: str


class StoryRepository:
    """All reads and writes for the `stories` table and its satellites."""

    def __init__(
        self,
        database: Database,
        clock: Clock = SYSTEM_CLOCK,
        *,
        simhash_band_count: int = 4,
    ) -> None:
        self._db = database
        self._clock = clock
        # Must match the band count the dedup engine queries with; both are fed
        # from `deduplication.layers.near_duplicate.band_count`.
        self._band_count = simhash_band_count

    def _reload(self, story_id: str) -> Story:
        story = self.get(story_id)
        if story is None:  # pragma: no cover - implies the write was rolled back
            raise StoryNotFoundError("story disappeared during write", story_id=story_id)
        return story

    # --- lookups -------------------------------------------------------------

    def get(self, story_id: str) -> Story | None:
        row = self._db.query_one(f"{_SELECT} WHERE id = ?", (story_id,))
        return row_to_story(row) if row else None

    def require(self, story_id: str) -> Story:
        story = self.get(story_id)
        if story is None:
            raise StoryNotFoundError("no such story", story_id=story_id)
        return story

    def find_by_source(self, source_platform: str, source_id: str) -> Story | None:
        """Dedup layer 1: the exact same post from the exact same platform."""
        row = self._db.query_one(
            f"{_SELECT} WHERE source_platform = ? AND source_id = ?",
            (source_platform, source_id),
        )
        return row_to_story(row) if row else None

    def find_by_url_fingerprint(
        self, fingerprint: str, *, exclude_id: str | None = None
    ) -> Story | None:
        """Dedup layer 2. Prefers the earliest discovery as the canonical row."""
        row = self._db.query_one(
            f"{_SELECT} WHERE url_fingerprint = ? AND id IS NOT ? "
            "AND duplicate_of_id IS NULL ORDER BY discovered_at ASC, id ASC LIMIT 1",
            (fingerprint, exclude_id),
        )
        return row_to_story(row) if row else None

    def find_by_content_hash(
        self, content_hash: str, *, exclude_id: str | None = None
    ) -> Story | None:
        """Dedup layer 3. Matches across platforms -- an identical repost."""
        row = self._db.query_one(
            f"{_SELECT} WHERE content_hash = ? AND id IS NOT ? "
            "AND duplicate_of_id IS NULL ORDER BY discovered_at ASC, id ASC LIMIT 1",
            (content_hash, exclude_id),
        )
        return row_to_story(row) if row else None

    def find_simhash_candidates(
        self, bands: Sequence[str], *, exclude_id: str | None = None, limit: int = 50
    ) -> list[Story]:
        """Dedup layer 4 candidate set, via the banded LSH index.

        Returns stories sharing at least one identical band. The caller
        confirms with an exact Hamming distance check -- banding is a recall
        filter, not the decision.
        """
        if not bands:
            return []
        conditions = " OR ".join("(b.band_index = ? AND b.band_value = ?)" for _ in bands)
        params: list[Any] = []
        for index, value in enumerate(bands):
            params.extend((index, value))
        params.append(exclude_id)
        params.append(limit)
        rows = self._db.query_all(
            f"SELECT DISTINCT {', '.join('s.' + column for column in _COLUMNS)} "
            "FROM stories s JOIN story_simhash_bands b ON b.story_id = s.id "
            f"WHERE ({conditions}) AND s.id IS NOT ? AND s.duplicate_of_id IS NULL "
            "ORDER BY s.discovered_at ASC, s.id ASC LIMIT ?",
            params,
        )
        return [row_to_story(row) for row in rows]

    def novelty_corpus(
        self, *, limit: int, compare_chars: int, exclude_id: str | None = None
    ) -> list[NoveltyEntry]:
        """The recent-story window the novelty signal compares against.

        Deterministically ordered so the same database state always produces
        the same novelty scores.
        """
        rows = self._db.query_all(
            "SELECT id, title, substr(normalized_content, 1, ?) AS prefix FROM stories "
            "WHERE id IS NOT ? AND status NOT IN ('DUPLICATE', 'REJECTED', 'FAILED') "
            "ORDER BY discovered_at DESC, id DESC LIMIT ?",
            (compare_chars, exclude_id, limit),
        )
        return [
            NoveltyEntry(
                story_id=str(row["id"]),
                title=str(row["title"]),
                content_prefix=str(row["prefix"] or ""),
            )
            for row in rows
        ]

    def iter_by_status(
        self, statuses: Sequence[StoryStatus], *, batch_size: int = 200
    ) -> Iterator[Story]:
        """Stream stories in a set of statuses.

        Paginated by keyset rather than OFFSET so a long ranking pass holds one
        batch in memory at a time and stays correct if rows are written
        concurrently.
        """
        if not statuses:
            return
        placeholders = ", ".join("?" for _ in statuses)
        cursor_key: tuple[str, str] | None = None
        while True:
            if cursor_key is None:
                rows = self._db.query_all(
                    f"{_SELECT} WHERE status IN ({placeholders}) "
                    "ORDER BY discovered_at ASC, id ASC LIMIT ?",
                    [status.value for status in statuses] + [batch_size],
                )
            else:
                rows = self._db.query_all(
                    f"{_SELECT} WHERE status IN ({placeholders}) "
                    "AND (discovered_at, id) > (?, ?) "
                    "ORDER BY discovered_at ASC, id ASC LIMIT ?",
                    [status.value for status in statuses]
                    + [cursor_key[0], cursor_key[1], batch_size],
                )
            if not rows:
                return
            for row in rows:
                yield row_to_story(row)
            last = rows[-1]
            cursor_key = (str(last["discovered_at"]), str(last["id"]))
            if len(rows) < batch_size:
                return

    # --- writes --------------------------------------------------------------

    def upsert(self, story: Story, *, job_id: str | None = None) -> UpsertResult:
        """Insert a new story or refresh an existing one.

        On conflict the mutable fields (content, engagement, metadata) are
        refreshed and `updated_at` moves. `id`, `discovered_at`, `status` and
        the duplicate linkage are left alone: a post that was edited upstream is
        still the same story at the same point in the pipeline.
        """
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            existing_row = connection.execute(
                f"{_SELECT} WHERE source_platform = ? AND source_id = ?",
                (story.source_platform, story.source_id),
            ).fetchone()

            if existing_row is None:
                connection.execute(
                    f"INSERT INTO stories ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    (
                        story.id,
                        story.source_platform,
                        story.source_id,
                        story.canonical_url,
                        story.url_fingerprint,
                        story.author,
                        story.title,
                        story.raw_content,
                        story.normalized_content,
                        story.content_hash,
                        simhash_to_hex(story.simhash) if story.simhash is not None else None,
                        story.word_count,
                        story.language,
                        to_iso(story.created_at),
                        to_iso(story.discovered_at),
                        now,
                        _dump_json(story.engagement.to_dict()),
                        _dump_json(story.metadata),
                        story.status.value,
                        story.duplicate_of_id,
                        story.duplicate_layer.value if story.duplicate_layer else None,
                    ),
                )
                self._write_simhash_bands(connection, story)
                self._append_event(
                    connection,
                    story_id=story.id,
                    from_status=None,
                    to_status=story.status,
                    stage=PipelineStage.PERSIST,
                    job_id=job_id,
                    reason="discovered",
                    occurred_at=now,
                )
                return UpsertResult(story=self._reload(story.id), created=True, updated=False)

            existing = row_to_story(existing_row)
            changed = (
                existing.content_hash != story.content_hash
                or existing.title != story.title
                or existing.engagement != story.engagement
                or dict(existing.metadata) != dict(story.metadata)
            )
            if not changed:
                return UpsertResult(story=existing, created=False, updated=False)

            connection.execute(
                "UPDATE stories SET title = ?, raw_content = ?, normalized_content = ?, "
                "content_hash = ?, simhash = ?, word_count = ?, language = ?, "
                "engagement_json = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                (
                    story.title,
                    story.raw_content,
                    story.normalized_content,
                    story.content_hash,
                    simhash_to_hex(story.simhash) if story.simhash is not None else None,
                    story.word_count,
                    story.language,
                    _dump_json(story.engagement.to_dict()),
                    _dump_json(story.metadata),
                    now,
                    existing.id,
                ),
            )
            if existing.content_hash != story.content_hash:
                # The body was edited upstream; the LSH index must follow it.
                self._write_simhash_bands(connection, story)
            return UpsertResult(story=self._reload(existing.id), created=False, updated=True)

    def _write_simhash_bands(self, connection: sqlite3.Connection, story: Story) -> None:
        connection.execute("DELETE FROM story_simhash_bands WHERE story_id = ?", (story.id,))
        if story.simhash is None:
            return
        for index, value in enumerate(simhash_bands(story.simhash, self._band_count)):
            connection.execute(
                "INSERT INTO story_simhash_bands (story_id, band_index, band_value) "
                "VALUES (?, ?, ?)",
                (story.id, index, value),
            )

    def transition(
        self,
        story_id: str,
        to_status: StoryStatus,
        *,
        stage: PipelineStage,
        job_id: str | None = None,
        reason: str | None = None,
    ) -> Story:
        """Move a story to a new state, validating the edge and logging it.

        The read, the validation, the update and the audit row happen in one
        transaction, so a crash cannot leave a story in a state with no
        corresponding event.
        """
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM stories WHERE id = ?", (story_id,)
            ).fetchone()
            if row is None:
                raise StoryNotFoundError("no such story", story_id=story_id)
            current = StoryStatus(str(row["status"]))
            ensure_transition(story_id, current, to_status)

            connection.execute(
                "UPDATE stories SET status = ?, updated_at = ? WHERE id = ?",
                (to_status.value, now, story_id),
            )
            self._append_event(
                connection,
                story_id=story_id,
                from_status=current,
                to_status=to_status,
                stage=stage,
                job_id=job_id,
                reason=reason,
                occurred_at=now,
            )
        return self._reload(story_id)

    def mark_duplicate(
        self,
        story_id: str,
        *,
        duplicate_of_id: str,
        layer: DedupLayer,
        job_id: str | None = None,
    ) -> Story:
        """Link a story to its original and move it to DUPLICATE."""
        if story_id == duplicate_of_id:
            raise ValueError("a story cannot be a duplicate of itself")
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM stories WHERE id = ?", (story_id,)
            ).fetchone()
            if row is None:
                raise StoryNotFoundError("no such story", story_id=story_id)
            current = StoryStatus(str(row["status"]))
            ensure_transition(story_id, current, StoryStatus.DUPLICATE)

            connection.execute(
                "UPDATE stories SET status = ?, duplicate_of_id = ?, duplicate_layer = ?, "
                "updated_at = ? WHERE id = ?",
                (StoryStatus.DUPLICATE.value, duplicate_of_id, layer.value, now, story_id),
            )
            # A duplicate must never surface as a near-duplicate match for a
            # later story; drop it from the LSH index.
            connection.execute("DELETE FROM story_simhash_bands WHERE story_id = ?", (story_id,))
            self._append_event(
                connection,
                story_id=story_id,
                from_status=current,
                to_status=StoryStatus.DUPLICATE,
                stage=PipelineStage.DEDUPLICATE,
                job_id=job_id,
                reason=f"duplicate of {duplicate_of_id} via {layer.value}",
                occurred_at=now,
            )
        return self._reload(story_id)

    def update_normalization(self, story: Story) -> None:
        """Rewrite derived text fields after a normalizer change.

        Provenance columns are not in the UPDATE list, so `renormalize` cannot
        rewrite a canonical URL even by accident.
        """
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE stories SET normalized_content = ?, content_hash = ?, simhash = ?, "
                "word_count = ?, updated_at = ? WHERE id = ?",
                (
                    story.normalized_content,
                    story.content_hash,
                    simhash_to_hex(story.simhash) if story.simhash is not None else None,
                    story.word_count,
                    now,
                    story.id,
                ),
            )
            self._write_simhash_bands(connection, story)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        story_id: str,
        from_status: StoryStatus | None,
        to_status: StoryStatus,
        stage: PipelineStage,
        job_id: str | None,
        reason: str | None,
        occurred_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO story_state_events "
            "(story_id, from_status, to_status, stage, job_id, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                story_id,
                from_status.value if from_status else None,
                to_status.value,
                stage.value,
                job_id,
                reason,
                occurred_at,
            ),
        )

    def history(self, story_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            "SELECT from_status, to_status, stage, job_id, reason, occurred_at "
            "FROM story_state_events WHERE story_id = ? ORDER BY id ASC LIMIT ?",
            (story_id, limit),
        )
        return [dict(row) for row in rows]

    # --- aggregates ----------------------------------------------------------

    def count_all(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM stories") or 0)

    def count_by_status(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT status, count(*) AS total FROM stories GROUP BY status ORDER BY status"
        )
        return {str(row["status"]): int(row["total"]) for row in rows}

    def count_by_platform(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT source_platform, count(*) AS total FROM stories "
            "GROUP BY source_platform ORDER BY source_platform"
        )
        return {str(row["source_platform"]): int(row["total"]) for row in rows}

    def count_duplicates_by_layer(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT duplicate_layer, count(*) AS total FROM stories "
            "WHERE duplicate_of_id IS NOT NULL GROUP BY duplicate_layer"
        )
        return {str(row["duplicate_layer"] or "unknown"): int(row["total"]) for row in rows}

    def discovered_since(self, since: datetime) -> int:
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM stories WHERE discovered_at >= ?", (to_iso(since),)
            )
            or 0
        )
