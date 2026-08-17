"""Editorial response schema and validation.

Model output is untrusted input. It is validated three times over:

1. The API is asked for JSON matching a schema (structured outputs).
2. The parsed payload is validated against a Pydantic model.
3. The *semantics* are checked here -- every story id must be one we actually
   offered, positions must be a contiguous 1..N permutation, and the count must
   match what we asked for.

Step 3 is the one that matters. A syntactically perfect response that invents a
story id, or ranks the same story twice, is still wrong, and acting on it would
produce a video for a story that does not exist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulpmill.domain.errors import EditorialResponseError

#: JSON Schema handed to the API's structured-output mode. Deliberately flat and
#: free of the constraint keywords structured outputs does not support
#: (`minimum`, `maxLength`, ...) -- ranges are expressed as enums, and
#: everything else is enforced by `validate_selection` below.
SELECTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "story_id": {
                        "type": "string",
                        "description": "Must be one of the candidate ids provided.",
                    },
                    "position": {
                        "type": "integer",
                        "description": "Publication order, starting at 1, no gaps or repeats.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence explaining the placement.",
                    },
                    "hook_strength": {
                        "type": "integer",
                        "enum": [1, 2, 3, 4, 5],
                        "description": "How strongly the opening grabs attention.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Short topic label, used to check topic diversity.",
                    },
                },
                "required": [
                    "story_id",
                    "position",
                    "rationale",
                    "hook_strength",
                    "category",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "string",
            "description": "Brief overall reasoning about the ordering.",
        },
    },
    "required": ["selections", "notes"],
    "additionalProperties": False,
}


class SelectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: str
    position: int = Field(ge=1)
    rationale: str = ""
    hook_strength: int = Field(default=3, ge=1, le=5)
    category: str = ""


class EditorialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[SelectionItem]
    notes: str = ""


def parse_json_payload(text: str) -> Any:
    """Parse a JSON document, tolerating a markdown code fence.

    Structured outputs should make fences impossible, but a provider that falls
    back to plain text still needs to be handled rather than crashing.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise EditorialResponseError(
            "editorial provider did not return valid JSON",
            detail=str(exc),
            preview=stripped[:200],
        ) from exc


def validate_selection(
    payload: Any,
    *,
    allowed_ids: Sequence[str],
    expected_count: int,
) -> EditorialResponse:
    """Validate a parsed editorial payload, or raise `EditorialResponseError`.

    Enforces, in order: shape, known story ids, no duplicates, exactly
    `expected_count` items, and positions forming 1..N with no gaps.
    """
    if not isinstance(payload, Mapping):
        raise EditorialResponseError(
            "editorial response must be a JSON object",
            found=type(payload).__name__,
        )

    try:
        response = EditorialResponse.model_validate(payload)
    except ValidationError as exc:
        raise EditorialResponseError(
            "editorial response failed schema validation",
            detail="; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            ),
        ) from exc

    allowed = set(allowed_ids)
    seen_ids: set[str] = set()

    for item in response.selections:
        if item.story_id not in allowed:
            raise EditorialResponseError(
                "editorial response referenced a story that was not a candidate",
                story_id=item.story_id,
            )
        if item.story_id in seen_ids:
            raise EditorialResponseError(
                "editorial response selected the same story twice",
                story_id=item.story_id,
            )
        seen_ids.add(item.story_id)

    if len(response.selections) != expected_count:
        raise EditorialResponseError(
            "editorial response returned the wrong number of selections",
            expected=expected_count,
            received=len(response.selections),
        )

    positions = sorted(item.position for item in response.selections)
    if positions != list(range(1, expected_count + 1)):
        raise EditorialResponseError(
            "editorial response positions must be 1..N with no gaps or repeats",
            positions=positions,
        )

    return response
