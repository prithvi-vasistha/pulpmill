"""Structured logging.

Two sinks with different jobs:

* console -- human-readable (or JSON), at the configured level
* file    -- always JSON lines, rotated, usually at DEBUG

A log line identifies component, stage, source, story_id, job_id and duration
where they apply. Secrets never reach either sink: a redaction processor runs
before any renderer.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from pulpmill.config.models import LoggingConfig

#: Event-dict keys whose values are replaced with a placeholder. Matching is on
#: a lowercased substring, so `reddit_client_secret` and `Authorization` are both
#: caught.
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
)

_REDACTED = "***redacted***"

_configured = False


def _redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace anything that looks like a credential before it is rendered."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
            event_dict[key] = _REDACTED
            continue
        value = event_dict[key]
        if isinstance(value, dict):
            event_dict[key] = {
                inner: (
                    _REDACTED
                    if any(f in str(inner).lower() for f in _SECRET_KEY_FRAGMENTS)
                    else inner_value
                )
                for inner, inner_value in value.items()
            }
    return event_dict


_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.UnicodeDecoder(),
    _redact_secrets,
]


def configure_logging(
    config: LoggingConfig,
    *,
    log_file_path: Path | None = None,
    force: bool = False,
) -> None:
    """Install the logging pipeline. Idempotent unless `force` is set."""
    global _configured
    if _configured and not force:
        return

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console_renderer: Any
    if config.console_format == "json":
        console_renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(config.level)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                console_renderer,
            ],
        )
    )
    root.addHandler(console_handler)

    handler_levels = [logging.getLevelName(config.level)]

    if config.file.enabled and log_file_path is not None:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=config.file.max_bytes,
            backupCount=config.file.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(config.file.level)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=_SHARED_PROCESSORS,
                processors=[
                    structlog.processors.dict_tracebacks,
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(sort_keys=True),
                ],
            )
        )
        root.addHandler(file_handler)
        handler_levels.append(logging.getLevelName(config.file.level))

    # The root logger must pass through anything any handler wants to see.
    root.setLevel(min(int(level) for level in handler_levels))

    # httpx logs every request at INFO, including full URLs. Ours already log
    # the useful parts with structure, so keep its noise out of the console.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def get_logger(component: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to a component name and any extra context.

    Context is passed to `structlog.get_logger` as initial values rather than
    via a `.bind()` call. That distinction matters: `.bind()` materialises the
    logger immediately against whatever configuration is active *at import
    time*, so a module-level logger would permanently keep structlog's default
    console config and its records would never reach the JSON file sink. Initial
    values keep the proxy lazy, so it resolves against the real configuration on
    first use.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(
        component, component=component, **initial
    )
    return logger


@contextmanager
def log_duration(
    logger: structlog.stdlib.BoundLogger, event: str, **context: Any
) -> Iterator[dict[str, Any]]:
    """Log `event` on success or failure with a `duration_ms` measurement.

    Yields a mutable dict; anything put in it is included in the final log line,
    which lets a block report counts it only learns as it runs.
    """
    extra: dict[str, Any] = {}
    started = time.monotonic()
    try:
        yield extra
    except Exception as exc:
        logger.error(
            event,
            outcome="error",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            error_type=type(exc).__name__,
            error=str(exc),
            **context,
            **extra,
        )
        raise
    logger.info(
        event,
        outcome="ok",
        duration_ms=round((time.monotonic() - started) * 1000, 2),
        **context,
        **extra,
    )
