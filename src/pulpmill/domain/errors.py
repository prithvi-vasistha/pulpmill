"""Exception hierarchy.

Every error carries enough structure for the failure to be persisted and
queried later: which source, which story, which stage, which operation. Code
must never swallow an exception silently -- either handle it and record a
`job_failures` row, or let it propagate.
"""

from __future__ import annotations

from typing import Any


class PulpmillError(Exception):
    """Base class for every error raised by this application."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = {k: v for k, v in context.items() if v is not None}

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


# --- Configuration -----------------------------------------------------------


class ConfigError(PulpmillError):
    """Configuration file is missing, malformed, or fails validation."""


# --- Persistence -------------------------------------------------------------


class PersistenceError(PulpmillError):
    """Database operation failed."""


class MigrationError(PersistenceError):
    """Schema migration could not be applied."""


class StoryNotFoundError(PersistenceError):
    """No story exists with the requested identifier."""


# --- Domain rules ------------------------------------------------------------


class InvalidStateTransitionError(PulpmillError):
    """A story was asked to move between two states that are not connected."""

    def __init__(self, story_id: str, from_status: str, to_status: str) -> None:
        super().__init__(
            "invalid story state transition",
            story_id=story_id,
            from_status=from_status,
            to_status=to_status,
        )
        self.from_status = from_status
        self.to_status = to_status


# --- Ingestion ---------------------------------------------------------------


class IngestionError(PulpmillError):
    """Base class for source acquisition failures."""


class SourceUnavailableError(IngestionError):
    """A source cannot be used at all -- missing credentials, disabled, retired.

    Distinct from a transient fetch failure: retrying will not help until the
    operator changes something. The pipeline skips the source and continues.
    """


class SourceRequestError(IngestionError):
    """An HTTP request to a source failed after the retry policy was exhausted."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        attempts: int | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, url=url, status_code=status_code, attempts=attempts, **context)
        self.url = url
        self.status_code = status_code
        self.attempts = attempts


class SourceResponseError(IngestionError):
    """A source returned a response we could not interpret.

    Raised for malformed JSON, a payload shape that does not match the
    documented API, or a record missing fields required to build provenance.
    """


class UnknownSourceError(IngestionError):
    """No adapter is registered under the requested platform name."""


# --- Editorial ---------------------------------------------------------------


class EditorialError(PulpmillError):
    """Base class for editorial-selection failures."""


class EditorialProviderUnavailableError(EditorialError):
    """The configured editorial provider cannot run (no API key, missing package)."""


class EditorialResponseError(EditorialError):
    """The editorial provider returned output that failed validation."""


# --- Production --------------------------------------------------------------


class ScriptError(PulpmillError):
    """Base class for script-generation failures."""


class ScriptProviderUnavailableError(ScriptError):
    """The configured script provider cannot run (no API key, missing package)."""


class ScriptResponseError(ScriptError):
    """A script provider returned advice that failed validation."""


class StoryTooLongError(ScriptError):
    """A story needs more parts than the configuration permits.

    Not a bug and not a transient failure: the story is set aside rather than
    published as a series nobody finishes.
    """


class SynthesisError(PulpmillError):
    """Base class for text-to-speech failures."""


class TTSUnavailableError(SynthesisError):
    """No usable speech provider -- model weights missing, or an unknown voice."""


class RenderError(PulpmillError):
    """Base class for video composition failures."""


class AssetError(RenderError):
    """A required render asset is missing, unreadable, or unusable."""


class FFmpegError(RenderError):
    """An ffmpeg or ffprobe invocation failed.

    Carries the tail of stderr rather than the whole stream: ffmpeg is verbose
    and the diagnosis is always in the last few lines, but the full output would
    swamp a log record.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        returncode: int | None = None,
        stderr_tail: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            command=command,
            returncode=returncode,
            stderr_tail=stderr_tail,
            **context,
        )
        self.command = command
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class VideoValidationError(PulpmillError):
    """A rendered file failed one or more publishability checks."""

    def __init__(self, message: str, *, failures: tuple[str, ...] = (), **context: Any) -> None:
        super().__init__(message, failures=", ".join(failures) or None, **context)
        self.failures = failures


class PublishError(PulpmillError):
    """Base class for publishing failures."""


class PublisherUnavailableError(PublishError):
    """A publishing target cannot run -- disabled, unauthenticated, unapproved."""


class PublishRejectedError(PublishError):
    """A platform accepted the request and refused the content."""
