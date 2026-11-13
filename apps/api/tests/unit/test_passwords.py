"""Argon2id password hashing (`hub.auth.passwords`)."""

import pytest

from hub.auth.passwords import hash_password, needs_rehash, verify_password


def test_a_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert "correct horse" not in hashed
    assert verify_password(hashed, "correct horse battery staple")
    assert not needs_rehash(hashed)


def test_a_wrong_password_does_not_verify() -> None:
    assert not verify_password(hash_password("one password"), "another password")


def test_a_broken_hash_does_not_verify() -> None:
    assert not verify_password("not-a-hash", "anything")


def test_two_hashes_of_one_password_differ() -> None:
    # A random salt per hash: equal passwords are not visible in the table.
    assert hash_password("same") != hash_password("same")


def test_an_empty_password_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        hash_password("")
