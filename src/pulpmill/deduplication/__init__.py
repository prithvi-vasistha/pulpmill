"""Layered deterministic deduplication."""

from pulpmill.deduplication.engine import DeduplicationEngine, DedupOutcome, DedupResult
from pulpmill.deduplication.strategies import (
    CanonicalUrlStrategy,
    ContentHashStrategy,
    DedupStrategy,
    DuplicateVerdict,
    ExactSourceStrategy,
    SimHashStrategy,
)

__all__ = [
    "CanonicalUrlStrategy",
    "ContentHashStrategy",
    "DedupOutcome",
    "DedupResult",
    "DedupStrategy",
    "DeduplicationEngine",
    "DuplicateVerdict",
    "ExactSourceStrategy",
    "SimHashStrategy",
]
