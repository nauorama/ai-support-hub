"""The signed demo customer cookie and the access-code comparison."""

import uuid

import pytest
from itsdangerous import URLSafeTimedSerializer

from hub.auth import customer_session
from hub.auth.customer_session import (
    MAX_AGE_SECONDS,
    CustomerSessionSigner,
    access_code_matches,
)

SECRET = "unit-test-session-secret"  # noqa: S105 - test value


def test_sign_and_verify_round_trip() -> None:
    signer = CustomerSessionSigner(SECRET)
    customer_id = uuid.uuid4()
    assert signer.verify(signer.sign(customer_id)) == customer_id


def test_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        CustomerSessionSigner("")


def test_tampered_cookie_is_refused() -> None:
    signer = CustomerSessionSigner(SECRET)
    value = signer.sign(uuid.uuid4())
    tampered = value[:-2] + ("AA" if not value.endswith("AA") else "BB")
    assert signer.verify(tampered) is None
    assert signer.verify("garbage") is None
    assert signer.verify("") is None


def test_cookie_of_another_secret_is_refused() -> None:
    value = CustomerSessionSigner("another-secret").sign(uuid.uuid4())
    assert CustomerSessionSigner(SECRET).verify(value) is None


def test_expired_cookie_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = CustomerSessionSigner(SECRET)
    value = signer.sign(uuid.uuid4())
    # itsdangerous reads the clock through `get_timestamp`; move it past the max age.
    real = URLSafeTimedSerializer.default_signer.get_timestamp
    monkeypatch.setattr(
        URLSafeTimedSerializer.default_signer,
        "get_timestamp",
        lambda self: real(self) + MAX_AGE_SECONDS + 1,
    )
    assert signer.verify(value) is None


@pytest.mark.parametrize("payload", [12345, "not-a-uuid", ["x"]])
def test_signed_payload_that_is_not_a_uuid_is_refused(payload: object) -> None:
    serializer = URLSafeTimedSerializer(SECRET, salt=customer_session._SALT)
    assert CustomerSessionSigner(SECRET).verify(serializer.dumps(payload)) is None


def test_session_is_valid_for_eight_hours() -> None:
    assert MAX_AGE_SECONDS == 8 * 60 * 60


def test_access_code() -> None:
    assert access_code_matches("open-sesame", "open-sesame")
    assert not access_code_matches("open-sesam", "open-sesame")
    assert not access_code_matches("", "")
    assert not access_code_matches("anything", "")
    assert access_code_matches("ünïcode", "ünïcode")
