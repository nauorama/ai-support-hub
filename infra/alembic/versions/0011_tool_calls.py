"""Tool calls (P7-03, data model `tool_calls`, security G6).

One row per tool call of a run, also for calls the permissions denied. `idempotency_key` is
unique and set only on successful side-effect calls (a retry after an error reuses the key).
`run_id` has no delete rule, like `drafts.ticket_id`: the rows stay with their run.

The app role gets access through the default privileges of migration 0001. The value list of
the `status` CHECK constraint is copied from `hub.domain.enums.ToolCallStatus`;
tests/unit/test_db_models.py compares them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("side_effect", sa.Boolean(), nullable=False),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'error', 'denied')", name=op.f("ck_tool_calls_status")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(arguments) = 'object'", name=op.f("ck_tool_calls_arguments")
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_tool_calls_latency_ms")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_tool_calls_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["operators.id"], name=op.f("fk_tool_calls_approved_by_operators")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_tool_calls_idempotency_key")),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
