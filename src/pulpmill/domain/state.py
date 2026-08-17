"""The story state machine.

Transitions are declared in one table rather than scattered through the stages,
so "can a story go from X to Y" has exactly one answer and adding a stage means
adding edges here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pulpmill.domain.enums import StoryStatus
from pulpmill.domain.errors import InvalidStateTransitionError

S = StoryStatus

#: Allowed forward edges. FAILED is reachable from any non-terminal state and is
#: handled separately rather than being listed on every row.
_TRANSITIONS: Mapping[StoryStatus, frozenset[StoryStatus]] = MappingProxyType(
    {
        S.DISCOVERED: frozenset({S.NORMALIZED, S.REJECTED}),
        S.NORMALIZED: frozenset({S.DEDUPLICATED, S.DUPLICATE, S.REJECTED}),
        S.DEDUPLICATED: frozenset({S.RANKED, S.DUPLICATE, S.REJECTED}),
        # Re-ranking a ranked story is legal and idempotent.
        S.RANKED: frozenset({S.RANKED, S.SELECTED, S.DUPLICATE, S.REJECTED}),
        S.SELECTED: frozenset({S.SCRIPT_PENDING, S.RANKED, S.REJECTED}),
        S.SCRIPT_PENDING: frozenset({S.SCRIPT_READY, S.REJECTED}),
        S.SCRIPT_READY: frozenset({S.AUDIO_PENDING, S.SCRIPT_PENDING}),
        S.AUDIO_PENDING: frozenset({S.AUDIO_READY}),
        S.AUDIO_READY: frozenset({S.VIDEO_PENDING, S.AUDIO_PENDING}),
        S.VIDEO_PENDING: frozenset({S.VIDEO_READY}),
        S.VIDEO_READY: frozenset({S.VALIDATED, S.VIDEO_PENDING}),
        S.VALIDATED: frozenset({S.PUBLISHED, S.VIDEO_PENDING}),
        S.PUBLISHED: frozenset(),
        # Recovery edges: a failed or set-aside story can be retried from the
        # start of the stage that owns it.
        S.FAILED: frozenset({S.DISCOVERED, S.NORMALIZED, S.DEDUPLICATED, S.RANKED}),
        S.DUPLICATE: frozenset({S.DEDUPLICATED}),
        S.REJECTED: frozenset({S.DEDUPLICATED}),
    }
)

#: States from which a story can no longer be worked on without operator action.
TERMINAL_STATUSES: frozenset[StoryStatus] = frozenset({S.PUBLISHED})

#: States that mean "this story is out of the running for selection".
EXCLUDED_FROM_RANKING: frozenset[StoryStatus] = frozenset({S.DUPLICATE, S.REJECTED, S.FAILED})


def allowed_transitions(status: StoryStatus) -> frozenset[StoryStatus]:
    """Every status reachable from `status` in one step."""
    allowed = _TRANSITIONS.get(status, frozenset())
    if status in TERMINAL_STATUSES:
        return allowed
    return allowed | {S.FAILED}


def can_transition(from_status: StoryStatus, to_status: StoryStatus) -> bool:
    """Whether the edge `from_status -> to_status` exists.

    A self-transition is permitted only where it is explicitly declared (RANKED),
    which is what makes re-running a stage idempotent instead of an error.
    """
    return to_status in allowed_transitions(from_status)


def ensure_transition(story_id: str, from_status: StoryStatus, to_status: StoryStatus) -> None:
    """Raise `InvalidStateTransitionError` unless the edge exists."""
    if not can_transition(from_status, to_status):
        raise InvalidStateTransitionError(story_id, from_status.value, to_status.value)
