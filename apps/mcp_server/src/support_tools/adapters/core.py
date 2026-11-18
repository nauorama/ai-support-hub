"""Core banking mock: account data from `data/seed/`, read only (API §7, data model §3).

Seed dates are offsets from an anchor date. The anchor is `CORE_ANCHOR_DATE`, or today's date in
CET when it is empty, read on every call so a long-running server moves on with the calendar.
Expected dates are computed here from the timeframes of world file §5, in business days.

Every result is a model built field by field from the seed: the restriction reason and the
support scenarios have no field in any result, so they cannot leave this server (data model §3).
"""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from support_tools.business_days import CET, add_business_days, cet_date, is_business_day
from support_tools.seed import (
    CardKind,
    CardStatus,
    CoreSeed,
    Currency,
    DeliveryStatus,
    SeedCard,
    SeedCustomer,
    SeedTransaction,
    Tier,
    TransactionStatus,
    TransactionType,
    UpgradeStatus,
    resolve_offset,
)

MAX_TRANSACTIONS = 20


class CoreRequestError(Exception):
    """A request the core mock refuses. The message is a constant: it never holds a value."""


class NotFoundError(CoreRequestError):
    """The customer or transaction does not exist for this customer (one message for both)."""


# --- results (the output schemas of the tools) -------------------------------------------------


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class Upgrade(_Result):
    target_tier: Tier
    status: UpgradeStatus
    submitted_at: datetime
    error_code: str | None


class Verification(_Result):
    # Every seed customer finished the checks of the current tier (data model §3).
    current_tier_checks: Literal["complete"]
    upgrade: Upgrade | None


class LimitName(StrEnum):
    RECEIVE_PER_MONTH = "receive_per_month"
    WITHDRAW_PER_DAY = "withdraw_per_day"
    WITHDRAW_PER_MONTH = "withdraw_per_month"
    CARD_SPEND_PER_DAY = "card_spend_per_day"
    ATM_WITHDRAWAL_PER_DAY = "atm_withdrawal_per_day"


class Limit(_Result):
    name: LimitName
    limit_eur: Decimal
    # Per currency, not converted: the mock has no exchange rates. Pending and completed count.
    used: dict[Currency, Decimal]


class CustomerProfile(_Result):
    customer_ref: str
    tier: Tier
    country: str
    verification: Verification
    # The flag only, never the reason (tipping-off rule, world file §6).
    restricted: bool
    limits: list[Limit]
    as_of: date


class TransactionSummary(_Result):
    reference: str
    type: TransactionType
    amount: Decimal
    currency: Currency
    fee: Decimal
    fee_currency: Currency
    status: TransactionStatus
    error_code: str | None
    requested_at: datetime
    updated_at: datetime


class TransactionList(_Result):
    transactions: list[TransactionSummary]
    as_of: date


class StatusChange(_Result):
    status: TransactionStatus
    at: datetime


class ExpectedEvent(StrEnum):
    ARRIVAL = "arrival"
    FUNDS_AVAILABLE = "funds_available"
    REVIEW_DECISION = "review_decision"
    RETURN_TO_BALANCE = "return_to_balance"


class TransactionDetail(TransactionSummary):
    counterparty: str
    card_ref: str | None
    merchant_category: str | None
    status_history: list[StatusChange]
    # The latest date (CET) by which the next event is due; null when nothing is expected.
    expected_event: ExpectedEvent | None
    expected_by: date | None
    overdue: bool
    as_of: date


class Card(_Result):
    card_ref: str
    kind: CardKind
    last4: str
    status: CardStatus
    issued_at: datetime
    delivery_status: DeliveryStatus | None
    delivery_method: Literal["standard", "express"] | None
    expected_delivery_by: date | None
    delivery_overdue: bool


class CardList(_Result):
    cards: list[Card]
    as_of: date


# --- facts from the world file ----------------------------------------------------------------

