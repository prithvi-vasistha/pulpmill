"""Enumerations shared across the pipeline.

Note what is deliberately *not* an enum: the source platform. Platform names are
plain strings validated against the adapter registry, so adding a source means
adding an adapter module -- not editing a core enum and a database CHECK
constraint.
"""

from __future__ import annotations

from enum import StrEnum


class PipelineStage(StrEnum):
    """Stage labels used in logs, failure records and state events."""

    FETCH = "fetch"
    NORMALIZE = "normalize"
    DEDUPLICATE = "deduplicate"
    PERSIST = "persist"
    RANK = "rank"
    SELECT = "select"
    SCRIPT = "script"
    TTS = "tts"
    RENDER = "render"
    VALIDATE = "validate"
    PUBLISH = "publish"


class DedupLayer(StrEnum):
    """Which deduplication layer matched.

    Ordered cheapest-first; the engine stops at the first match.
    """

    EXACT_SOURCE = "exact_source"
    CANONICAL_URL = "canonical_url"
    CONTENT_HASH = "content_hash"
    NEAR_DUPLICATE = "near_duplicate"


class StoryStatus(StrEnum):
    """Persistent lifecycle state of a story.

    States past RANKED are declared now so the schema and state machine do not
    need to change when the script/audio/video workers are added. Only the
    states through SELECTED are exercised today.
    """

    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    DEDUPLICATED = "DEDUPLICATED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    RANKED = "RANKED"
    SELECTED = "SELECTED"
    SCRIPT_PENDING = "SCRIPT_PENDING"
    SCRIPT_READY = "SCRIPT_READY"
    AUDIO_PENDING = "AUDIO_PENDING"
    AUDIO_READY = "AUDIO_READY"
    VIDEO_PENDING = "VIDEO_PENDING"
    VIDEO_READY = "VIDEO_READY"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    """Lifecycle of a pipeline run."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: The process died without finishing; detected on a later run.
    INTERRUPTED = "INTERRUPTED"


class SeriesStatus(StrEnum):
    """Lifecycle of a multi-part series. Reserved for the rendering stages."""

    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    ABANDONED = "ABANDONED"
