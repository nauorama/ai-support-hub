"""SQLAlchemy models of the Postgres tables built in phase 2 (docs/data-model.md §1).

Conventions from the data model: primary keys are UUID v7 made in the application, timestamps
are `timestamptz`, enums are text with a `CHECK` constraint, every table has `created_at`.
The schema itself is created by the Alembic migrations in `infra/alembic/`, never from these
models; a test compares the two.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Dialect,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from uuid_utils.compat import uuid7

from hub.domain.enums import (
    ActorType,
    Channel,
    CustomerTier,
    FeedbackRating,
    Intent,
    MessageRole,
    OperatorRole,
    Resolution,
    Route,
    SettingKey,
    TicketStatus,
    Urgency,
)
from hub.domain.ticket_status import INITIAL_STATUS

# Every constraint gets a predictable name, so later migrations can alter or drop it.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TextEnum[E: StrEnum](TypeDecorator[E]):
    """A `StrEnum` stored as plain text. Not a Postgres ENUM type: adding a value is a new
    `CHECK` constraint, not an `ALTER TYPE`. The constraint comes from `enum_check`."""

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        # Through the enum, so a wrong value fails here and not only in the database.
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """`CHECK (column IN (...))` named `ck_<table>_<column>`. A NULL value passes the check;
    whether the column may be NULL is decided by its `nullable`."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=column)


def new_id() -> uuid.UUID:
    return uuid7()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: TIMESTAMP(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        str: Text(),
        dict[str, Any]: JSONB(),
        list[Any]: JSONB(),
    }


def _id_column() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=new_id)


def _created_at_column() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Customer(Base):
    """Mirror of the demo customers of the core mock: only what the Hub needs."""

    __tablename__ = "customers"
    __table_args__ = (enum_check("tier", CustomerTier),)

    id: Mapped[uuid.UUID] = _id_column()
    external_ref: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    email: Mapped[str]
    tier: Mapped[CustomerTier] = mapped_column(TextEnum(CustomerTier))
    country: Mapped[str]
    anonymised_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at_column()


class Operator(Base):
    __tablename__ = "operators"
    __table_args__ = (enum_check("role", OperatorRole),)

    id: Mapped[uuid.UUID] = _id_column()
    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    password_hash: Mapped[str]
    role: Mapped[OperatorRole] = mapped_column(TextEnum(OperatorRole))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at_column()


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        enum_check("channel", Channel),
        enum_check("status", TicketStatus),
        enum_check("intent", Intent),
        enum_check("secondary_intent", Intent),
        enum_check("urgency", Urgency),
        enum_check("last_route", Route),
        enum_check("resolution", Resolution),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("jsonb_typeof(route_reasons) = 'array'", name="route_reasons"),
        Index("ix_tickets_status_urgency_created_at", "status", "urgency", "created_at"),
        Index("ix_tickets_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_tickets_assigned_operator_id_status", "assigned_operator_id", "status"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[Channel] = mapped_column(TextEnum(Channel), default=Channel.CHAT)
    status: Mapped[TicketStatus] = mapped_column(TextEnum(TicketStatus), default=INITIAL_STATUS)
    intent: Mapped[Intent | None] = mapped_column(TextEnum(Intent))
    secondary_intent: Mapped[Intent | None] = mapped_column(TextEnum(Intent))
    urgency: Mapped[Urgency | None] = mapped_column(TextEnum(Urgency))
    language: Mapped[str | None]
    summary: Mapped[str | None]
    last_route: Mapped[Route | None] = mapped_column(TextEnum(Route))
    route_reasons: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    assigned_operator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    ai_disclosed_at: Mapped[datetime]
    first_response_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    resolution: Mapped[Resolution | None] = mapped_column(TextEnum(Resolution))
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = _created_at_column()

    # Optimistic lock: every ORM UPDATE checks and increments `version` (StaleDataError when
    # another transaction changed the row first).
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy mapper option


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        enum_check("role", MessageRole),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="citations"),
        CheckConstraint("jsonb_typeof(masked_entities) = 'object'", name="masked_entities"),
        Index("ix_messages_ticket_id_created_at", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    role: Mapped[MessageRole] = mapped_column(TextEnum(MessageRole))
    # Card data is already masked here (G1); the raw text never reaches this column.
    body: Mapped[str]
    is_ai_generated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    author_operator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    # The foreign key to `runs` is added with that table (phase 6).
    run_id: Mapped[uuid.UUID | None]
    citations: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    masked_entities: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = _created_at_column()


class Setting(Base):
    """Key-value runtime settings. Only the keys of `SettingKey` exist."""

    __tablename__ = "settings"
    __table_args__ = (enum_check("key", SettingKey),)

    key: Mapped[SettingKey] = mapped_column(TextEnum(SettingKey), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB())
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (enum_check("rating", FeedbackRating),)

    id: Mapped[uuid.UUID] = _id_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"))
    rating: Mapped[FeedbackRating] = mapped_column(TextEnum(FeedbackRating))
    comment: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at_column()


class AuditLog(Base):
    """Append-only: the app role has only INSERT and SELECT here (migrations 0001 and 0002).

    Rows are written by `hub/audit` (P2-04), which computes the hash chain. `ts` is the
    `created_at` of this table.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        enum_check("actor_type", ActorType),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[ActorType] = mapped_column(TextEnum(ActorType))
    actor_id: Mapped[str | None]
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str]
    hash: Mapped[str] = mapped_column(unique=True)
