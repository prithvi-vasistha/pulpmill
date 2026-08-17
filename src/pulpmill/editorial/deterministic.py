"""Deterministic editorial provider.

Takes the top `count` candidates in ranking order. That is the whole algorithm,
and the simplicity is the point: this is the floor the system falls back to, so
it must never fail, never need a network, and never surprise anyone.

It is also the baseline any smarter provider has to beat.
"""

from __future__ import annotations

from collections.abc import Sequence

from pulpmill.editorial.provider import (
    EditorialCandidate,
    EditorialDecision,
    SelectedStory,
)

PROVIDER_NAME = "deterministic"


class DeterministicProvider:
    """Ranking order, unchanged."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def available(self) -> tuple[bool, str]:
        return True, "always available (no network, no credentials, no model)"

    def select(
        self,
        candidates: Sequence[EditorialCandidate],
        *,
        count: int,
        # Part of the provider contract, and deliberately ignored: taking recent
        # history into account would make this provider non-deterministic, which
        # is exactly what it exists to avoid.
        recently_used_titles: Sequence[str] = (),  # noqa: ARG002
    ) -> EditorialDecision:
        # Candidates arrive pre-sorted by score with a deterministic tiebreak, so
        # re-sorting here would be redundant -- but sorting explicitly makes the
        # ordering guarantee local and testable.
        ordered = sorted(candidates, key=lambda c: (-c.final_score, c.story_id))
        chosen = ordered[: max(0, count)]
        return EditorialDecision(
            provider=PROVIDER_NAME,
            selections=tuple(
                SelectedStory(
                    story_id=candidate.story_id,
                    position=index,
                    rationale=f"rank #{index} by local score {candidate.final_score:.2f}",
                    metadata={"final_score": candidate.final_score},
                )
                for index, candidate in enumerate(chosen, start=1)
            ),
            notes="deterministic ranking order",
        )
