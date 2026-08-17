"""The editorial selection stage.

Pulls the top candidates from the ranking table, hands that small set to the
configured provider, and persists the ordering. If the provider fails for *any*
reason, the deterministic ordering is used instead and the reason is recorded on
the batch -- a degraded run is visible in the data, not just in a log line that
will rotate away.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.domain.errors import EditorialError, InvalidStateTransitionError
from pulpmill.editorial.claude import ClaudeProvider
from pulpmill.editorial.deterministic import DeterministicProvider
from pulpmill.editorial.provider import (
    EditorialCandidate,
    EditorialDecision,
    EditorialProvider,
)
from pulpmill.infrastructure.logging import get_logger
from pulpmill.persistence.repositories.editorial import EditorialRepository, SelectionEntry
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import StoryRepository

_log = get_logger("editorial.service")


@dataclass(frozen=True, slots=True)
class SelectionResult:
    batch_id: str
    provider: str
    effective_provider: str
    fallback_reason: str | None
    decision: EditorialDecision
    candidate_count: int

    @property
    def used_fallback(self) -> bool:
        return self.provider != self.effective_provider


def build_provider(
    config: AppConfig, secrets: SecretStore, *, name: str | None = None
) -> EditorialProvider:
    """Instantiate the configured provider. Never raises for a missing key."""
    provider_name = name or config.editorial.provider
    if provider_name == "claude":
        return ClaudeProvider(
            config.editorial.claude,
            # Read under the SDK's conventional name rather than a pulpmill one.
            api_key=secrets.get("ANTHROPIC_API_KEY", prefixed=False),
        )
    return DeterministicProvider()


class EditorialSelector:
    """Runs the selection stage and persists its outcome."""

    def __init__(
        self,
        *,
        config: AppConfig,
        stories: StoryRepository,
        rankings: RankingRepository,
        editorial: EditorialRepository,
        provider: EditorialProvider,
    ) -> None:
        self._config = config
        self._stories = stories
        self._rankings = rankings
        self._editorial = editorial
        self._provider = provider
        self._fallback = DeterministicProvider()

    def select(
        self,
        *,
        ranking_version: str,
        config_fingerprint: str,
        pool_size: int | None = None,
        count: int | None = None,
        job_id: str | None = None,
    ) -> SelectionResult | None:
        """Choose the next batch of stories to publish.

        Returns None when there is nothing ranked to choose from.
        """
        editorial_config = self._config.editorial
        pool = pool_size or editorial_config.candidate_pool_size
        wanted = count or editorial_config.select_count

        ranked = self._rankings.top_candidates(
            ranking_version=ranking_version,
            config_fingerprint=config_fingerprint,
            limit=pool,
        )
        if not ranked:
            _log.warning("editorial_no_candidates", ranking_version=ranking_version)
            return None

        candidates = [EditorialCandidate.from_ranked(entry) for entry in ranked]
        wanted = min(wanted, len(candidates))

        recent_titles: Sequence[str] = self._editorial.recently_selected_titles(
            hours=editorial_config.claude.recent_selection_hours
        )

        requested_provider = self._provider.name
        fallback_reason: str | None = None

        available, detail = self._provider.available()
        if available:
            try:
                decision = self._provider.select(
                    candidates, count=wanted, recently_used_titles=recent_titles
                )
            except EditorialError as exc:
                fallback_reason = str(exc)
                _log.warning(
                    "editorial_provider_failed",
                    provider=requested_provider,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    falling_back_to=self._fallback.name,
                )
                decision = self._fallback.select(
                    candidates, count=wanted, recently_used_titles=recent_titles
                )
        else:
            fallback_reason = detail
            if requested_provider != self._fallback.name:
                _log.warning(
                    "editorial_provider_unavailable",
                    provider=requested_provider,
                    reason=detail,
                    falling_back_to=self._fallback.name,
                )
            decision = self._fallback.select(
                candidates, count=wanted, recently_used_titles=recent_titles
            )

        batch_id = self._editorial.save_batch(
            provider=requested_provider,
            effective_provider=decision.provider,
            fallback_reason=fallback_reason if decision.provider != requested_provider else None,
            ranking_version=ranking_version,
            config_fingerprint=config_fingerprint,
            candidate_count=len(candidates),
            entries=[
                SelectionEntry(
                    story_id=item.story_id,
                    position=item.position,
                    rationale=item.rationale,
                    metadata=item.metadata,
                )
                for item in decision.selections
            ],
        )

        for item in decision.selections:
            try:
                self._stories.transition(
                    item.story_id,
                    StoryStatus.SELECTED,
                    stage=PipelineStage.SELECT,
                    job_id=job_id,
                    reason=f"selected at position {item.position} by {decision.provider}",
                )
            except InvalidStateTransitionError as exc:
                # Already selected in an earlier batch, or moved on. Recorded in
                # the batch either way; not a failure of the run.
                _log.info(
                    "editorial_transition_skipped",
                    story_id=item.story_id,
                    reason=str(exc),
                )

        _log.info(
            "editorial_selection_complete",
            batch_id=batch_id,
            provider=requested_provider,
            effective_provider=decision.provider,
            selected=len(decision.selections),
            candidates=len(candidates),
            used_fallback=decision.provider != requested_provider,
        )

        return SelectionResult(
            batch_id=batch_id,
            provider=requested_provider,
            effective_provider=decision.provider,
            fallback_reason=fallback_reason if decision.provider != requested_provider else None,
            decision=decision,
            candidate_count=len(candidates),
        )
