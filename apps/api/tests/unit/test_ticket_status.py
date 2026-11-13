"""Ticket status transitions: every pair (from, to) against the state diagram of
docs/data-model.md §1. The expected table is written out here, not derived from the code."""

import itertools

import pytest

from hub.domain.enums import TicketStatus
from hub.domain.ticket_status import (
    ALLOWED_TRANSITIONS,
    INITIAL_STATUS,
    InvalidTransitionError,
    can_transition,
    check_transition,
)

S = TicketStatus

# One line per arrow of the diagram.
DIAGRAM = {
    ("processing", "answered"),  # auto-reply
    ("processing", "pending_approval"),  # draft waits
    ("processing", "handed_off"),  # handoff / error
    ("pending_approval", "answered"),  # approve / edit
    ("pending_approval", "handed_off"),  # reject without reply
    ("handed_off", "answered"),  # operator reply
    ("answered", "processing"),  # customer message (AI owns)
    ("answered", "handed_off"),  # customer message (human owns) / asks for human
    ("answered", "closed"),  # operator closes / 72 h without reply
    ("handed_off", "closed"),  # operator closes
}

ALL_PAIRS = list(itertools.product(S, S))
ALLOWED = [(a, b) for a, b in ALL_PAIRS if (a.value, b.value) in DIAGRAM]
FORBIDDEN = [(a, b) for a, b in ALL_PAIRS if (a.value, b.value) not in DIAGRAM]


def test_the_pairs_cover_every_status_combination() -> None:
    assert len(ALL_PAIRS) == 25
    assert len(ALLOWED) == len(DIAGRAM) == 10
    assert len(FORBIDDEN) == 15


def test_the_code_table_is_the_diagram() -> None:
    assert {(a.value, b.value) for a, b in ALLOWED_TRANSITIONS} == DIAGRAM


@pytest.mark.parametrize(("current", "target"), ALLOWED, ids=lambda s: str(s))
def test_allowed_transition(current: TicketStatus, target: TicketStatus) -> None:
    assert can_transition(current, target)
    assert check_transition(current, target) is target


@pytest.mark.parametrize(("current", "target"), FORBIDDEN, ids=lambda s: str(s))
def test_forbidden_transition(current: TicketStatus, target: TicketStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError) as caught:
        check_transition(current, target)
    assert caught.value.current is current
    assert caught.value.target is target
    assert str(caught.value) == f"ticket status cannot change from {current} to {target}"


def test_a_ticket_starts_in_processing() -> None:
    assert INITIAL_STATUS is S.PROCESSING


@pytest.mark.parametrize("target", list(S))
def test_closed_is_terminal(target: TicketStatus) -> None:
    assert not can_transition(S.CLOSED, target)


@pytest.mark.parametrize("status", list(S))
def test_no_status_moves_to_itself(status: TicketStatus) -> None:
    assert not can_transition(status, status)


def test_nothing_returns_to_pending_approval_except_from_processing() -> None:
    sources = {a for a, b in ALLOWED_TRANSITIONS if b is S.PENDING_APPROVAL}
    assert sources == {S.PROCESSING}


def test_invalid_transition_is_a_value_error() -> None:
    # Callers that map ValueError to a 409/422 problem response catch it without a new type.
    assert issubclass(InvalidTransitionError, ValueError)
