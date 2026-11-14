"""Role rules (api.md §1): operator < lead < admin; compliance reads only."""

import pytest

from hub.auth.roles import (
    ADMIN,
    AUDIT_READER,
    LEAD,
    OPERATOR,
    TICKET_READER,
    RoleRequirement,
    can_override_claim,
)
from hub.domain.enums import OperatorRole

R = OperatorRole
ALLOWED: dict[str, set[OperatorRole]] = {
    "OPERATOR": {R.OPERATOR, R.LEAD, R.ADMIN},
    "TICKET_READER": {R.OPERATOR, R.LEAD, R.ADMIN, R.COMPLIANCE},
    "LEAD": {R.LEAD, R.ADMIN},
    "ADMIN": {R.ADMIN},
    "AUDIT_READER": {R.COMPLIANCE, R.LEAD, R.ADMIN},
}
REQUIREMENTS: dict[str, RoleRequirement] = {
    "OPERATOR": OPERATOR,
    "TICKET_READER": TICKET_READER,
    "LEAD": LEAD,
    "ADMIN": ADMIN,
    "AUDIT_READER": AUDIT_READER,
}


@pytest.mark.parametrize("name", sorted(REQUIREMENTS))
@pytest.mark.parametrize("role", list(OperatorRole))
def test_every_role_against_every_requirement(name: str, role: OperatorRole) -> None:
    assert REQUIREMENTS[name].allows(role) is (role in ALLOWED[name])


def test_compliance_can_change_nothing() -> None:
    assert not OPERATOR.allows(R.COMPLIANCE)
    assert not LEAD.allows(R.COMPLIANCE)
    assert not ADMIN.allows(R.COMPLIANCE)
    assert not can_override_claim(R.COMPLIANCE)


@pytest.mark.parametrize(
    ("role", "expected"),
    [(R.OPERATOR, False), (R.LEAD, True), (R.ADMIN, True), (R.COMPLIANCE, False)],
)
def test_only_lead_and_admin_override_a_claim(role: OperatorRole, expected: bool) -> None:
    assert can_override_claim(role) is expected
