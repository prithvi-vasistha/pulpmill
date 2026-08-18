"""Audio, video and validation persistence.

Paths are stored relative to the project root wherever they sit inside it. A
database full of `/home/ppv/...` would break the moment the project moved, and
"machine-specific paths in stored data" is the same mistake as machine-specific
paths in code, one layer down.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulpmill.domain.media import AudioArtifact, VideoArtifact, WordTiming
from pulpmill.domain.story import Provenance
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from pulpmill.persistence.database import Database


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_relative(path: Path, root: Path) -> str:
    """Store a path relative to the project root when it lives inside it."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        # Outside the project (an absolute cache dir, say). Absolute is correct
        # here -- a relative path would silently resolve somewhere else.
        return str(path)


def from_relative(stored: str, root: Path) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else root / path


def _dump_timings(timings: Sequence[WordTiming]) -> str:
    return json.dumps(
        [
            [timing.word, round(timing.start_seconds, 4), round(timing.end_seconds, 4)]
            for timing in timings
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _load_timings(raw: str) -> tuple[WordTiming, ...]:
    return tuple(
        WordTiming(word=str(item[0]), start_seconds=float(item[1]), end_seconds=float(item[2]))
        for item in json.loads(raw)
    )


class AudioRepository:
    def __init__(self, database: Database, root: Path, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._root = root
        self._clock = clock

    def save(self, artifact: AudioArtifact, *, cache_key: str) -> str:
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO audio_artifacts (id, script_id, story_id, path, duration_seconds, "
                "sample_rate, voice_id, provider, model_version, cache_key, word_timings_json, "
                "metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (script_id) DO UPDATE SET path = excluded.path, "
                "duration_seconds = excluded.duration_seconds, "
                "sample_rate = excluded.sample_rate, voice_id = excluded.voice_id, "
                "provider = excluded.provider, model_version = excluded.model_version, "
                "cache_key = excluded.cache_key, "
                "word_timings_json = excluded.word_timings_json, "
                "metadata_json = excluded.metadata_json, created_at = excluded.created_at",
                (
                    artifact.id,
                    artifact.script_id,
                    artifact.story_id,
                    to_relative(artifact.path, self._root),
                    artifact.duration_seconds,
                    artifact.sample_rate,
                    artifact.voice_id,
                    artifact.provider,
                    artifact.model_version,
                    cache_key,
                    _dump_timings(artifact.word_timings),
                    _dump(dict(artifact.metadata)),
                    now,
                ),
            )
        return artifact.id

    def for_script(self, script_id: str, provenance: Provenance) -> AudioArtifact | None:
        row = self._db.query_one("SELECT * FROM audio_artifacts WHERE script_id = ?", (script_id,))
        return self._row_to_audio(row, provenance) if row else None

    def _row_to_audio(self, row: sqlite3.Row, provenance: Provenance) -> AudioArtifact:
        return AudioArtifact(
            id=str(row["id"]),
            script_id=str(row["script_id"]),
            story_id=str(row["story_id"]),
            path=from_relative(str(row["path"]), self._root),
            duration_seconds=float(row["duration_seconds"]),
            sample_rate=int(row["sample_rate"]),
            voice_id=str(row["voice_id"]),
            provider=str(row["provider"]),
            model_version=str(row["model_version"]),
            provenance=provenance,
            word_timings=_load_timings(str(row["word_timings_json"])),
            created_at=from_iso(str(row["created_at"])),
            metadata=json.loads(row["metadata_json"]),
        )

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM audio_artifacts") or 0)

    def total_seconds(self) -> float:
        return float(
            self._db.query_scalar("SELECT coalesce(sum(duration_seconds), 0) FROM audio_artifacts")
            or 0.0
        )


class VideoRepository:
    def __init__(self, database: Database, root: Path, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._root = root
        self._clock = clock

    def save(self, artifact: VideoArtifact) -> str:
        now = to_iso(self._clock.now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO video_artifacts (id, script_id, story_id, audio_id, path, "
                "duration_seconds, width, height, fps, size_bytes, encoder, background_source, "
                "production_fingerprint, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (script_id) DO UPDATE SET audio_id = excluded.audio_id, "
                "path = excluded.path, duration_seconds = excluded.duration_seconds, "
                "width = excluded.width, height = excluded.height, fps = excluded.fps, "
                "size_bytes = excluded.size_bytes, encoder = excluded.encoder, "
                "background_source = excluded.background_source, "
                "production_fingerprint = excluded.production_fingerprint, "
                "metadata_json = excluded.metadata_json, created_at = excluded.created_at",
                (
                    artifact.id,
                    artifact.script_id,
                    artifact.story_id,
                    artifact.audio_id,
                    to_relative(artifact.path, self._root),
                    artifact.duration_seconds,
                    artifact.width,
                    artifact.height,
                    artifact.fps,
                    artifact.size_bytes,
                    artifact.encoder,
                    artifact.background_source,
                    artifact.production_fingerprint,
                    _dump(dict(artifact.metadata)),
                    now,
                ),
            )
        return artifact.id

    def for_script(self, script_id: str, provenance: Provenance) -> VideoArtifact | None:
        row = self._db.query_one("SELECT * FROM video_artifacts WHERE script_id = ?", (script_id,))
        return self._row_to_video(row, provenance) if row else None

    def get(self, video_id: str, provenance: Provenance) -> VideoArtifact | None:
        row = self._db.query_one("SELECT * FROM video_artifacts WHERE id = ?", (video_id,))
        return self._row_to_video(row, provenance) if row else None

    def _row_to_video(self, row: sqlite3.Row, provenance: Provenance) -> VideoArtifact:
        return VideoArtifact(
            id=str(row["id"]),
            script_id=str(row["script_id"]),
            story_id=str(row["story_id"]),
            audio_id=str(row["audio_id"]),
            path=from_relative(str(row["path"]), self._root),
            duration_seconds=float(row["duration_seconds"]),
            width=int(row["width"]),
            height=int(row["height"]),
            fps=float(row["fps"]),
            size_bytes=int(row["size_bytes"]),
            encoder=str(row["encoder"]),
            background_source=str(row["background_source"]),
            production_fingerprint=str(row["production_fingerprint"]),
            provenance=provenance,
            created_at=from_iso(str(row["created_at"])),
            metadata=json.loads(row["metadata_json"]),
        )

    def count(self) -> int:
        return int(self._db.query_scalar("SELECT count(*) FROM video_artifacts") or 0)

    def count_stale(self, production_fingerprint: str) -> int:
        return int(
            self._db.query_scalar(
                "SELECT count(*) FROM video_artifacts WHERE production_fingerprint <> ?",
                (production_fingerprint,),
            )
            or 0
        )

    def background_usage(self, *, limit: int = 20) -> list[tuple[str, int]]:
        """How often each background clip has been used. Variety auditing."""
        rows = self._db.query_all(
            "SELECT background_source, count(*) AS uses FROM video_artifacts "
            "GROUP BY background_source ORDER BY uses DESC, background_source ASC LIMIT ?",
            (limit,),
        )
        return [(str(row["background_source"]), int(row["uses"])) for row in rows]


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    video_id: str
    story_id: str
    passed: bool
    checks: Mapping[str, Any]
    failures: tuple[str, ...]
    validated_at: str


class ValidationRepository:
    """Append-only validation verdicts."""

    def __init__(self, database: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self._db = database
        self._clock = clock

    def record(
        self,
        *,
        video_id: str,
        story_id: str,
        passed: bool,
        checks: Mapping[str, Any],
        failures: Sequence[str],
    ) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO video_validations (video_id, story_id, passed, checks_json, "
                "failures_json, validated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    video_id,
                    story_id,
                    1 if passed else 0,
                    _dump(dict(checks)),
                    _dump(list(failures)),
                    to_iso(self._clock.now()),
                ),
            )

    def latest(self, video_id: str) -> ValidationRecord | None:
        row = self._db.query_one(
            "SELECT * FROM video_validations WHERE video_id = ? ORDER BY id DESC LIMIT 1",
            (video_id,),
        )
        if row is None:
            return None
        return ValidationRecord(
            video_id=str(row["video_id"]),
            story_id=str(row["story_id"]),
            passed=bool(row["passed"]),
            checks=json.loads(row["checks_json"]),
            failures=tuple(json.loads(row["failures_json"])),
            validated_at=str(row["validated_at"]),
        )

    def counts(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT passed, count(*) AS total FROM ("
            "  SELECT video_id, passed, max(id) FROM video_validations GROUP BY video_id"
            ") GROUP BY passed"
        )
        result = {"passed": 0, "failed": 0}
        for row in rows:
            result["passed" if row["passed"] else "failed"] = int(row["total"])
        return result
