"""Operator session: the signed `hub_operator` cookie and its CSRF token (api.md §1, security §4).

The cookie holds the operator id and the operator's `session_version`, signed with
`SESSION_SECRET` and a timestamp (itsdangerous). It expires 8 hours after login. The request
dependency compares the version with the stored one: logout and a password change increase the
stored version, so every cookie issued before (a copied one too) stops working. Logout therefore
ends all sessions of that operator.

The CSRF token is an HMAC of the cookie value with a key derived from `SESSION_SECRET`. A page on
another site can make the browser send the cookie, but cannot read the token, which the API
returns only in the bodies of `POST /auth/login` and `GET /auth/me`. The token is tied to one
cookie: a new login gives a new token.
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from itsdangerous import BadData, URLSafeTimedSerializer

COOKIE_NAME = "hub_operator"
CSRF_HEADER = "X-CSRF-Token"
MAX_AGE_SECONDS = 8 * 60 * 60
# Separates this signature from the customer session, which uses the same secret.
_SALT = "hub-operator-session"
_CSRF_KEY_LABEL = b"hub-operator-csrf"


@dataclass(frozen=True)
class OperatorSession:
    operator_id: uuid.UUID
    session_version: int


class OperatorSessionSigner:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("SESSION_SECRET is empty")
        self._serializer = URLSafeTimedSerializer(secret, salt=_SALT)
        self._csrf_key = hmac.new(secret.encode(), _CSRF_KEY_LABEL, hashlib.sha256).digest()

    def sign(self, operator_id: uuid.UUID, session_version: int) -> str:
        return self._serializer.dumps([str(operator_id), session_version])

    def verify(self, value: str) -> OperatorSession | None:
        """The session of a valid, unexpired cookie; `None` for anything else."""
        try:
            payload = self._serializer.loads(value, max_age=MAX_AGE_SECONDS)
        except BadData:
            return None
        if not isinstance(payload, list) or len(payload) != 2:
            return None
        operator_id, version = payload
        # bool is an int subclass; a version is never a bool.
        if not isinstance(operator_id, str) or type(version) is not int:
            return None
        try:
            return OperatorSession(operator_id=uuid.UUID(operator_id), session_version=version)
        except ValueError:
            return None

    def csrf_token(self, cookie_value: str) -> str:
        return hmac.new(self._csrf_key, cookie_value.encode(), hashlib.sha256).hexdigest()

    def csrf_matches(self, cookie_value: str, token: str | None) -> bool:
        """Constant-time comparison of the header value with the token of this cookie."""
        if not token:
            return False
        return hmac.compare_digest(self.csrf_token(cookie_value).encode(), token.encode())
