"""Operator account lockout after repeated wrong passwords (security §4). Pure functions.

The state lives in `operators.failed_logins` and `operators.locked_until` (migration 0004), so a
restart or a scale-to-zero does not reset it. A lock ends by itself after `LOCKOUT`: a permanent
lock would let anyone who knows an operator's email lock that operator out for good.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_FAILED_LOGINS = 10
LOCKOUT = timedelta(minutes=15)


@dataclass(frozen=True)
class LoginState:
    failed_logins: int
    locked_until: datetime | None


def is_locked(state: LoginState, now: datetime) -> bool:
    return state.locked_until is not None and state.locked_until > now


def after_failure(state: LoginState, now: datetime) -> LoginState:
    """The state after one more wrong password. The 10th failure locks the account and starts
    the count again, so the first failure after the lock ends does not lock at once."""
    failed = state.failed_logins + 1
    if failed >= MAX_FAILED_LOGINS:
        return LoginState(failed_logins=0, locked_until=now + LOCKOUT)
    return LoginState(failed_logins=failed, locked_until=state.locked_until)


def after_success() -> LoginState:
    return LoginState(failed_logins=0, locked_until=None)
