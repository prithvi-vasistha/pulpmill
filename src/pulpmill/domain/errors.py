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
