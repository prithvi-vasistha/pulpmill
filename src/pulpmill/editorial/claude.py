"""Optional Claude editorial provider.

Strictly optional. The `anthropic` package is imported lazily and only when this
provider is actually selected, so the pipeline installs and runs without it and
without an API key. Every failure path -- missing package, missing key, timeout,
API error, refusal, truncation, malformed JSON, invalid story id -- raises an
`EditorialError` that the selection service turns into a deterministic fallback.

The model is asked for structured JSON output, so a well-formed document is
guaranteed by the API. That is not the same as a *correct* one: the response is
still validated against the candidate set before anything acts on it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pulpmill.config.models import ClaudeEditorialConfig
from pulpmill.domain.errors import (
    EditorialError,
    EditorialProviderUnavailableError,
    EditorialResponseError,
)
from pulpmill.editorial.provider import (
    EditorialCandidate,
    EditorialDecision,
    SelectedStory,
)
from pulpmill.editorial.schema import (
    SELECTION_JSON_SCHEMA,
    parse_json_payload,
    validate_selection,
)
from pulpmill.infrastructure.logging import get_logger

PROVIDER_NAME = "claude"

_SYSTEM_PROMPT = """You are an editor for a short-form vertical video channel that \
narrates real stories from public internet sources.

A local ranking engine has already filtered thousands of stories down to the small \
candidate set below. Your job is editorial selection and sequencing, not scoring: \
decide which of these to publish and in what order.

Weigh:
- hook strength: does the opening make someone stop scrolling?
- story quality: is there a real arc, or just a situation?
- emotional intensity and payoff
- topic diversity across the batch -- avoid two near-identical premises back to back
- source diversity where the quality is comparable
- estimated narration duration; prefer a varied mix
- similarity to recently published titles, which you should steer away from

Order for a publication run: open strong, vary the emotional register, and do not \
cluster the same category consecutively.

Return only story ids that appear in the candidate list."""


class ClaudeProvider:
    """Editorial selection via the Claude API."""

    def __init__(self, config: ClaudeEditorialConfig, *, api_key: str | None) -> None:
        self._config = config
        self._api_key = api_key
        self._log = get_logger("editorial.claude")

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "ANTHROPIC_API_KEY is not set"
        try:
            import anthropic  # noqa: F401  (probe only)
        except ImportError:
            return False, "the 'anthropic' package is not installed (uv sync --extra claude)"
        return True, f"ready ({self._config.model})"

    def select(
        self,
        candidates: Sequence[EditorialCandidate],
        *,
        count: int,
        recently_used_titles: Sequence[str] = (),
    ) -> EditorialDecision:
        ok, detail = self.available()
        if not ok:
            raise EditorialProviderUnavailableError(detail, provider=PROVIDER_NAME)
        if not candidates:
            raise EditorialError("no candidates to select from", provider=PROVIDER_NAME)

        wanted = min(count, len(candidates))
        payload = self._request(candidates, wanted, recently_used_titles)
        response = validate_selection(
            payload,
            allowed_ids=[candidate.story_id for candidate in candidates],
            expected_count=wanted,
        )

        ordered = sorted(response.selections, key=lambda item: item.position)
        return EditorialDecision(
            provider=PROVIDER_NAME,
            selections=tuple(
                SelectedStory(
                    story_id=item.story_id,
                    position=item.position,
                    rationale=item.rationale,
                    metadata={
                        "hook_strength": item.hook_strength,
                        "category": item.category,
                        "model": self._config.model,
                    },
                )
                for item in ordered
            ),
            notes=response.notes,
        )

    # --- API plumbing --------------------------------------------------------

    def _request(
        self,
        candidates: Sequence[EditorialCandidate],
        count: int,
        recently_used_titles: Sequence[str],
    ) -> Any:
        import anthropic

        client = anthropic.Anthropic(
            api_key=self._api_key,
            timeout=self._config.timeout_seconds,
            max_retries=self._config.max_attempts - 1,
        )

        prompt = _build_prompt(candidates, count, recently_used_titles)

        try:
            message = client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_output_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": SELECTION_JSON_SCHEMA}},
            )
        except anthropic.APIStatusError as exc:
            raise EditorialError(
                "claude API returned an error status",
                provider=PROVIDER_NAME,
                status_code=exc.status_code,
            ) from exc
        except anthropic.APIConnectionError as exc:
            # Covers timeouts: APITimeoutError subclasses APIConnectionError.
            raise EditorialError(
                "could not reach the claude API",
                provider=PROVIDER_NAME,
                detail=type(exc).__name__,
            ) from exc

        if message.stop_reason == "refusal":
            raise EditorialError("claude declined to answer this request", provider=PROVIDER_NAME)
        if message.stop_reason == "max_tokens":
            raise EditorialResponseError(
                "claude response was truncated; raise editorial.claude.max_output_tokens",
                provider=PROVIDER_NAME,
            )

        # Written as an explicit loop so the block union narrows on its `type`
        # discriminator: a response can also carry thinking and tool blocks.
        parts: list[str] = []
        for block in message.content:
            if block.type == "text":
                parts.append(block.text)
        text = "".join(parts)
        if not text.strip():
            raise EditorialResponseError(
                "claude returned no text content",
                provider=PROVIDER_NAME,
                stop_reason=str(message.stop_reason),
            )

        self._log.info(
            "claude_editorial_response",
            model=self._config.model,
            input_tokens=getattr(message.usage, "input_tokens", None),
            output_tokens=getattr(message.usage, "output_tokens", None),
            candidates=len(candidates),
        )
        return parse_json_payload(text)


def _build_prompt(
    candidates: Sequence[EditorialCandidate],
    count: int,
    recently_used_titles: Sequence[str],
) -> str:
    sections = [
        f"Select and order exactly {count} of the {len(candidates)} candidates below.",
        "",
        "CANDIDATES:",
        json.dumps(
            [candidate.to_prompt_dict() for candidate in candidates],
            indent=2,
            ensure_ascii=False,
        ),
    ]
    if recently_used_titles:
        sections += [
            "",
            "RECENTLY PUBLISHED (avoid repeating these premises):",
            json.dumps(list(recently_used_titles[:20]), indent=2, ensure_ascii=False),
        ]
    sections += [
        "",
        f"Return exactly {count} selections, with positions 1 through {count}.",
    ]
    return "\n".join(sections)
