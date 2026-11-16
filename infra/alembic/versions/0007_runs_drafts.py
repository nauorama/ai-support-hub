"""Runs and drafts (P3-05, data model §1), and the foreign keys to `runs`.

- `runs`: one execution of the workflow for one customer message.
- `drafts`: the AI draft of a run that waits for an operator (one per run at most). Two columns
  beyond the first data model: `can_answer` and `missing_information` from `DraftAnswer`, which
  the operator sees and which decide whether an unchanged draft may be approved.
- `llm_calls.run_id → runs.id` (P3-01 created the column without it) and `messages.run_id →
  runs.id`, both `ON DELETE SET NULL`: deleting old runs (retention) never deletes cost rows or
  messages. `drafts.run_id` is `ON DELETE CASCADE`: a draft belongs to its run.
- `runs.trigger_message_id` is nullable with `ON DELETE SET NULL`: erasure deletes a customer's
  messages but keeps runs for statistics (security §6).

The app role gets access through the default privileges of migration 0001. The value lists of
the CHECK constraints are copied from `hub.domain.enums`; tests/integration/test_schema_checks.py
compares them with the enums.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamp(name: str, *, default: bool) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        server_default=sa.text("now()") if default else None,
        nullable=not default,
    )


def _jsonb(name: str, empty: str) -> sa.Column[object]:
    return sa.Column(name, JSONB, server_default=sa.text(f"'{empty}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("trigger_message_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=True),
        _jsonb("route_reasons", "[]"),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        _jsonb("features", "{}"),
        sa.Column("graph_version", sa.Text(), nullable=False),
        _jsonb("prompt_versions", "{}"),
        _jsonb("models", "{}"),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        _timestamp("started_at", default=True),
        _timestamp("finished_at", default=False),
        sa.CheckConstraint(
            "status IN ('running', 'interrupted', 'completed', 'failed')",
            name=op.f("ck_runs_status"),
        ),
        sa.CheckConstraint(
            "route IN ('auto_reply', 'approval', 'handoff')", name=op.f("ck_runs_route")
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_runs_confidence")),
        sa.CheckConstraint(
            "jsonb_typeof(route_reasons) = 'array'", name=op.f("ck_runs_route_reasons")
        ),
        sa.CheckConstraint("jsonb_typeof(features) = 'object'", name=op.f("ck_runs_features")),
        sa.CheckConstraint(
            "jsonb_typeof(prompt_versions) = 'object'", name=op.f("ck_runs_prompt_versions")
        ),
        sa.CheckConstraint("jsonb_typeof(models) = 'object'", name=op.f("ck_runs_models")),
        sa.CheckConstraint(
            "total_cost_usd >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name=op.f("ck_runs_usage"),
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_runs_latency_ms")),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_runs_ticket_id_tickets")
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"],
            ["messages.id"],
            name=op.f("fk_runs_trigger_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_ticket_id_started_at", "runs", ["ticket_id", "started_at"])
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "drafts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("can_answer", sa.Boolean(), nullable=False),
        sa.Column("missing_information", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        _jsonb("citations", "[]"),
        _jsonb("evidence", "{}"),
        _jsonb("proposed_actions", "[]"),
        sa.Column("groundedness", sa.Numeric(precision=4, scale=3), nullable=True),
        _jsonb("unsupported_claims", "[]"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("final_body", sa.Text(), nullable=True),
        sa.Column("edit_ratio", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("reject_note", sa.Text(), nullable=True),
        _timestamp("created_at", default=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'edited', 'rejected', 'superseded')",
            name=op.f("ck_drafts_status"),
        ),
        sa.CheckConstraint(
            "reject_reason IN ('wrong_facts', 'wrong_tone', 'missing_info', 'policy', 'other')",
            name=op.f("ck_drafts_reject_reason"),
        ),
        sa.CheckConstraint("groundedness BETWEEN 0 AND 1", name=op.f("ck_drafts_groundedness")),
        sa.CheckConstraint("edit_ratio BETWEEN 0 AND 1", name=op.f("ck_drafts_edit_ratio")),
        sa.CheckConstraint("jsonb_typeof(citations) = 'array'", name=op.f("ck_drafts_citations")),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name=op.f("ck_drafts_evidence")),
        sa.CheckConstraint(
            "jsonb_typeof(proposed_actions) = 'array'", name=op.f("ck_drafts_proposed_actions")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(unsupported_claims) = 'array'",
            name=op.f("ck_drafts_unsupported_claims"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_drafts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_drafts_ticket_id_tickets")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["operators.id"], name=op.f("fk_drafts_reviewed_by_operators")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
        sa.UniqueConstraint("run_id", name=op.f("uq_drafts_run_id")),
    )
    op.create_index("ix_drafts_ticket_id", "drafts", ["ticket_id"])

    op.create_foreign_key(
        op.f("fk_llm_calls_run_id_runs"),
        "llm_calls",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_messages_run_id_runs"),
        "messages",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_messages_run_id_runs"), "messages", type_="foreignkey")
    op.drop_constraint(op.f("fk_llm_calls_run_id_runs"), "llm_calls", type_="foreignkey")
    op.drop_index("ix_drafts_ticket_id", table_name="drafts")
    op.drop_table("drafts")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_ticket_id_started_at", table_name="runs")
    op.drop_table("runs")
