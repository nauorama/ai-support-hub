"""The enum `CHECK` constraints of the migrated database list exactly the values of the domain
enums. The value lists in the migrations are copied by hand, and `alembic check` does not compare
CHECK constraints (Alembic docs, "What does Autogenerate Detect"), so this test is what ties a
migration to `hub/domain/enums.py`."""

import re
from enum import StrEnum

import psycopg
import pytest

from hub.db.models import Base, TextEnum
from hub.observability.readiness import libpq_url

ENUM_COLUMNS: list[tuple[str, str, type[StrEnum]]] = sorted(
    (
        (table.name, column.name, column.type.enum)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, TextEnum)
    ),
    key=lambda item: (item[0], item[1]),
)


def test_enum_columns_are_found() -> None:
    assert len(ENUM_COLUMNS) == 23


@pytest.mark.parametrize(
    ("table", "column", "enum"), ENUM_COLUMNS, ids=[f"{t}.{c}" for t, c, _ in ENUM_COLUMNS]
)
def test_migrated_check_lists_exactly_the_enum_values(
    migrated_db_url: str, table: str, column: str, enum: type[StrEnum]
) -> None:
    with psycopg.connect(libpq_url(migrated_db_url)) as connection:
        row = connection.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = %s AND c.conname = %s AND c.contype = 'c'",
            (table, f"ck_{table}_{column}"),
        ).fetchone()
    assert row is not None, f"no CHECK constraint ck_{table}_{column} in the migrated database"
    definition = str(row[0])
    # Postgres renders `x IN ('a', 'b')` as `(x = ANY (ARRAY['a'::text, 'b'::text]))`.
    assert re.search(rf"\b{column}\b", definition), definition
    assert sorted(re.findall(r"'([^']*)'::text", definition)) == sorted(m.value for m in enum)
