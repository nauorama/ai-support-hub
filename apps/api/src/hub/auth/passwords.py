"""Operator password hashing with Argon2id (docs/security.md §4).

argon2-cffi 25.1: `PasswordHasher()` uses Argon2id with the RFC 9106 low-memory profile
(64 MiB, t=3, p=4). Used by `make seed` now and by operator login (P2-07).
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("an empty password cannot be hashed")
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True when the password matches. A wrong password or a broken hash gives False."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with other parameters than the current ones."""
    return _HASHER.check_needs_rehash(password_hash)
