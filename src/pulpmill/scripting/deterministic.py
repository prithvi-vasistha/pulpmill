"""The always-available script provider.

Produces no advice beyond a tidied title, which is exactly right: the
deterministic path *is* the segmentation in `pulpmill.scripting.segmentation`,
and this provider exists so that path has a name, a version and a record on
every script it produced.

Never fails, never needs a key, never needs a network.
"""

from __future__ import annotations

from pulpmill.scripting.hooks import tidy_title
from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance

PROVIDER_NAME = "deterministic"


class DeterministicScriptProvider:
    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def available(self) -> tuple[bool, str]:
        return True, "always available (no model, no network)"

    def guide(self, brief: ScriptBrief) -> ScriptGuidance:
        return ScriptGuidance(
            title=tidy_title(brief.title) or None,
            notes="even duration split, snapped to paragraph breaks",
        )
