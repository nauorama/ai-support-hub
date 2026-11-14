"""LLM call log: one row per model call attempt of the gateway (P3-01, data model §1).

`run_id` has no foreign key yet: the `runs` table is created in P3-05, and that migration adds
`llm_calls.run_id → runs.id` (owner decision, recorded in the build plan). `prompt_version` is
nullable because embeddings use no prompt file.

The value lists of the CHECK constraints are copied from `hub.domain.enums`;
tests/integration/test_schema_checks.py compares them with the enums.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NODES = (
    "'triage', 'research', 'write', 'verify', 'handoff_prep', 'memory_summary', 'judge', "
    "'embedding'"
)


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context IN ('online', 'eval', 'memory', 'record')", name=op.f("ck_llm_calls_context")
        ),
        sa.CheckConstraint(f"node IN ({NODES})", name=op.f("ck_llm_calls_node")),
        sa.CheckConstraint(
            "status IN ('ok', 'retry', 'error', 'invalid_output')",
            name=op.f("ck_llm_calls_status"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0",
            name=op.f("ck_llm_calls_tokens"),
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_llm_calls_cost_usd")),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_llm_calls_latency_ms")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_run_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")
