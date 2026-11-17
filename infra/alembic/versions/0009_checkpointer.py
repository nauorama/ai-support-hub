"""LangGraph checkpoint tables (P6-01, ADR-0008, data model "LangGraph checkpoint tables").

The tables belong to `langgraph-checkpoint-postgres`: this migration runs the library's own
migration list (`AsyncPostgresSaver.MIGRATIONS`) the way its `setup()` does, recording each
step in `checkpoint_migrations`, but as the migrator inside `make migrate`, so the app role never
needs DDL rights. The default privileges of migration 0001 give the app role its DML grants on
the new tables. Some steps are `CREATE INDEX CONCURRENTLY`, which cannot run in a transaction:
they run in an autocommit block.

Running only the steps not yet recorded keeps this idempotent. Only the first `STEPS_0009` steps
(those of 3.2.0) run here, so a later library version cannot change what 0009 does; its new
steps need a new migration (an applied migration
is never edited, AGENTS.md never-do 9).

The checkpoint tables are not in the SQLAlchemy models; `env.py` leaves them out of
`alembic check`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langgraph.checkpoint.postgres.base import BasePostgresSaver

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


# The steps of langgraph-checkpoint-postgres 3.2.0. 0009 applies exactly these, whatever version
# is installed later, so it never changes meaning; a later migration applies the rest.
STEPS_0009 = 10


def apply_checkpoint_migrations(steps: int) -> None:
    """Needs a live connection: not supported in offline mode (`alembic upgrade --sql`)."""
    migrations = BasePostgresSaver.MIGRATIONS[:steps]
    with op.get_context().autocommit_block():
        op.execute(migrations[0])
        done = op.get_bind().execute(sa.text("SELECT max(v) FROM checkpoint_migrations")).scalar()
        start = -1 if done is None else int(done)
        for version in range(start + 1, len(migrations)):
            op.execute(migrations[version])
            op.get_bind().execute(
                sa.text("INSERT INTO checkpoint_migrations (v) VALUES (:v)"), {"v": version}
            )


def upgrade() -> None:
    apply_checkpoint_migrations(STEPS_0009)


def downgrade() -> None:
    for table in CHECKPOINT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
