"""Core tables of phase 2: customers, operators, tickets, messages, settings, feedback, audit_log.

Enums are text with a `CHECK` constraint (docs/data-model.md §1). The value lists are copied
here on purpose: a migration must not change when the code changes later. A test checks that
the migrated schema matches `hub/db/models.py`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTENTS = (
    "'payment_pending', 'withdrawal_issue', 'card_declined', 'card_lost_stolen', "
    "'kyc_verification', 'account_restricted', 'fees_fx', 'limits', 'dispute_chargeback', "
    "'login_security', 'account_management', 'other'"
)
SETTING_KEYS = (
    "'auto_reply_enabled', 'confidence_threshold', 'daily_budget_usd', 'intent_allowlist'"
)


def upgrade() -> None:

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('ai', 'operator', 'customer', 'system')",
            name=op.f("ck_audit_log_actor_type"),
        ),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name=op.f("ck_audit_log_details")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_log_hash")),
    )
    # Append-only (docs/security.md §4-5): the default privileges of 0001 granted the app role
    # UPDATE and DELETE on this table; take them back. TRUNCATE was never granted.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM hub_app")
    op.create_table(
        "customers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("anonymised_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tier IN ('starter', 'verified', 'business')", name=op.f("ck_customers_tier")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("external_ref", name=op.f("uq_customers_external_ref")),
    )
    op.create_table(
        "operators",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('operator', 'lead', 'compliance', 'admin')", name=op.f("ck_operators_role")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operators")),
        sa.UniqueConstraint("email", name=op.f("uq_operators_email")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"key IN ({SETTING_KEYS})",
            name=op.f("ck_settings_key"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("secondary_intent", sa.Text(), nullable=True),
        sa.Column("urgency", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("last_route", sa.Text(), nullable=True),
        sa.Column(
            "route_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("assigned_operator_id", sa.UUID(), nullable=True),
        sa.Column("ai_disclosed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("first_response_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("channel IN ('chat')", name=op.f("ck_tickets_channel")),
        sa.CheckConstraint(
            f"intent IN ({INTENTS})",
            name=op.f("ck_tickets_intent"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(route_reasons) = 'array'", name=op.f("ck_tickets_route_reasons")
        ),
        sa.CheckConstraint(
            "last_route IN ('auto_reply', 'approval', 'handoff')",
            name=op.f("ck_tickets_last_route"),
        ),
        sa.CheckConstraint(
            "resolution IN ('ai', 'ai_approved', 'ai_edited', 'human')",
            name=op.f("ck_tickets_resolution"),
        ),
        sa.CheckConstraint(
            f"secondary_intent IN ({INTENTS})",
            name=op.f("ck_tickets_secondary_intent"),
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'pending_approval', 'handed_off', 'answered', 'closed')",
            name=op.f("ck_tickets_status"),
        ),
        sa.CheckConstraint("urgency IN ('low', 'normal', 'high')", name=op.f("ck_tickets_urgency")),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_tickets_confidence")),
        sa.ForeignKeyConstraint(
            ["assigned_operator_id"],
            ["operators.id"],
            name=op.f("fk_tickets_assigned_operator_id_operators"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_tickets_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(
        "ix_tickets_assigned_operator_id_status",
        "tickets",
        ["assigned_operator_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_customer_id_created_at", "tickets", ["customer_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_tickets_status_urgency_created_at",
        "tickets",
        ["status", "urgency", "created_at"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_ai_generated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("author_operator_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "masked_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("jsonb_typeof(citations) = 'array'", name=op.f("ck_messages_citations")),
        sa.CheckConstraint(
            "jsonb_typeof(masked_entities) = 'object'", name=op.f("ck_messages_masked_entities")
        ),
        sa.CheckConstraint(
            "role IN ('customer', 'assistant', 'operator', 'system')", name=op.f("ck_messages_role")
        ),
        sa.ForeignKeyConstraint(
            ["author_operator_id"],
            ["operators.id"],
            name=op.f("fk_messages_author_operator_id_operators"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_messages_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_messages_idempotency_key")),
    )
    op.create_index(
        "ix_messages_ticket_id_created_at", "messages", ["ticket_id", "created_at"], unique=False
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rating IN ('up', 'down')", name=op.f("ck_feedback_rating")),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_feedback_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_feedback_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )


def downgrade() -> None:

    op.drop_table("feedback")
    op.drop_index("ix_messages_ticket_id_created_at", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_tickets_status_urgency_created_at", table_name="tickets")
    op.drop_index("ix_tickets_customer_id_created_at", table_name="tickets")
    op.drop_index("ix_tickets_assigned_operator_id_status", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("settings")
    op.drop_table("operators")
    op.drop_table("customers")
    op.drop_table("audit_log")
