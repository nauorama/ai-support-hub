"""Operator login state: persistent lockout and a session version (P2-07).

`failed_logins` and `locked_until` keep the lockout after 10 wrong passwords (security §4) in the
database: Cloud Run scales to zero, so a lockout held in process memory would reset too often.
`session_version` is written into the signed `hub_operator` cookie; logout and a password change
increase it, so every earlier cookie of that operator stops working.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operators",
        sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "operators",
        sa.Column("locked_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "operators",
        sa.Column("session_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_operators_failed_logins"), "operators", "failed_logins >= 0"
    )
    op.create_check_constraint(
        op.f("ck_operators_session_version"), "operators", "session_version >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_operators_session_version"), "operators", type_="check")
    op.drop_constraint(op.f("ck_operators_failed_logins"), "operators", type_="check")
    op.drop_column("operators", "session_version")
    op.drop_column("operators", "locked_until")
    op.drop_column("operators", "failed_logins")
