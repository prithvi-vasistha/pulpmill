"""The always-available script provider.

Produces no advice beyond a tidied title, which is exactly right: the
deterministic path *is* the segmentation in `pulpmill.scripting.segmentation`,
and this provider exists so that path has a name, a version and a record on
every script it produced.

Never fails, never needs a key, never needs a network.
"""

from __future__ import annotations

from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance

PROVIDER_NAME = "deterministic"


class DeterministicScriptProvider:
    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def available(self) -> tuple[bool, str]:
        return True, "always available (no model, no network)"

    def guide(self, brief: ScriptBrief) -> ScriptGuidance:  # noqa: ARG002
        """Offers no title of its own.

        Returning a tidied title here looked harmless and was not: `tidy_title`
        shortens to title-card width, and the service treats a provider's title
        as the source for *both* the card and the spoken hook. The hook then
        lost its last few words -- which, on a question-shaped title, are the
        entire point of the question.

        A provider's job is to propose an *alternative*. This one has none, so
        it says so and lets the service tidy the original for each use.
        """
        return ScriptGuidance(notes="even duration split, snapped to paragraph breaks")
