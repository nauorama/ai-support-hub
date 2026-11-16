"""Ticket status transitions (docs/data-model.md §1, state diagram). Anything else is an error.

The table holds pairs (from, to), not events: the same event can lead to different statuses
(a customer message on an `answered` ticket goes to `processing` while the AI owns the ticket and
to `handed_off` once a human owns it). Who owns the ticket is decided by the caller.
"""

from hub.domain.enums import TicketStatus

S = TicketStatus

INITIAL_STATUS = S.PROCESSING
"""A ticket starts with the customer's first message, so it is always created in `processing`."""

ALLOWED_TRANSITIONS: frozenset[tuple[TicketStatus, TicketStatus]] = frozenset(
    {
        (S.PROCESSING, S.ANSWERED),  # auto-reply
        (S.PROCESSING, S.PENDING_APPROVAL),  # draft waits
        (S.PROCESSING, S.HANDED_OFF),  # handoff / error
        (S.PENDING_APPROVAL, S.ANSWERED),  # approve / edit / reject with a reply
        (S.PENDING_APPROVAL, S.HANDED_OFF),  # reject without reply
        (S.HANDED_OFF, S.ANSWERED),  # operator reply
        (S.ANSWERED, S.PROCESSING),  # customer message (AI owns)
        (S.ANSWERED, S.HANDED_OFF),  # customer message (human owns) / asks for human
        (S.ANSWERED, S.CLOSED),  # operator closes / 72 h without reply
        (S.HANDED_OFF, S.CLOSED),  # operator closes
    }
)


class InvalidTransitionError(ValueError):
    """A status change that the state diagram does not allow."""

    def __init__(self, current: TicketStatus, target: TicketStatus) -> None:
        super().__init__(f"ticket status cannot change from {current} to {target}")
        self.current = current
        self.target = target


def can_transition(current: TicketStatus, target: TicketStatus) -> bool:
    return (current, target) in ALLOWED_TRANSITIONS


def check_transition(current: TicketStatus, target: TicketStatus) -> TicketStatus:
    """Return `target` when the change is allowed; raise `InvalidTransitionError` otherwise."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
    return target
