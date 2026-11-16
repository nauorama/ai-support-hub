"""The SQLAlchemy models follow the data model: enum CHECKs list exactly the enum values,
constraints have predictable names, ids are UUID v7 (the KB tables use text ids). No database
needed."""

import re
import uuid
from enum import StrEnum

import pytest
from sqlalchemy import CheckConstraint, Table
from sqlalchemy.engine import make_url

from hub.db.models import Base, TextEnum, Ticket, new_id
from hub.domain.enums import (
    ActorType,
    Channel,
    CustomerTier,
    DraftStatus,
    FeedbackRating,
    Intent,
    LlmCallContext,
    LlmCallMode,
    LlmCallStatus,
    LlmNode,
    MessageRole,
    OperatorRole,
    RejectReason,
    Resolution,
    Route,
    RunStatus,
    SettingKey,
    TicketStatus,
    Urgency,
)
from hub.domain.kb import Product, Visibility

ENUM_COLUMNS: list[tuple[str, str, type[StrEnum]]] = [
    ("customers", "tier", CustomerTier),
    ("operators", "role", OperatorRole),
    ("tickets", "channel", Channel),
    ("tickets", "status", TicketStatus),
    ("tickets", "intent", Intent),
    ("tickets", "secondary_intent", Intent),
    ("tickets", "urgency", Urgency),
    ("tickets", "last_route", Route),
    ("tickets", "resolution", Resolution),
    ("messages", "role", MessageRole),
    ("settings", "key", SettingKey),
    ("feedback", "rating", FeedbackRating),
    ("audit_log", "actor_type", ActorType),
    ("llm_calls", "context", LlmCallContext),
    ("llm_calls", "mode", LlmCallMode),
    ("llm_calls", "node", LlmNode),
    ("llm_calls", "status", LlmCallStatus),
    ("runs", "status", RunStatus),
    ("runs", "route", Route),
    ("drafts", "status", DraftStatus),
    ("drafts", "reject_reason", RejectReason),
    ("kb_documents", "product", Product),
    ("kb_documents", "visibility", Visibility),
]

# The dialect the app uses (psycopg 3), without a database.
DIALECT = make_url("postgresql+psycopg://").get_dialect()()

TABLES = {
    "customers",
    "operators",
    "tickets",
    "messages",
    "settings",
    "feedback",
    "audit_log",
    "llm_calls",
    "runs",
    "drafts",
    "kb_documents",
    "kb_chunks",
}
# Tables whose primary key is text (the KB slug and `<doc_id>#<n>`, data model §1).
TEXT_KEY_TABLES = {"kb_documents", "kb_chunks"}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _check(table: str, name: str) -> CheckConstraint:
    for constraint in _table(table).constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"no CHECK constraint {name} on {table}")


def test_all_tables_exist() -> None:
    assert set(Base.metadata.tables) == TABLES


@pytest.mark.parametrize(("table", "column", "enum"), ENUM_COLUMNS)
def test_enum_check_lists_exactly_the_enum_values(
    table: str, column: str, enum: type[StrEnum]
) -> None:
    sql = str(_check(table, f"ck_{table}_{column}").sqltext)
    assert sql.startswith(f"{column} IN (")
    assert re.findall(r"'([^']*)'", sql) == [member.value for member in enum]


@pytest.mark.parametrize(("table", "column", "enum"), ENUM_COLUMNS)
def test_enum_columns_are_stored_as_text(table: str, column: str, enum: type[StrEnum]) -> None:
    column_type = _table(table).c[column].type
    assert isinstance(column_type, TextEnum)
    assert column_type.enum is enum
    assert column_type.compile(dialect=DIALECT) == "TEXT"


def test_every_enum_column_has_a_check() -> None:
    checked = {(table, column) for table, column, _ in ENUM_COLUMNS}
    enum_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, TextEnum)
    }
    assert enum_columns == checked


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_table_has_a_creation_time(table: str) -> None:
    # audit_log names it `ts`, runs `started_at` (data model §1).
    name = {"audit_log": "ts", "runs": "started_at"}.get(table, "created_at")
    column = _table(table).c[name]
    assert column.server_default is not None
    assert not column.nullable


@pytest.mark.parametrize("table", sorted(TABLES - {"settings", "audit_log"} - TEXT_KEY_TABLES))
def test_uuid_primary_keys_are_made_in_the_application(table: str) -> None:
    column = _table(table).c["id"]
    assert column.primary_key
    assert column.default is not None
    assert column.server_default is None


def test_ids_are_uuid_v7_in_creation_order() -> None:
    ids = [new_id() for _ in range(50)]
    assert all(isinstance(value, uuid.UUID) and value.version == 7 for value in ids)
    assert ids == sorted(ids)


def test_tickets_use_version_as_optimistic_lock() -> None:
    assert Ticket.__mapper__.version_id_col is _table("tickets").c["version"]


def test_text_enum_converts_both_ways() -> None:
    column_type = TextEnum(Urgency)
    assert column_type.process_bind_param(Urgency.HIGH, DIALECT) == "high"
    assert column_type.process_bind_param("low", DIALECT) == "low"
    assert column_type.process_bind_param(None, DIALECT) is None
    assert column_type.process_result_value("normal", DIALECT) is Urgency.NORMAL
    assert column_type.process_result_value(None, DIALECT) is None


def test_text_enum_rejects_unknown_values_before_the_database() -> None:
    with pytest.raises(ValueError, match="'urgent' is not a valid Urgency"):
        TextEnum(Urgency).process_bind_param("urgent", DIALECT)


def test_constraint_names_follow_the_convention() -> None:
    names = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    }
    assert None not in names
    assert {"pk_tickets", "fk_tickets_customer_id_customers", "uq_messages_idempotency_key"} <= {
        str(name) for name in names
    }
