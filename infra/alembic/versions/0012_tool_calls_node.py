"""The node of each tool call (P7-04, data model `tool_calls`, security G6).

The research loop's limit (5 calls per run, denied ones too, ai-pipeline §4) is counted from the
run's `tool_calls` rows, so it holds across a resume or a second session of the same run. A run
calls tools from more than one node (intake loads the profile with `core_get_customer`, which
research may call too), so each row names its node.

Rows written before this migration get the node from their audit record (`tool.called` or
`tool.denied`, written in the same transaction, `details.node`). The value list of the CHECK
constraint is copied from `hub.domain.enums.ToolNode`; tests/unit/test_db_models.py compares
them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("node", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE tool_calls AS t
        SET node = a.details ->> 'node'
        FROM audit_log AS a
        WHERE a.action IN ('tool.called', 'tool.denied')
          AND a.entity_type = 'run'
          AND a.details ->> 'tool_call_id' = t.id::text
        """
    )
    op.alter_column("tool_calls", "node", nullable=False)
    op.create_check_constraint(
        op.f("ck_tool_calls_node"),
        "tool_calls",
        "node IN ('intake', 'research', 'handoff_prep', 'act')",
    )
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    # The limit query reads the calls of one node in one run.
    op.create_index("ix_tool_calls_run_id_node", "tool_calls", ["run_id", "node"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_run_id_node", table_name="tool_calls")
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])
    op.drop_constraint(op.f("ck_tool_calls_node"), "tool_calls", type_="check")
    op.drop_column("tool_calls", "node")
