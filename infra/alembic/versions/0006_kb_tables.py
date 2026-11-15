"""Knowledge base in Postgres: `kb_documents` and `kb_chunks` (P2-09, data model §1).

Postgres is the source of truth for the KB; the Qdrant index (P5) is built from these rows.
`make kb-load` writes them as the app role, which gets read and write access through the
default privileges of migration 0001. Chunks are deleted with their document (`ON DELETE
CASCADE`): a document removed from `data/kb/` takes its chunks with it.

The value lists of the CHECK constraints are copied from `hub.domain.kb` (`Product`,
`Visibility`); tests/integration/test_schema_checks.py compares them with the enums.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCTS = (
    "'account', 'payment_requests', 'withdrawals', 'card', 'verification', 'security', "
    "'fees', 'general'"
)


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        postgresql.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("intents", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(f"product IN ({PRODUCTS})", name=op.f("ck_kb_documents_product")),
        sa.CheckConstraint(
            "visibility IN ('external', 'internal')", name=op.f("ck_kb_documents_visibility")
        ),
        sa.CheckConstraint("jsonb_typeof(intents) = 'array'", name=op.f("ck_kb_documents_intents")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_kb_documents_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
    )
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_kb_chunks_ordinal")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_kb_chunks_token_count")),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["kb_documents.id"],
            name=op.f("fk_kb_chunks_doc_id_kb_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
    )
    op.create_index("ix_kb_chunks_doc_id", "kb_chunks", ["doc_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_doc_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
