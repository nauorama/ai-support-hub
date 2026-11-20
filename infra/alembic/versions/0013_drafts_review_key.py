"""The review request on the draft (P7-05, data model `drafts`, API §3).

`approved_actions` holds the ids of the proposed actions the operator ticked; `act` runs them
after the review (ai-pipeline §3). `review_key` is the operator-scoped `Idempotency-Key` of the
review, kept for every review, a reject without a reply included (no message holds its key). A
repeated request is compared with what the draft stored (status, final text, approved actions,
reject reason and note), so the same key with other approved actions is refused
(`409 idempotency_key_reused`) instead of returning the first result.

No backfill (owner decision, PR #42 review): rows that exist before this migration keep
`approved_actions` null (compared as "no actions") and no `review_key`, so the key of a review
made before it is not recognised and its repeat answers `409 idempotency_key_reused` (API §3).
New rows get `[]`.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added without a default, so existing rows stay null; new rows get `[]`.
    op.add_column(
        "drafts",
        sa.Column("approved_actions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column("drafts", "approved_actions", server_default=sa.text("'[]'::jsonb"))
    op.add_column("drafts", sa.Column("review_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_drafts_approved_actions"),
        "drafts",
        "approved_actions IS NULL OR jsonb_typeof(approved_actions) = 'array'",
    )
    op.create_unique_constraint(op.f("uq_drafts_review_key"), "drafts", ["review_key"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_drafts_review_key"), "drafts", type_="unique")
    op.drop_constraint(op.f("ck_drafts_approved_actions"), "drafts", type_="check")
    op.drop_column("drafts", "review_key")
    op.drop_column("drafts", "approved_actions")