# World file §3, in EUR equivalent. Starter has no card, so no card limits.
LIMITS_EUR: dict[Tier, dict[LimitName, Decimal]] = {
    Tier.STARTER: {
        LimitName.RECEIVE_PER_MONTH: Decimal(1_000),
        LimitName.WITHDRAW_PER_DAY: Decimal(500),
        LimitName.WITHDRAW_PER_MONTH: Decimal(1_000),
    },
    Tier.VERIFIED: {
        LimitName.RECEIVE_PER_MONTH: Decimal(30_000),
        LimitName.WITHDRAW_PER_DAY: Decimal(10_000),
        LimitName.WITHDRAW_PER_MONTH: Decimal(30_000),
        LimitName.CARD_SPEND_PER_DAY: Decimal(5_000),
        LimitName.ATM_WITHDRAWAL_PER_DAY: Decimal(1_000),
    },
    Tier.BUSINESS: {
        LimitName.RECEIVE_PER_MONTH: Decimal(250_000),
        LimitName.WITHDRAW_PER_DAY: Decimal(50_000),
        LimitName.WITHDRAW_PER_MONTH: Decimal(250_000),
        LimitName.CARD_SPEND_PER_DAY: Decimal(15_000),
        LimitName.ATM_WITHDRAWAL_PER_DAY: Decimal(2_000),
    },
}

INCOMING = frozenset(
    {
        TransactionType.INCOMING_SEPA,
        TransactionType.INCOMING_ACH,
        TransactionType.INCOMING_FPS,
        TransactionType.PAYMENT_REQUEST_CARD,
        TransactionType.PAYMENT_REQUEST_BANK,
    }
)
WITHDRAWALS = frozenset({TransactionType.WITHDRAWAL_SEPA, TransactionType.WITHDRAWAL_SWIFT})
_LIMIT_TYPES: dict[LimitName, frozenset[TransactionType]] = {
    LimitName.RECEIVE_PER_MONTH: INCOMING,
    LimitName.WITHDRAW_PER_DAY: WITHDRAWALS,
    LimitName.WITHDRAW_PER_MONTH: WITHDRAWALS,
    LimitName.CARD_SPEND_PER_DAY: frozenset({TransactionType.CARD_PAYMENT}),
    LimitName.ATM_WITHDRAWAL_PER_DAY: frozenset({TransactionType.ATM_WITHDRAWAL}),
}
_MONTHLY = frozenset({LimitName.RECEIVE_PER_MONTH, LimitName.WITHDRAW_PER_MONTH})
# Failed and returned transactions moved no money, so they use no limit.
_COUNTS_TOWARD_LIMIT = frozenset(
    {
        TransactionStatus.PENDING,
        TransactionStatus.HELD_FOR_REVIEW,
        TransactionStatus.COMPLETED,
    }
)

SEPA_CUTOFF_CET = time(15, 0)
# Business days until the latest expected arrival of a pending transfer (world file §5).
ARRIVAL_DAYS = {
    TransactionType.WITHDRAWAL_SWIFT: 5,
    TransactionType.INCOMING_SEPA: 1,
    TransactionType.INCOMING_ACH: 3,
    TransactionType.INCOMING_FPS: 0,
}
CARD_FUNDS_DAYS = 3
# New accounts wait 7 calendar days for their first 3 card payments. The world file does not
# define a "new account", so the mock does not decide it: `expected_by` is the later of the two
# dates, the latest the funds can be due either way (owner decision, PR #39).
NEW_ACCOUNT_CARD_FUNDS_CALENDAR_DAYS = 7
REVIEW_DAYS = 3
RETURN_DAYS = 7
# Physical card delivery, latest business day (world file §5).
DELIVERY_DAYS_EU = 14
DELIVERY_DAYS_ELSEWHERE = 21
DELIVERY_DAYS_EXPRESS = 5

EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }
)  # fmt: skip


# --- the adapter ------------------------------------------------------------------------------


