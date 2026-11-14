"""The signed operator cookie and its CSRF token (api.md §1, security §4)."""

import uuid

import pytest
from itsdangerous import URLSafeTimedSerializer

from hub.auth import operator_session
from hub.auth.customer_session import CustomerSessionSigner
from hub.auth.operator_session import MAX_AGE_SECONDS, OperatorSession, OperatorSessionSigner

SECRET = "unit-test-session-secret"  # noqa: S105 - test value


def test_sign_and_verify_round_trip() -> None:
    signer = OperatorSessionSigner(SECRET)
    operator_id = uuid.uuid4()
    assert signer.verify(signer.sign(operator_id, 3)) == OperatorSession(operator_id, 3)


def test_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        OperatorSessionSigner("")


def test_tampered_or_foreign_cookie_is_refused() -> None:
    signer = OperatorSessionSigner(SECRET)
    value = signer.sign(uuid.uuid4(), 1)
    tampered = value[:-2] + ("AA" if not value.endswith("AA") else "BB")
    assert signer.verify(tampered) is None
    assert signer.verify("garbage") is None
    assert signer.verify(OperatorSessionSigner("another-secret").sign(uuid.uuid4(), 1)) is None


def test_customer_cookie_is_not_an_operator_session() -> None:
    # Same secret, other salt: a demo customer cannot use their cookie on the console.
    customer_cookie = CustomerSessionSigner(SECRET).sign(uuid.uuid4())
    assert OperatorSessionSigner(SECRET).verify(customer_cookie) is None


def test_expired_cookie_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = OperatorSessionSigner(SECRET)
    value = signer.sign(uuid.uuid4(), 1)
    monkeypatch.setattr(operator_session, "MAX_AGE_SECONDS", -1)
    assert signer.verify(value) is None
    assert MAX_AGE_SECONDS == 8 * 60 * 60


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-list",
        ["only-one"],
        [str(uuid.uuid4()), "1"],
        [str(uuid.uuid4()), True],
        [str(uuid.uuid4()), 1.0],
        [123, 1],
        ["not-a-uuid", 1],
        [str(uuid.uuid4()), 1, "extra"],
    ],
)
def test_signed_payload_of_the_wrong_shape_is_refused(payload: object) -> None:
    # Signed with the right key and salt, so only the shape check can refuse it.
    value = URLSafeTimedSerializer(SECRET, salt="hub-operator-session").dumps(payload)
    assert OperatorSessionSigner(SECRET).verify(value) is None


def test_csrf_token_belongs_to_one_cookie() -> None:
    signer = OperatorSessionSigner(SECRET)
    cookie = signer.sign(uuid.uuid4(), 1)
    other_cookie = signer.sign(uuid.uuid4(), 1)
    token = signer.csrf_token(cookie)
    assert signer.csrf_matches(cookie, token)
    assert not signer.csrf_matches(other_cookie, token)
    assert not signer.csrf_matches(cookie, None)
    assert not signer.csrf_matches(cookie, "")
    assert not signer.csrf_matches(cookie, token[:-1] + ("0" if token[-1] != "0" else "1"))
    # Another secret gives another token for the same cookie.
    assert OperatorSessionSigner("another-secret").csrf_token(cookie) != token


def test_csrf_token_is_not_the_cookie_signature() -> None:
    signer = OperatorSessionSigner(SECRET)
    cookie = signer.sign(uuid.uuid4(), 1)
    assert signer.csrf_token(cookie) not in cookie
