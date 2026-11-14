"""Demo customer session: the signed `hub_customer` cookie (docs/api.md §1, security §4).

DEMO ONLY. It stands in for the login of the Tallybridge app: after the demo access code, the
customer picker creates a session for a seed customer. A real deployment gets the customer
identity from the company's own login instead.

The cookie holds only the internal customer UUID, signed with `SESSION_SECRET` and a timestamp
(itsdangerous). The identity of every customer request comes from here - never from the body
and never from a model (AGENTS.md, never-do 3).
"""

import hmac
import uuid

from itsdangerous import BadData, URLSafeTimedSerializer

COOKIE_NAME = "hub_customer"
# The same lifetime as an operator session (security §4).
MAX_AGE_SECONDS = 8 * 60 * 60
# Separates this signature from any other use of SESSION_SECRET (the operator session, P10).
_SALT = "hub-customer-session"


class CustomerSessionSigner:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("SESSION_SECRET is empty")
        self._serializer = URLSafeTimedSerializer(secret, salt=_SALT)

    def sign(self, customer_id: uuid.UUID) -> str:
        return self._serializer.dumps(str(customer_id))

    def verify(self, value: str) -> uuid.UUID | None:
        """The customer id of a valid, unexpired cookie; `None` for anything else."""
        try:
            payload = self._serializer.loads(value, max_age=MAX_AGE_SECONDS)
        except BadData:
            return None
        if not isinstance(payload, str):
            return None
        try:
            return uuid.UUID(payload)
        except ValueError:
            return None


def access_code_matches(given: str, expected: str) -> bool:
    """Constant-time comparison. An empty expected code matches nothing (demo switched off)."""
    if not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())