class CoreAdapter:
    def __init__(self, seed: CoreSeed, today: Callable[[], date]) -> None:
        self._customers = {c.external_ref: c for c in seed.customers}
        self._cards: dict[str, list[SeedCard]] = {}
        for card in seed.cards:
            self._cards.setdefault(card.customer_ref, []).append(card)
        self._transactions: dict[str, list[SeedTransaction]] = {}
        for tx in seed.transactions:
            self._transactions.setdefault(tx.customer_ref, []).append(tx)
        self._today = today

    def require_customer(self, customer_ref: str) -> None:
        """Raise `NotFoundError` unless the customer exists (the Jira mock checks this)."""
        self._customer(customer_ref)

    def _customer(self, customer_ref: str) -> SeedCustomer:
        customer = self._customers.get(customer_ref)
        if customer is None:
            raise NotFoundError("customer not found")
        return customer

    def get_customer(self, customer_ref: str) -> CustomerProfile:
        customer = self._customer(customer_ref)
        anchor = self._today()
        upgrade = None
        if customer.upgrade is not None:
            upgrade = Upgrade(
                target_tier=customer.upgrade.target_tier,
                status=customer.upgrade.status,
                submitted_at=resolve_offset(customer.upgrade.submitted_at, anchor),
                error_code=customer.upgrade.error_code,
            )
        txs = self._transactions.get(customer_ref, [])
        limits = [
            Limit(name=name, limit_eur=amount, used=_used(txs, name, anchor))
            for name, amount in LIMITS_EUR[customer.tier].items()
        ]
        return CustomerProfile(
            customer_ref=customer.external_ref,
            tier=customer.tier,
            country=customer.country,
            verification=Verification(current_tier_checks="complete", upgrade=upgrade),
            restricted=customer.restricted,
            limits=limits,
            as_of=anchor,
        )

    def list_transactions(
        self,
        customer_ref: str,
        type: TransactionType | None = None,
        status: TransactionStatus | None = None,
        limit: int = 10,
    ) -> TransactionList:
        self._customer(customer_ref)
        if not 1 <= limit <= MAX_TRANSACTIONS:
            raise CoreRequestError(f"limit must be 1-{MAX_TRANSACTIONS}")
        anchor = self._today()
        txs = [
            tx
            for tx in self._transactions.get(customer_ref, [])
            if (type is None or tx.type is type) and (status is None or tx.status is status)
        ]
        txs.sort(key=lambda tx: (resolve_offset(tx.requested_at, anchor), tx.reference))
        newest = list(reversed(txs))[:limit]
        return TransactionList(
            transactions=[_summary(tx, anchor) for tx in newest],
            as_of=anchor,
        )

    def get_transaction(self, customer_ref: str, reference: str) -> TransactionDetail:
        self._customer(customer_ref)
        txs = self._transactions.get(customer_ref, [])
        tx = next((t for t in txs if t.reference == reference), None)
        if tx is None:
            # The same message whether the reference is unknown or another customer's.
            raise NotFoundError("transaction not found")
        anchor = self._today()
        event, expected = _expected(tx, anchor)
        return TransactionDetail(
            **_summary(tx, anchor).model_dump(),
            counterparty=tx.counterparty,
            card_ref=tx.card_ref,
            merchant_category=tx.merchant_category,
            status_history=[
                StatusChange(status=c.status, at=resolve_offset(c.at, anchor))
                for c in tx.status_history
            ],
            expected_event=event,
            expected_by=expected,
            overdue=expected is not None and expected < anchor,
            as_of=anchor,
        )

    def get_cards(self, customer_ref: str) -> CardList:
        customer = self._customer(customer_ref)
        anchor = self._today()
        cards = []
        for card in self._cards.get(customer_ref, []):
            expected = _delivery_by(card, customer.country, anchor)
            cards.append(
                Card(
                    card_ref=card.card_ref,
                    kind=card.kind,
                    last4=card.last4,
                    status=card.status,
                    issued_at=resolve_offset(card.issued_at, anchor),
                    delivery_status=card.delivery_status,
                    delivery_method=card.delivery_method,
                    expected_delivery_by=expected,
                    delivery_overdue=expected is not None and expected < anchor,
                )
            )
        return CardList(cards=cards, as_of=anchor)


