"""Ingestion: source adapters and the registry that resolves them.

Importing this package has the side effect of registering the built-in
adapters, which is what makes `sources.<name>.adapter` resolvable.
"""

from pulpmill.ingestion import adapters as _adapters  # noqa: F401  (registration side effect)
from pulpmill.ingestion.base import QUALITY_KEY, build_story
from pulpmill.ingestion.registry import (
    AdapterContext,
    build_adapters,
    create_adapter,
    register_adapter,
    registered_adapters,
)

__all__ = [
    "QUALITY_KEY",
    "AdapterContext",
    "build_adapters",
    "build_story",
    "create_adapter",
    "register_adapter",
    "registered_adapters",
]
