"""Optional Claude script provider.

Strictly optional, and strictly advisory. The `anthropic` package is imported
lazily and only when this provider is selected, so the pipeline installs and
runs without it and without an API key.

What the model is allowed to influence: the hook, the on-screen title, and where
a long story is cut. What it cannot touch: how many parts there are, what they
are numbered, and which text belongs to which part. Those are computed from its
suggested cut points by the pipeline, and its suggestions are discarded whole if
any of them is out of range.

Every failure path -- missing package, missing key, timeout, API error, refusal,
truncation, malformed JSON, unusable cut points -- raises a `ScriptError` that
the service turns into the deterministic plan.
"""

from __future__ import annotations

import json
from typing import Any

from pulpmill.config.models import ClaudeScriptConfig
from pulpmill.domain.errors import (
    ScriptError,
    ScriptProviderUnavailableError,
    ScriptResponseError,
)
from pulpmill.infrastructure.logging import get_logger
from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance
from pulpmill.scripting.schema import (
    SCRIPT_JSON_SCHEMA,
    parse_json_payload,
    validate_guidance,
)

PROVIDER_NAME = "claude"

_SYSTEM_PROMPT = """You prepare real stories from public internet sources for \
narration as short-form vertical videos.

You are given a story already split into numbered sentences. Return three things:

1. hook -- the opening line of narration. It must be faithful to the story: do not \
invent events, names, or outcomes that are not in the sentences you were given. \
Most of these titles were written to be clicked and already work as hooks; return \
an empty string when you cannot clearly beat the original.

2. title -- a short on-screen title. Empty string keeps the original.

3. cut_after -- if the story is too long for one video, the sentence indices after \
which each new part should begin. Cut where tension is highest, not where the text \
happens to be halfway. Prefer cutting at a paragraph boundary. Return an empty \
array to accept an even split.

You are advising on pacing. You do not decide how many parts exist or what they \
are numbered; that is computed from your cut points and validated before use. \
Indices outside the range you were given cause your entire response to be \
discarded."""


class ClaudeScriptProvider:
    """Hook and pacing advice via the Claude API."""

    def __init__(self, config: ClaudeScriptConfig, *, api_key: str | None) -> None:
        self._config = config
        self._api_key = api_key
        self._log = get_logger("scripting.claude")

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

    def guide(self, brief: ScriptBrief) -> ScriptGuidance:
        ok, detail = self.available()
        if not ok:
            raise ScriptProviderUnavailableError(detail, provider=PROVIDER_NAME)
        if not brief.sentences:
            raise ScriptError("story has no sentences to script", story_id=brief.story_id)

        payload = self._request(brief)
        guidance = validate_guidance(payload, brief=brief)
        return ScriptGuidance(
            hook=guidance.hook,
            title=guidance.title,
            cut_after=guidance.cut_after,
            notes=guidance.notes,
            metadata={"model": self._config.model},
        )

    # --- API plumbing --------------------------------------------------------

    def _request(self, brief: ScriptBrief) -> Any:
        import anthropic

        client = anthropic.Anthropic(
            api_key=self._api_key,
            timeout=self._config.timeout_seconds,
            max_retries=self._config.max_attempts - 1,
        )

        try:
            message = client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_output_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_prompt(brief)}],
                output_config={"format": {"type": "json_schema", "schema": SCRIPT_JSON_SCHEMA}},
            )
        except anthropic.APIStatusError as exc:
            raise ScriptError(
                "claude API returned an error status",
                provider=PROVIDER_NAME,
                status_code=exc.status_code,
            ) from exc
        except anthropic.APIConnectionError as exc:
            # Covers timeouts: APITimeoutError subclasses APIConnectionError.
            raise ScriptError(
                "could not reach the claude API",
                provider=PROVIDER_NAME,
                detail=type(exc).__name__,
            ) from exc

        if message.stop_reason == "refusal":
            raise ScriptError("claude declined to script this story", provider=PROVIDER_NAME)
        if message.stop_reason == "max_tokens":
            raise ScriptResponseError(
                "claude response was truncated; raise script.claude.max_output_tokens",
                provider=PROVIDER_NAME,
            )

        # Explicit loop so the block union narrows on its `type` discriminator:
        # a response can also carry thinking and tool blocks.
        parts: list[str] = []
        for block in message.content:
            if block.type == "text":
                parts.append(block.text)
        text = "".join(parts)
        if not text.strip():
            raise ScriptResponseError(
                "claude returned no text content",
                provider=PROVIDER_NAME,
                stop_reason=str(message.stop_reason),
            )

        self._log.info(
            "claude_script_response",
            model=self._config.model,
            story_id=brief.story_id,
            input_tokens=getattr(message.usage, "input_tokens", None),
            output_tokens=getattr(message.usage, "output_tokens", None),
            sentences=len(brief.sentences),
        )
        return parse_json_payload(text)


def _build_prompt(brief: ScriptBrief) -> str:
    estimated_parts = max(1, round(brief.estimated_seconds / brief.target_seconds))
    return "\n".join(
        [
            f"Community: {brief.community}",
            f"Estimated narration: {round(brief.estimated_seconds)} seconds "
            f"across roughly {estimated_parts} part(s) at "
            f"{round(brief.target_seconds)}s each, {brief.max_seconds:.0f}s hard maximum.",
            "",
            "STORY:",
            json.dumps(brief.to_prompt_dict(), indent=2, ensure_ascii=False),
            "",
            f"Valid cut_after indices are 0 to {len(brief.sentences) - 2}. "
            f"At most {brief.max_parts} parts.",
        ]
    )