def _summary(tx: SeedTransaction, anchor: date) -> TransactionSummary:
    return TransactionSummary(
        reference=tx.reference,
        type=tx.type,
        amount=tx.amount,
        currency=tx.currency,
        fee=tx.fee,
        fee_currency=tx.fee_currency or tx.currency,
        status=tx.status,
        error_code=tx.error_code,
        requested_at=resolve_offset(tx.requested_at, anchor),
        updated_at=resolve_offset(tx.status_history[-1].at, anchor),
    )


def _used(txs: list[SeedTransaction], name: LimitName, anchor: date) -> dict[Currency, Decimal]:
    """Amounts on the anchor day (daily limits) or in the anchor month (monthly), in CET."""
    used: dict[Currency, Decimal] = {}
    for tx in txs:
        if tx.type not in _LIMIT_TYPES[name] or tx.status not in _COUNTS_TOWARD_LIMIT:
            continue
        day = cet_date(resolve_offset(tx.requested_at, anchor))
        same_period = (
            (day.year, day.month) == (anchor.year, anchor.month)
            if name in _MONTHLY
            else day == anchor
        )
        if same_period:
            used[tx.currency] = used.get(tx.currency, Decimal(0)) + tx.amount
    return used


def _last_change(tx: SeedTransaction, status: TransactionStatus, anchor: date) -> datetime:
    at = next(c.at for c in reversed(tx.status_history) if c.status is status)
    return resolve_offset(at, anchor)


def _expected(tx: SeedTransaction, anchor: date) -> tuple[ExpectedEvent | None, date | None]:
    """The next event of an open transaction and the latest CET date it is due (world file §5)."""
    requested = resolve_offset(tx.requested_at, anchor)
    start = cet_date(requested)
    if tx.status is TransactionStatus.HELD_FOR_REVIEW:
        held = cet_date(_last_change(tx, TransactionStatus.HELD_FOR_REVIEW, anchor))
        return ExpectedEvent.REVIEW_DECISION, add_business_days(held, REVIEW_DAYS)
    if tx.status is TransactionStatus.FAILED and tx.type in WITHDRAWALS:
        failed = cet_date(_last_change(tx, TransactionStatus.FAILED, anchor))
        return ExpectedEvent.RETURN_TO_BALANCE, add_business_days(failed, RETURN_DAYS)
    if tx.status is not TransactionStatus.PENDING:
        return None, None
    if tx.type is TransactionType.WITHDRAWAL_SEPA:
        # World file §5 gives a time only for a request before 15:00 CET on a business day;
        # after the cut-off or on a closing day the mock states no date (owner decision, PR #39).
        local = requested.astimezone(CET)
        if not (is_business_day(start) and local.time() < SEPA_CUTOFF_CET):
            return None, None
        return ExpectedEvent.ARRIVAL, add_business_days(start, 1)
    if tx.type in ARRIVAL_DAYS:
        # Faster Payments: "same day", the calendar day of the request.
        return ExpectedEvent.ARRIVAL, add_business_days(start, ARRIVAL_DAYS[tx.type])
    if tx.type is TransactionType.PAYMENT_REQUEST_CARD:
        business = add_business_days(start, CARD_FUNDS_DAYS)
        calendar = start + timedelta(days=NEW_ACCOUNT_CARD_FUNDS_CALENDAR_DAYS)
        return ExpectedEvent.FUNDS_AVAILABLE, max(business, calendar)
    return None, None


def _delivery_by(card: SeedCard, country: str, anchor: date) -> date | None:
    if card.kind is not CardKind.PHYSICAL or card.delivery_status is DeliveryStatus.DELIVERED:
        return None
    if card.status is CardStatus.CANCELLED:
        return None
    if card.delivery_method == "express":
        days = DELIVERY_DAYS_EXPRESS
    elif country in EU_COUNTRIES:
        days = DELIVERY_DAYS_EU
    else:
        days = DELIVERY_DAYS_ELSEWHERE
    return add_business_days(cet_date(resolve_offset(card.issued_at, anchor)), days)
