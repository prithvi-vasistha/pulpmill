"""Audio and video artifacts, and the timing model that connects them.

`WordTiming` lives here rather than beside the TTS provider because three
layers need it -- synthesis produces it, captions consume it, validation checks
it -- and none of them should have to import a provider package to name a type.

Every artifact carries `provenance` and the id of the script it was built from,
so the chain video -> script -> part -> story -> source URL is walkable from any
link without a database round trip.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pulpmill.domain.story import Provenance

#: Fixed namespaces for deterministic artifact identifiers. Never change these.
AUDIO_ID_NAMESPACE = uuid.UUID("9a3f1d57-2c84-5e60-b17a-4f9d3e8c2b15")
VIDEO_ID_NAMESPACE = uuid.UUID("47b8e2c0-5d19-5f7a-8e34-1a6c9b0d5e82")


def build_audio_id(script_id: str) -> str:
    return str(uuid.uuid5(AUDIO_ID_NAMESPACE, script_id))


def build_video_id(script_id: str) -> str:
    return str(uuid.uuid5(VIDEO_ID_NAMESPACE, script_id))


@dataclass(frozen=True, slots=True)
class WordTiming:
    """When one word is spoken, in seconds from the start of the clip."""

    word: str
    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if self.start_seconds < 0:
            raise ValueError(f"word {self.word!r} starts before zero")
        if self.end_seconds < self.start_seconds:
            raise ValueError(f"word {self.word!r} ends before it starts")

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def shifted(self, offset: float) -> WordTiming:
        """Same word, moved along the timeline. Used when clips are joined."""
        return WordTiming(
            word=self.word,
            start_seconds=self.start_seconds + offset,
            end_seconds=self.end_seconds + offset,
        )


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """One on-screen caption: a few words shown together.

    `words` is kept alongside `text` because karaoke-style highlighting needs
    per-word boundaries, and because a cue whose words are missing must still
    render as plain text rather than failing.
    """

    index: int
    start_seconds: float
    end_seconds: float
    text: str
    words: tuple[WordTiming, ...] = ()

    def __post_init__(self) -> None:
        if self.end_seconds <= self.start_seconds:
            raise ValueError(f"cue {self.index} has non-positive duration")
        if not self.text.strip():
            raise ValueError(f"cue {self.index} has no text")

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class AudioArtifact:
    """A synthesised narration track.

    `word_timings` may be empty: not every provider can align, and the caption
    stage degrades to even distribution rather than refusing to caption.
    """

    id: str
    script_id: str
    story_id: str
    path: Path
    duration_seconds: float
    sample_rate: int
    voice_id: str
    provider: str
    model_version: str
    provenance: Provenance
    word_timings: tuple[WordTiming, ...] = ()
    created_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_alignment(self) -> bool:
        return bool(self.word_timings)

    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True, slots=True)
class VideoArtifact:
    """A rendered, muxed video file."""

    id: str
    script_id: str
    story_id: str
    audio_id: str
    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float
    size_bytes: int
    encoder: str
    #: Where the moving background came from -- a library clip's name, or
    #: "procedural". Recorded so a batch of videos sharing footage is findable.
    background_source: str
    #: Hash of the script/tts/caption/render configuration that produced this.
    #: A mismatch means the file is stale relative to current settings.
    production_fingerprint: str
    provenance: Provenance
    created_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0

    def exists(self) -> bool:
        return self.path.is_file()


def total_duration(timings: Sequence[WordTiming]) -> float:
    """End of the last word, or 0 for an empty alignment."""
    return max((timing.end_seconds for timing in timings), default=0.0)
