"""Editorial selection: an optional AI pass over a small candidate set.

The pipeline never depends on this. `DeterministicProvider` is the default and
the fallback, and it works with no key, no network and no model.
"""

from pulpmill.editorial.claude import ClaudeProvider
from pulpmill.editorial.deterministic import DeterministicProvider
from pulpmill.editorial.provider import (
    EditorialCandidate,
    EditorialDecision,
    EditorialProvider,
    SelectedStory,
)
from pulpmill.editorial.schema import (
    SELECTION_JSON_SCHEMA,
    EditorialResponse,
    parse_json_payload,
    validate_selection,
)
from pulpmill.editorial.service import EditorialSelector, SelectionResult, build_provider

__all__ = [
    "SELECTION_JSON_SCHEMA",
    "ClaudeProvider",
    "DeterministicProvider",
    "EditorialCandidate",
    "EditorialDecision",
    "EditorialProvider",
    "EditorialResponse",
    "EditorialSelector",
    "SelectedStory",
    "SelectionResult",
    "build_provider",
    "parse_json_payload",
    "validate_selection",
]
