"""Read-only schemas for the demo data in `data/seed/` (data model §3).

The full schemas and the rules across files are in `hub.domain.seed`, which this package must
not import (the two apps talk only over MCP, design §8). This is the core mock's own copy of the
shapes it reads; `tests/test_seed_agreement.py` keeps the two in agreement. Unknown fields are
refused here too, so a new seed field fails loudly instead of being dropped without notice.

`restriction_reason` and `support_scenarios` are parsed so the files validate, but no tool
result model has a field for them (data model §3): they must never leave this server.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

CUSTOMER_REF_PATTERN = r"^CUS-\d{4}$"
TRANSACTION_REF_PATTERN = r"^TX-\d{6}$"

_OFFSET = re.compile(r"^(?P<days>[+-][1-9]\d{0,3}|0)d (?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")

Offset = Annotated[str, Field(pattern=_OFFSET.pattern)]
CustomerRef = Annotated[str, Field(pattern=CUSTOMER_REF_PATTERN)]
CardRef = Annotated[str, Field(pattern=r"^CRD-\d{4}$")]
TransactionRef = Annotated[str, Field(pattern=TRANSACTION_REF_PATTERN)]
ErrorCode = Annotated[str, Field(pattern=r"^ERR-[1-5]\d{3}$")]


def resolve_offset(offset: str, anchor: date) -> datetime:
    """`"-3d 10:15"` → the UTC datetime three days before `anchor` at 10:15."""
    match = _OFFSET.fullmatch(offset)
    if match is None:
        raise ValueError(f"offset {offset!r} is not like '-3d 10:15'")
    at = time(int(match["hour"]), int(match["minute"]))
    return datetime.combine(anchor + timedelta(days=int(match["days"])), at, tzinfo=UTC)


class Tier(StrEnum):
    STARTER = "starter"
    VERIFIED = "verified"
    BUSINESS = "business"


class Currency(StrEnum):
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"


class UpgradeStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"


class CardKind(StrEnum):
    VIRTUAL = "virtual"
    PHYSICAL = "physical"


class CardStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    PIN_BLOCKED = "pin_blocked"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    ORDERED = "ordered"
    SHIPPED = "shipped"
    DELIVERED = "delivered"


class TransactionType(StrEnum):
    INCOMING_SEPA = "incoming_sepa"
    INCOMING_ACH = "incoming_ach"
    INCOMING_FPS = "incoming_fps"
    PAYMENT_REQUEST_CARD = "payment_request_card"
    PAYMENT_REQUEST_BANK = "payment_request_bank"
    WITHDRAWAL_SEPA = "withdrawal_sepa"
    WITHDRAWAL_SWIFT = "withdrawal_swift"
    CARD_PAYMENT = "card_payment"
    ATM_WITHDRAWAL = "atm_withdrawal"
    CONVERSION = "conversion"
    FEE = "fee"
    MASS_PAYOUT = "mass_payout"


class TransactionStatus(StrEnum):
    PENDING = "pending"
    HELD_FOR_REVIEW = "held_for_review"
    COMPLETED = "completed"
    FAILED = "failed"
    RETURNED = "returned"


class _Seed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedUpgrade(_Seed):
    target_tier: Tier
    status: UpgradeStatus
    submitted_at: Offset
    error_code: ErrorCode | None = None


class SeedCustomer(_Seed):
    external_ref: CustomerRef
    display_name: str
    email: str
    tier: Tier
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    opened_at: Offset
    balances: dict[Currency, Decimal]
    restricted: bool = False
    # Internal only: never in a tool result (tipping-off rule, world file §6).
    restriction_reason: str | None = None
    upgrade: SeedUpgrade | None = None
    # Test-data metadata: never in a tool result (it would leak the expected intent).
    support_scenarios: list[str]


class SeedCard(_Seed):
    card_ref: CardRef
    customer_ref: CustomerRef
    kind: CardKind
    last4: Annotated[str, Field(pattern=r"^\d{4}$")]
    status: CardStatus
    issued_at: Offset
    delivery_status: DeliveryStatus | None = None
    delivery_method: Literal["standard", "express"] | None = None


class SeedStatusChange(_Seed):
    status: TransactionStatus
    at: Offset


class SeedTransaction(_Seed):
    reference: TransactionRef
    customer_ref: CustomerRef
    type: TransactionType
    amount: Decimal
    currency: Currency
    fee: Decimal = Decimal(0)
    fee_currency: Currency | None = None
    status: TransactionStatus
    error_code: ErrorCode | None = None
    requested_at: Offset
    status_history: Annotated[list[SeedStatusChange], Field(min_length=1)]
    counterparty: str
    card_ref: CardRef | None = None
    merchant_category: str | None = None


class CoreSeed(_Seed):
    customers: list[SeedCustomer]
    cards: list[SeedCard]
    transactions: list[SeedTransaction]


_CUSTOMERS = TypeAdapter(list[SeedCustomer])
_CARDS = TypeAdapter(list[SeedCard])
_TRANSACTIONS = TypeAdapter(list[SeedTransaction])


def load_seed(seed_dir: Path) -> CoreSeed:
    """Read the three files the core mock serves (`operators.json` is the Hub's, not the core's)."""
    return CoreSeed(
        customers=_CUSTOMERS.validate_json((seed_dir / "customers.json").read_bytes()),
        cards=_CARDS.validate_json((seed_dir / "cards.json").read_bytes()),
        transactions=_TRANSACTIONS.validate_json((seed_dir / "transactions.json").read_bytes()),
    )
