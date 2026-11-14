"""Operator roles (api.md §1): `operator` < `lead` < `admin`; `compliance` reads, never changes.

Pure rules. The FastAPI dependency that applies them is `hub.api.dependencies.RequireRole`; every
console and admin route declares one of the requirements below (a test walks all routes).
"""

from dataclasses import dataclass

from hub.domain.enums import OperatorRole

R = OperatorRole


@dataclass(frozen=True)
class RoleRequirement:
    """The roles that may use a route. `name` is the minimum role written in api.md §3."""

    name: str
    allowed: frozenset[OperatorRole]

    def allows(self, role: OperatorRole) -> bool:
        return role in self.allowed


# Changes tickets: operator and every role above it. Compliance cannot change anything.
OPERATOR = RoleRequirement("operator", frozenset({R.OPERATOR, R.LEAD, R.ADMIN}))
# Reads tickets: as OPERATOR, plus compliance ("can read tickets and the audit log").
TICKET_READER = RoleRequirement("operator", frozenset({R.OPERATOR, R.LEAD, R.ADMIN, R.COMPLIANCE}))
LEAD = RoleRequirement("lead", frozenset({R.LEAD, R.ADMIN}))
ADMIN = RoleRequirement("admin", frozenset({R.ADMIN}))
AUDIT_READER = RoleRequirement("compliance", frozenset({R.COMPLIANCE, R.LEAD, R.ADMIN}))


def can_override_claim(role: OperatorRole) -> bool:
    """A lead (or admin) may take or release a ticket another operator has claimed."""
    return LEAD.allows(role)
