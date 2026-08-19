"""The publisher contract and its registry.

Same shape as the source adapters, for the same reason: adding a platform must
mean adding a module, not editing a chain of `if target == "youtube"` branches
through the pipeline. The service below never learns what a YouTube `snippet` is
or that Instagram fetches files over HTTP instead of accepting an upload.

Every adapter is expected to be honest in `health()`. All three of these
platforms have an approval gate that no amount of retrying will clear, so
"cannot publish, and here is the specific step that unblocks it" is more useful
than any error raised at upload time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pulpmill.config.models import AppConfig, PublishTargetConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.errors import PublishError
from pulpmill.domain.publishing import (
    PublisherHealth,
    PublishRequest,
    PublishResult,
    VideoMetadata,
)
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock


@dataclass(frozen=True, slots=True)
class PublisherContext:
    """Everything an adapter is constructed from.

    `transport` exists so tests can hand an adapter a mocked HTTP transport and
    exercise the real request construction, auth and error paths without
    contacting a platform. It mirrors `AdapterContext` on the ingestion side.
    """

    name: str
    config: AppConfig
    target: PublishTargetConfig
    secrets: SecretStore
    clock: Clock = SYSTEM_CLOCK
    transport: object | None = None


@runtime_checkable
class Publisher(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def platform(self) -> str: ...

    def health(self) -> PublisherHealth:
        """Whether this target can publish right now, and what unblocks it."""
        ...

    def publish(self, request: PublishRequest) -> PublishResult:
        """Upload one video. Raises `PublishError` on failure."""
        ...

    def update_metadata(self, remote_id: str, metadata: VideoMetadata) -> bool:
        """Rewrite an already-published video's title and description.

        Returns False when the platform has no such capability -- Instagram and
        TikTok both publish captions immutably, so a series index can only ever
        point backwards there. Only YouTube can be corrected after the fact.

        Not raising for the unsupported case is deliberate: "this platform
        cannot do it" is a fact about the platform, not a failure of the run.
        """
        ...

    def close(self) -> None: ...


PublisherFactory = Callable[[PublisherContext], Publisher]

_REGISTRY: dict[str, PublisherFactory] = {}


def register_publisher(name: str, factory: PublisherFactory) -> None:
    """Register an adapter under a configuration key."""
    if name in _REGISTRY:
        raise ValueError(f"a publisher is already registered as {name!r}")
    _REGISTRY[name] = factory


def create_publisher(context: PublisherContext) -> Publisher:
    _load_builtin_publishers()
    factory = _REGISTRY.get(context.target.adapter)
    if factory is None:
        raise PublishError(
            "no publisher registered under this adapter name",
            adapter=context.target.adapter,
            available=", ".join(sorted(_REGISTRY)) or "none",
        )
    return factory(context)


def available_publishers() -> tuple[str, ...]:
    _load_builtin_publishers()
    return tuple(sorted(_REGISTRY))


def _load_builtin_publishers() -> None:
    """Import the shipped adapters, which register themselves on import.

    Deferred rather than done at module import so that a broken adapter cannot
    stop the whole application from starting.
    """
    if _REGISTRY:
        return
    from pulpmill.publishing.adapters import instagram, tiktok, youtube  # noqa: F401


def redacted(payload: Mapping[str, object]) -> dict[str, object]:
    """Strip anything credential-shaped before a payload is logged or stored.

    Adapters build their own auth and it never enters a `PublishRequest`, so
    this is a backstop rather than the primary defence -- but a backstop worth
    having on a path that writes to the database.
    """
    blocked = {"access_token", "refresh_token", "client_secret", "authorization", "token"}
    return {
        key: ("<redacted>" if key.lower() in blocked else value) for key, value in payload.items()
    }
