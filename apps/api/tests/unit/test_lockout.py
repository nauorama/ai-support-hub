"""Account lockout after 10 wrong passwords (security §4)."""

from datetime import UTC, datetime, timedelta

from hub.auth.lockout import (
    LOCKOUT,
    MAX_FAILED_LOGINS,
    LoginState,
    after_failure,
    after_success,
    is_locked,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def test_the_tenth_failure_locks_for_the_lockout_time() -> None:
    assert MAX_FAILED_LOGINS == 10
    state = LoginState(0, None)
    for _ in range(9):
        state = after_failure(state, NOW)
        assert not is_locked(state, NOW)
    assert state.failed_logins == 9
    state = after_failure(state, NOW)
    assert state == LoginState(0, NOW + LOCKOUT)
    assert is_locked(state, NOW + LOCKOUT - timedelta(seconds=1))
    assert not is_locked(state, NOW + LOCKOUT)


def test_after_the_lock_ends_one_failure_does_not_lock_again() -> None:
    locked = LoginState(0, NOW)
    later = NOW + timedelta(seconds=1)
    state = after_failure(locked, later)
    assert state.failed_logins == 1
    assert not is_locked(state, later)


def test_success_clears_the_count_and_the_lock() -> None:
    assert after_success() == LoginState(0, None)
    assert not is_locked(LoginState(5, None), NOW)
