"""Adapter registry.

The one place that maps a configured `adapter:` name to an implementation.
Nothing else in the codebase branches on a platform name -- downstream stages
take a `SourceAdapter` or read per-platform values from config.

Adding a source is: write the adapter, register it here, add a `sources:` block.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pulpmill.config.models import AppConfig, SourceConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.errors import UnknownSourceError
from pulpmill.domain.source import SourceAdapter
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Everything an adapter needs, injected rather than reached for.

    `transport` exists so tests can hand an adapter a mocked HTTP transport and
    exercise the real fetch/normalize code without touching the network.
    """

    name: str
    config: AppConfig
    source_config: SourceConfig
    secrets: SecretStore
    clock: Clock = SYSTEM_CLOCK
    rng: random.Random | None = None
    transport: object | None = None


AdapterFactory = Callable[[AdapterContext], SourceAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """Register an adapter factory. Re-registering the same name replaces it."""
    _REGISTRY[name] = factory


def registered_adapters() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def create_adapter(context: AdapterContext) -> SourceAdapter:
    factory = _REGISTRY.get(context.source_config.adapter)
    if factory is None:
        raise UnknownSourceError(
            "no adapter registered under that name",
            adapter=context.source_config.adapter,
            source=context.name,
            available=", ".join(registered_adapters()),
        )
    return factory(context)


def build_adapters(
    config: AppConfig,
    *,
    secrets: SecretStore,
    clock: Clock = SYSTEM_CLOCK,
    only: Mapping[str, SourceConfig] | None = None,
    rng: random.Random | None = None,
) -> dict[str, SourceAdapter]:
    """Instantiate one adapter per selected source."""
    selected = only if only is not None else config.enabled_sources()
    adapters: dict[str, SourceAdapter] = {}
    for name, source_config in selected.items():
        adapters[name] = create_adapter(
            AdapterContext(
                name=name,
                config=config,
                source_config=source_config,
                secrets=secrets,
                clock=clock,
                rng=rng,
            )
        )
    return adapters
