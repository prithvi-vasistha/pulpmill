"""The story state machine."""

from __future__ import annotations

import pytest

from pulpmill.domain.enums import StoryStatus
from pulpmill.domain.errors import InvalidStateTransitionError
from pulpmill.domain.state import (
    EXCLUDED_FROM_RANKING,
    TERMINAL_STATUSES,
    allowed_transitions,
    can_transition,
    ensure_transition,
)

S = StoryStatus


class TestTransitions:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (S.DISCOVERED, S.NORMALIZED),
            (S.NORMALIZED, S.DEDUPLICATED),
            (S.NORMALIZED, S.DUPLICATE),
            (S.DEDUPLICATED, S.RANKED),
            (S.RANKED, S.SELECTED),
            (S.SELECTED, S.SCRIPT_PENDING),
            (S.SCRIPT_READY, S.AUDIO_PENDING),
            (S.AUDIO_READY, S.VIDEO_PENDING),
            (S.VIDEO_READY, S.VALIDATED),
            (S.VALIDATED, S.PUBLISHED),
        ],
    )
    def test_the_happy_path_is_connected(self, source: S, target: S) -> None:
        assert can_transition(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (S.DISCOVERED, S.RANKED),
            (S.DISCOVERED, S.PUBLISHED),
            (S.NORMALIZED, S.SELECTED),
            (S.PUBLISHED, S.RANKED),
            (S.RANKED, S.PUBLISHED),
        ],
    )
    def test_stages_cannot_be_skipped(self, source: S, target: S) -> None:
        assert not can_transition(source, target)

    def test_re_ranking_is_allowed_and_idempotent(self) -> None:
        """Re-running `rank` must not be an error."""
        assert can_transition(S.RANKED, S.RANKED)

    def test_other_self_transitions_are_rejected(self) -> None:
        assert not can_transition(S.NORMALIZED, S.NORMALIZED)
        assert not can_transition(S.DEDUPLICATED, S.DEDUPLICATED)

    def test_failure_is_reachable_from_any_non_terminal_state(self) -> None:
        for status in StoryStatus:
            if status in TERMINAL_STATUSES or status is S.FAILED:
                continue
            assert can_transition(status, S.FAILED), status

    def test_published_is_terminal(self) -> None:
        assert allowed_transitions(S.PUBLISHED) == frozenset()

    def test_failed_stories_can_be_retried(self) -> None:
        assert can_transition(S.FAILED, S.DISCOVERED)
        assert can_transition(S.FAILED, S.RANKED)

    def test_a_duplicate_can_be_restored(self) -> None:
        """Needed when a dedup threshold change turns out to be wrong."""
        assert can_transition(S.DUPLICATE, S.DEDUPLICATED)

    def test_ensure_transition_raises_with_full_context(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc:
            ensure_transition("story-1", S.DISCOVERED, S.PUBLISHED)
        assert exc.value.from_status == "DISCOVERED"
        assert exc.value.to_status == "PUBLISHED"
        assert "story-1" in str(exc.value)

    def test_ensure_transition_passes_for_a_legal_edge(self) -> None:
        ensure_transition("story-1", S.DISCOVERED, S.NORMALIZED)

    def test_set_aside_states_are_excluded_from_ranking(self) -> None:
        assert frozenset({S.DUPLICATE, S.REJECTED, S.FAILED}) == EXCLUDED_FROM_RANKING

    def test_every_status_is_reachable_from_discovered(self) -> None:
        """No state is stranded: an unreachable state is a modelling bug."""
        reachable = {S.DISCOVERED}
        frontier = [S.DISCOVERED]
        while frontier:
            for target in allowed_transitions(frontier.pop()):
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        assert reachable == set(StoryStatus)
