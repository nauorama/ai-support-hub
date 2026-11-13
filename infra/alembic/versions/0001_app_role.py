"""App database role `hub_app` and its default privileges.

Least privilege (docs/security.md §4): migrations run as the migrator, the role that owns the
schema; the app connects as a login user that is a member of `hub_app`. `hub_app` is a group
role without login, so no password lives in a migration. The login user is created outside the
migrations: by `infra/postgres/init/02-app-user.sql` locally, by the deploy setup elsewhere
(docs/operations.md §2).

Default privileges give `hub_app` read and write access to every table and sequence that the
migrator creates from now on, including later tables. A table that needs less (`audit_log`)
revokes in its own migration.

Downgrade removes the grants and default privileges but keeps two things on purpose: the role
`hub_app` (see `downgrade`), and `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, which only
tightens access and is the Postgres 15+ default anyway.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Roles belong to the whole cluster (the eval database shares them), so create only once.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hub_app') THEN
                CREATE ROLE hub_app NOLOGIN;
            END IF;
        END
        $$
        """
    )
    # Postgres 15+ already denies CREATE on `public` to PUBLIC; stated here so the app role
    # cannot create objects even on a server where an older default was kept.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO hub_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hub_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO hub_app"
    )


def downgrade() -> None:
    # The role itself stays: roles belong to the whole cluster, so grants in other databases
    # and the login user (member of hub_app) still refer to it.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM hub_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM hub_app"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM hub_app")
