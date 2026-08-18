"""Script-guidance response schema and validation.

Model output is untrusted input, and the checks here are the ones that matter:
a syntactically perfect response proposing a cut after sentence 900 of a
40-sentence story is still wrong, and acting on it would produce a part that
does not exist.

Anything that fails validation raises `ScriptResponseError`, which the service
turns into "ignore the advice and use the deterministic plan". The advice is
never partially honoured -- half a rejected plan is a plan nobody chose.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulpmill.domain.errors import ScriptResponseError
from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance, validate_cut_points

#: Longest hook we will accept. A hook is the first two seconds of narration;
#: anything longer is a summary, and the model has misunderstood the task.
MAX_HOOK_CHARS = 220
MAX_TITLE_CHARS = 140

#: JSON Schema for the API's structured-output mode. Flat, and free of the
#: constraint keywords structured outputs does not support -- lengths and ranges
#: are enforced by `validate_guidance` below.
SCRIPT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hook": {
            "type": "string",
            "description": (
                "The opening line of narration, one sentence. It must be faithful to "
                "the story and must not invent events. Return an empty string to keep "
                "the original title as the hook."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "A short on-screen title. Return an empty string to keep the original."
            ),
        },
        "cut_after": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Sentence indices after which a new part should begin, strictly "
                "ascending. Return an empty array to accept the default even split."
            ),
        },
        "notes": {
            "type": "string",
            "description": "One sentence on the pacing choice. Not narrated.",
        },
    },
    "required": ["hook", "title", "cut_after", "notes"],
    "additionalProperties": False,
}


class _GuidancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook: str = ""
    title: str = ""
    cut_after: list[int] = Field(default_factory=list)
    notes: str = ""


def parse_json_payload(text: str) -> Any:
    """Parse a model response body, failing loudly rather than guessing."""
    stripped = text.strip()
    if not stripped:
        raise ScriptResponseError("script provider returned an empty response")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ScriptResponseError(
            "script provider returned text that is not valid JSON",
            detail=str(exc),
        ) from exc


def validate_guidance(payload: Any, *, brief: ScriptBrief) -> ScriptGuidance:
    """Check advice against the story it was given. Raises on any problem."""
    try:
        parsed = _GuidancePayload.model_validate(payload)
    except ValidationError as exc:
        raise ScriptResponseError(
            "script guidance did not match the expected shape",
            detail=exc.error_count(),
        ) from exc

    hook = parsed.hook.strip()
    if len(hook) > MAX_HOOK_CHARS:
        raise ScriptResponseError(
            "proposed hook is too long to be an opening line",
            length=len(hook),
            limit=MAX_HOOK_CHARS,
        )
    title = parsed.title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise ScriptResponseError(
            "proposed title is too long", length=len(title), limit=MAX_TITLE_CHARS
        )

    try:
        cuts = validate_cut_points(
            parsed.cut_after,
            sentence_count=len(brief.sentences),
            max_parts=brief.max_parts,
        )
    except ValueError as exc:
        raise ScriptResponseError(f"proposed cut points are unusable: {exc}") from exc

    return ScriptGuidance(
        hook=hook or None,
        title=title or None,
        cut_after=cuts,
        notes=parsed.notes.strip(),
    )
