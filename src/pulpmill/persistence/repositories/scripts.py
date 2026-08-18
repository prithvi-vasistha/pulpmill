"""Narration script persistence.

`save` is an upsert keyed on `(story_id, part_number)`, which is what makes
re-running the script stage an update instead of an accumulation. Regenerating
under changed settings overwrites the row and moves `config_fingerprint`, so
"which settings produced this script" always has one answer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence

from pulpmill.domain.script import LineRole, NarrationScript, ScriptLine
from pulpmill.domain.story import Provenance
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from pulpmill.persistence.database import Database


def _dump_lines(lines: Sequence[ScriptLine]) -> str:
    payload = [
        {
            "index": line.index,
            "role": line.role.value,
            "text": line.text,
            "speech_text": line.speech_text,
            "paragraph_break": line.paragraph_break,
        }
        for line in lines
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _load_lines(raw: str) -> tuple[ScriptLine, ...]:
    return tuple(
        ScriptLine(
            index=int(item["index"]),
            role=LineRole(item["role"]),
            text=str(item["text"]),
            speech_text=str(item["speech_text"]),
            paragraph_break=bool(item["paragraph_break"]),
        )
        for item in json.loads(raw)
    )


def row_to_script(row: sqlite3.Row, provenance: Provenance) -> NarrationScript:
    return NarrationScript(
        id=str(row["id"]),
        story_id=str(row["story_id"]),
        part_number=int(row["part_number"]),
        total_parts=int(row["total_parts"]),
        series_id=row["series_id"],
        part_id=row["part_id"],
        provenance=provenance,
        title=str(row["title"]),
        lines=_load_lines(str(row["lines_json"])),
        generator=str(row["generator"]),
        generator_version=str(row["generator_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        created_at=from_iso(str(row["created_at"])),
        metadata=json.loads(row["metadata_json"]),
    )


class ScriptRepository:
    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def save(
        self,
        script: NarrationScript,
        *,
        requested_provider: str,
        fallback_reason: str | None = None,
        notes: str = "",
        words_per_minute: float,
    ) -> str:
        now = to_iso(self._clock.now())
        word_count = sum(len(line.speech_text.split()) for line in script.lines)
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO story_scripts (id, story_id, series_id, part_id, part_number, "
                "total_parts, title, lines_json, word_count, estimated_seconds, generator, "
                "requested_provider, fallback_reason, generator_version, config_fingerprint, "
                "notes, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (story_id, part_number) DO UPDATE SET "
                "series_id = excluded.series_id, part_id = excluded.part_id, "
                "total_parts = excluded.total_parts, title = excluded.title, "
                "lines_json = excluded.lines_json, word_count = excluded.word_count, "
                "estimated_seconds = excluded.estimated_seconds, generator = excluded.generator, "
                "requested_provider = excluded.requested_provider, "
                "fallback_reason = excluded.fallback_reason, "
                "generator_version = excluded.generator_version, "
                "config_fingerprint = excluded.config_fingerprint, notes = excluded.notes, "
                "metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
                (
                    script.id,
                    script.story_id,
                    script.series_id,
                    script.part_id,
                    script.part_number,
                    script.total_parts,
                    script.title,
                    _dump_lines(script.lines),
                    word_count,
                    script.estimated_seconds(words_per_minute=words_per_minute),
                    script.generator,
                    requested_provider,
                    fallback_reason,
                    script.generator_version,
                    script.config_fingerprint,
                    notes or None,
                    json.dumps(dict(script.metadata), sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return script.id

    def get(self, script_id: str, provenance: Provenance) -> NarrationScript | None:
        row = self._db.query_one("SELECT * FROM story_scripts WHERE id = ?", (script_id,))
        return row_to_script(row, provenance) if row else None

    def for_story(self, story_id: str, provenance: Provenance) -> list[NarrationScript]:
        rows = self._db.query_all(
            "SELECT * FROM story_scripts WHERE story_id = ? ORDER BY part_number ASC",
            (story_id,),
        )
        return [row_to_script(row, provenance) for row in rows]

    def iter_awaiting(self, table: str, *, batch_size: int = 50) -> Iterator[sqlite3.Row]:
        """Scripts with no row yet in `table` -- the work queue for a stage.

        `table` is restricted to a known set rather than interpolated freely:
        it reaches SQL directly, and scraped data must never be able to steer
        it even indirectly.
        """
        if table not in {"audio_artifacts", "video_artifacts"}:
            raise ValueError(f"unsupported downstream table: {table!r}")
        offset = 0
        while True:
            rows = self._db.query_all(
                # `table` is allow-listed above; it never carries caller input.
                f"SELECT s.* FROM story_scripts s "
                f"LEFT JOIN {table} d ON d.script_id = s.id "
                "WHERE d.id IS NULL ORDER BY s.created_at ASC, s.id ASC LIMIT ? OFFSET ?",
                (batch_size, offset),
            )
            if not rows:
                return
            yield from rows
            offset += len(rows)
            if len(rows) < batch_size:
                return

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM story_scripts") or 0)

    def count_stale(self, config_fingerprint: str) -> int:
        """Scripts produced under settings other than the current ones."""
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM story_scripts WHERE config_fingerprint <> ?",
                (config_fingerprint,),
            )
            or 0
        )
