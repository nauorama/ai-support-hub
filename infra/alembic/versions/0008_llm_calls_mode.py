"""`llm_calls.mode`: which provider answered, `real` or `fake` (P3-06, data model §1).

The daily budget guard (G8) sums only `real` rows: a fake-mode row repeats the usage and cost of
the recording it answered from and was never paid (owner decision, P3-05). Rows written before
this migration cannot be told apart, so they become `real`: counting too much is the safe
mistake for a budget, and it only matters for the day of the upgrade. The column then has no
default: the gateway names the mode of every row.

The value list of the CHECK constraint is copied from `hub.domain.enums.LlmCallMode`;
tests/integration/test_schema_checks.py compares it with the enum.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column("mode", sa.Text(), server_default=sa.text("'real'"), nullable=False),
    )
    op.alter_column("llm_calls", "mode", server_default=None)
    op.create_check_constraint(op.f("ck_llm_calls_mode"), "llm_calls", "mode IN ('real', 'fake')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_llm_calls_mode"), "llm_calls", type_="check")
    op.drop_column("llm_calls", "mode")
