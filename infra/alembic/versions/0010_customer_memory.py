"""Customer memory (P6-05, data model `customer_memory`, ai-pipeline §9, ADR-0012).

One short summary per closed ticket, written by the close endpoint (and later the `close-stale`
job). `ticket_id` is unique: `closed` is final, so a ticket is summarised once, and a repeated
close cannot add a second row. `expires_at` defaults to 12 months after creation (security §6);
`intake` loads only rows that have not expired. Both foreign keys have no delete rule: erasure
(P8-05) deletes memory rows explicitly, like messages.

The app role gets access through the default privileges of migration 0001. The value list of
the `kind` CHECK constraint is copied from `hub.domain.enums.MemoryKind`;
tests/integration/test_schema_checks.py compares them.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_memory",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "expires_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now() + interval '12 months'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('ticket_summary')", name=op.f("ck_customer_memory_kind")),
        sa.CheckConstraint(
            "char_length(content) <= 400", name=op.f("ck_customer_memory_content_length")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_customer_memory_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_customer_memory_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_memory")),
        sa.UniqueConstraint("ticket_id", name=op.f("uq_customer_memory_ticket_id")),
    )
    op.create_index(
        "ix_customer_memory_customer_id_created_at",
        "customer_memory",
        ["customer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_customer_memory_customer_id_created_at", table_name="customer_memory")
    op.drop_table("customer_memory")
