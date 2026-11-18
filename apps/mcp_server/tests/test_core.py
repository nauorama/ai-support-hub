"""The core mock: account data from the seed, expected dates, limits, nothing internal leaks."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from support_tools.adapters.core import (
    CoreAdapter,
    CoreRequestError,
    ExpectedEvent,
    LimitName,
    NotFoundError,
)
from support_tools.seed import (
    CoreSeed,
    SeedCard,
    SeedCustomer,
    SeedTransaction,
    TransactionStatus,
    TransactionType,
    load_seed,
)
from support_tools.server import DEFAULT_SEED_DIR

from .conftest import ANCHOR


def _customer(**fields: object) -> SeedCustomer:
    base: dict[str, Any] = {
        "external_ref": "CUS-0001",
        "display_name": "Test Customer",
        "email": "test@example.com",
        "tier": "verified",
        "country": "DE",
        "opened_at": "-400d 09:00",
        "balances": {"EUR": "100.00"},
        "support_scenarios": ["other"],
    }
    return SeedCustomer.model_validate(base | fields)


def _tx(
    reference: str, type: str, status: str, requested_at: str, **fields: object
) -> SeedTransaction:
    history = fields.pop("status_history", [{"status": status, "at": requested_at}])
    base: dict[str, Any] = {
        "reference": reference,
        "customer_ref": "CUS-0001",
        "type": type,
        "amount": "100.00",
        "currency": "EUR",
        "status": status,
        "requested_at": requested_at,
        "status_history": history,
        "counterparty": "Someone",
    }
    return SeedTransaction.model_validate(base | fields)


def _adapter(
    transactions: list[SeedTransaction],
    *,
    customers: list[SeedCustomer] | None = None,
    cards: list[SeedCard] | None = None,
    anchor: date = ANCHOR,
) -> CoreAdapter:
    seed = CoreSeed(
        customers=customers or [_customer()], cards=cards or [], transactions=transactions
    )
    return CoreAdapter(seed, today=lambda: anchor)


def _expected(adapter: CoreAdapter, reference: str) -> tuple[ExpectedEvent | None, date | None]:
    detail = adapter.get_transaction("CUS-0001", reference)
    return detail.expected_event, detail.expected_by


# --- expected dates (world file §5); ANCHOR is Thursday 12 Nov 2026 ---------------------------


def test_a_sepa_withdrawal_before_the_cut_off_arrives_the_next_business_day() -> None:
    # Wed 11 Nov 13:59 UTC = 14:59 CET, before 15:00.
    adapter = _adapter([_tx("TX-000001", "withdrawal_sepa", "pending", "-1d 13:59")])
    assert _expected(adapter, "TX-000001") == (ExpectedEvent.ARRIVAL, date(2026, 11, 12))


@pytest.mark.parametrize(
    "offset",
    [
        "-1d 14:20",  # Wed 11 Nov 15:20 CET: after the cut-off
        "-5d 09:00",  # Sat 7 Nov: not a business day
    ],
)
def test_a_sepa_withdrawal_outside_the_stated_rule_gets_no_date(offset: str) -> None:
    # World file §5 states only "before 15:00 CET"; the mock invents nothing else (PR #39).
    adapter = _adapter([_tx("TX-000001", "withdrawal_sepa", "pending", offset)])
    detail = adapter.get_transaction("CUS-0001", "TX-000001")
    assert (detail.expected_event, detail.expected_by, detail.overdue) == (None, None, False)


@pytest.mark.parametrize(
    ("type", "offset", "due"),
    [
        ("withdrawal_swift", "-1d 09:00", date(2026, 11, 18)),  # Wed + 5 business days
        ("incoming_sepa", "-1d 09:00", date(2026, 11, 12)),
        ("incoming_ach", "-1d 09:00", date(2026, 11, 16)),
        ("incoming_fps", "-1d 09:00", date(2026, 11, 11)),
        ("incoming_fps", "-5d 09:00", date(2026, 11, 7)),  # "same day", also on a Saturday
    ],
)
def test_pending_transfers_have_an_arrival_date(type: str, offset: str, due: date) -> None:
    currency = {"incoming_ach": "USD", "incoming_fps": "GBP"}.get(type, "EUR")
    adapter = _adapter([_tx("TX-000001", type, "pending", offset, currency=currency)])
    assert _expected(adapter, "TX-000001") == (ExpectedEvent.ARRIVAL, due)


def test_a_late_swift_withdrawal_is_overdue() -> None:
    adapter = _adapter([_tx("TX-000001", "withdrawal_swift", "pending", "-16d 10:05")])
    assert adapter.get_transaction("CUS-0001", "TX-000001").overdue is True


@pytest.mark.parametrize(
    ("offset", "due"),
    [
        ("-9d 10:00", date(2026, 11, 10)),  # Tue 3 Nov: 3 business days Fri 6, 7 days Tue 10
        ("-6d 10:00", date(2026, 11, 13)),  # Fri 6 Nov: Wed 11 against Fri 13
        ("-324d 10:00", date(2025, 12, 30)),  # Tue 23 Dec 2025, over Christmas: both Tue 30
    ],
)
def test_card_payment_funds_are_due_by_the_later_of_both_rules(offset: str, due: date) -> None:
    # No "new account" rule (not defined in the world file): the latest date either way.
    adapter = _adapter([_tx("TX-000001", "payment_request_card", "pending", offset)])
    assert _expected(adapter, "TX-000001") == (ExpectedEvent.FUNDS_AVAILABLE, due)


def test_a_payment_held_for_review_is_decided_within_three_business_days() -> None:
    history = [
        {"status": "pending", "at": "-1d 09:10"},
        {"status": "held_for_review", "at": "-1d 09:12"},
    ]
    tx = _tx(
        "TX-000001",
        "incoming_sepa",
        "held_for_review",
        "-1d 09:10",
        error_code="ERR-1012",
        status_history=history,
    )
    assert _expected(_adapter([tx]), "TX-000001") == (
        ExpectedEvent.REVIEW_DECISION,
        date(2026, 11, 16),
    )


def test_a_failed_withdrawal_returns_to_the_balance_within_seven_business_days() -> None:
    history = [{"status": "pending", "at": "-3d 09:00"}, {"status": "failed", "at": "-2d 09:00"}]
    tx = _tx(
        "TX-000001",
        "withdrawal_sepa",
        "failed",
        "-3d 09:00",
        error_code="ERR-2003",
        status_history=history,
    )
    assert _expected(_adapter([tx]), "TX-000001") == (
        ExpectedEvent.RETURN_TO_BALANCE,
        date(2026, 11, 19),
    )


@pytest.mark.parametrize(
    ("type", "status"), [("withdrawal_sepa", "completed"), ("card_payment", "pending")]
)
def test_nothing_is_expected_for_a_closed_or_untimed_transaction(type: str, status: str) -> None:
    extra = (
        {"card_ref": "CRD-0001", "merchant_category": "travel"} if type == "card_payment" else {}
    )
    adapter = _adapter([_tx("TX-000001", type, status, "-3d 09:00", **extra)])
    detail = adapter.get_transaction("CUS-0001", "TX-000001")
    assert (detail.expected_event, detail.expected_by, detail.overdue) == (None, None, False)


def _card(**fields: object) -> SeedCard:
    base: dict[str, Any] = {
        "card_ref": "CRD-0001",
        "customer_ref": "CUS-0001",
        "kind": "physical",
        "last4": "4242",
        "status": "active",
        "issued_at": "-5d 10:00",
        "delivery_status": "shipped",
        "delivery_method": "standard",
    }
    return SeedCard.model_validate(base | fields)


@pytest.mark.parametrize(
    ("country", "method", "due"),
    [
        ("DE", "standard", date(2026, 11, 26)),  # Sat 7 Nov + 14 business days
        ("GB", "standard", date(2026, 12, 7)),  # + 21
        ("DE", "express", date(2026, 11, 13)),  # + 5
    ],
)
def test_a_card_on_its_way_has_a_delivery_date(country: str, method: str, due: date) -> None:
    adapter = _adapter(
        [], customers=[_customer(country=country)], cards=[_card(delivery_method=method)]
    )
    card = adapter.get_cards("CUS-0001").cards[0]
    assert card.expected_delivery_by == due
    assert card.delivery_overdue is False


def test_a_delivered_or_virtual_card_has_no_delivery_date() -> None:
    cards = [
        _card(delivery_status="delivered"),
        _card(card_ref="CRD-0002", kind="virtual", delivery_status=None, delivery_method=None),
        _card(card_ref="CRD-0003", status="cancelled"),
    ]
    adapter = _adapter([], cards=cards)
    assert [c.expected_delivery_by for c in adapter.get_cards("CUS-0001").cards] == [None] * 3


# --- limits -----------------------------------------------------------------------------------


def test_limits_count_pending_and_completed_amounts_of_the_period_per_currency() -> None:
    # Anchor month is November; 1 Nov 00:30 CET is 31 Oct 23:30 UTC.
    txs = [
        _tx("TX-000001", "withdrawal_sepa", "completed", "-3d 09:00", amount="200.00"),
        _tx(
            "TX-000002", "withdrawal_swift", "pending", "-2d 09:00", amount="50.00", currency="USD"
        ),
        _tx(
            "TX-000003",
            "withdrawal_sepa",
            "failed",
            "-2d 10:00",
            error_code="ERR-2003",
            amount="999.00",
        ),
        _tx("TX-000004", "withdrawal_sepa", "completed", "-12d 23:30", amount="10.00"),
        _tx("TX-000005", "withdrawal_sepa", "completed", "-13d 22:00", amount="7.00"),
        _tx("TX-000006", "incoming_sepa", "completed", "-4d 09:00", amount="300.00"),
    ]
    limits = {limit.name: limit for limit in _adapter(txs).get_customer("CUS-0001").limits}
    assert limits[LimitName.WITHDRAW_PER_MONTH].used == {
        "EUR": Decimal("210.00"),
        "USD": Decimal("50.00"),
    }
    assert limits[LimitName.WITHDRAW_PER_DAY].used == {}
    assert limits[LimitName.RECEIVE_PER_MONTH].used == {"EUR": Decimal("300.00")}
    assert limits[LimitName.WITHDRAW_PER_DAY].limit_eur == Decimal(10_000)


def test_a_starter_customer_has_no_card_limits() -> None:
    adapter = _adapter([], customers=[_customer(tier="starter")])
    names = {limit.name for limit in adapter.get_customer("CUS-0001").limits}
    assert names == {
        LimitName.RECEIVE_PER_MONTH,
        LimitName.WITHDRAW_PER_DAY,
        LimitName.WITHDRAW_PER_MONTH,
    }


# --- lookups ----------------------------------------------------------------------------------


def test_transactions_are_newest_first_filtered_and_limited() -> None:
    txs = [
        _tx("TX-000001", "withdrawal_sepa", "completed", "-9d 09:00"),
        _tx("TX-000002", "withdrawal_sepa", "pending", "-2d 09:00"),
        _tx("TX-000003", "incoming_sepa", "completed", "-5d 09:00"),
    ]
    adapter = _adapter(txs)
    refs = [t.reference for t in adapter.list_transactions("CUS-0001").transactions]
    assert refs == ["TX-000002", "TX-000003", "TX-000001"]
    filtered = adapter.list_transactions(
        "CUS-0001", type=TransactionType.WITHDRAWAL_SEPA, status=TransactionStatus.COMPLETED
    )
    assert [t.reference for t in filtered.transactions] == ["TX-000001"]
    assert len(adapter.list_transactions("CUS-0001", limit=1).transactions) == 1
    with pytest.raises(CoreRequestError, match="limit"):
        adapter.list_transactions("CUS-0001", limit=21)


def test_another_customers_transaction_is_not_found_like_an_unknown_one() -> None:
    other = _customer(external_ref="CUS-0002", email="other@example.com")
    txs = [_tx("TX-000001", "incoming_sepa", "completed", "-3d 09:00", customer_ref="CUS-0002")]
    adapter = _adapter(txs, customers=[_customer(), other])
    with pytest.raises(NotFoundError) as foreign:
        adapter.get_transaction("CUS-0001", "TX-000001")
    with pytest.raises(NotFoundError) as unknown:
        adapter.get_transaction("CUS-0001", "TX-999999")
    assert str(foreign.value) == str(unknown.value) == "transaction not found"


def test_an_unknown_customer_is_not_found() -> None:
    adapter = _adapter([])
    for call in (
        lambda: adapter.get_customer("CUS-0999"),
        lambda: adapter.list_transactions("CUS-0999"),
        lambda: adapter.get_transaction("CUS-0999", "TX-000001"),
        lambda: adapter.get_cards("CUS-0999"),
    ):
        with pytest.raises(NotFoundError, match="customer not found"):
            call()


# --- the real seed ----------------------------------------------------------------------------


def _every_result(adapter: CoreAdapter) -> str:
    """Every tool result for every seed customer and transaction, as JSON."""
    seed = load_seed(DEFAULT_SEED_DIR)
    parts: list[Any] = []
    for customer in seed.customers:
        ref = customer.external_ref
        parts.append(adapter.get_customer(ref).model_dump(mode="json"))
        parts.append(adapter.list_transactions(ref, limit=20).model_dump(mode="json"))
        parts.append(adapter.get_cards(ref).model_dump(mode="json"))
    for tx in seed.transactions:
        parts.append(adapter.get_transaction(tx.customer_ref, tx.reference).model_dump(mode="json"))
    return json.dumps(parts, sort_keys=True)


def test_no_result_contains_a_restriction_reason_or_the_support_scenarios() -> None:
    seed = load_seed(DEFAULT_SEED_DIR)
    reasons = [c.restriction_reason for c in seed.customers if c.restriction_reason]
    assert reasons, "the seed has restricted customers"
    restricted = [c for c in seed.customers if c.restricted]
    adapter = CoreAdapter(seed, today=lambda: ANCHOR)
    everything = _every_result(adapter)
    assert "restriction_reason" not in everything
    assert "support_scenarios" not in everything
    for reason in reasons:
        assert reason not in everything
    for customer in restricted:
        assert adapter.get_customer(customer.external_ref).restricted is True


def test_a_fixed_anchor_gives_the_same_dates_on_two_runs() -> None:
    first = _every_result(CoreAdapter(load_seed(DEFAULT_SEED_DIR), today=lambda: ANCHOR))
    second = _every_result(CoreAdapter(load_seed(DEFAULT_SEED_DIR), today=lambda: ANCHOR))
    assert first == second
    assert '"as_of": "2026-11-12"' in first
    later = _every_result(
        CoreAdapter(load_seed(DEFAULT_SEED_DIR), today=lambda: date(2026, 11, 13))
    )
    assert later != first


def test_every_seed_date_resolves_to_before_the_anchor_day() -> None:
    adapter = CoreAdapter(load_seed(DEFAULT_SEED_DIR), today=lambda: ANCHOR)
    for tx in load_seed(DEFAULT_SEED_DIR).transactions:
        detail = adapter.get_transaction(tx.customer_ref, tx.reference)
        assert detail.requested_at.date() < ANCHOR
