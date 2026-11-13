"""`tickets.version` defaults to 1 in the database too.

The ORM sets the first version itself (`version_id_col`), but a plain SQL insert, such as a seed
loader, would otherwise have to set it by hand. A new migration, not an edit of 0002, because
0002 was already applied (AGENTS.md "Never do" 9).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("tickets", "version", server_default=sa.text("1"))


def downgrade() -> None:
    op.alter_column("tickets", "version", server_default=None)
