"""Schemas and cross-file rules of the seed data (`hub.domain.seed`), on a synthetic dataset.

`_valid_seed()` builds a small dataset that passes every rule; each test breaks one rule.
The real files in `data/seed/` are checked in test_seed_files.py.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from hub.domain.enums import CustomerTier, Intent, OperatorRole
from hub.domain.seed import (
    CUSTOMER_COUNT,
    Card,
    Customer,
    Operator,
    SeedData,
    Transaction,
    Upgrade,
    parse_offset,
    parse_seed,
    resolve_offset,
    validate_seed,
)

INTENTS = list(Intent)


def _customer(i: int, **changes: Any) -> Customer:  # noqa: ANN401
    intent = INTENTS[i % len(INTENTS)]
    restricted = intent is Intent.ACCOUNT_RESTRICTED
    data: dict[str, Any] = {
        "external_ref": f"CUS-{i:04d}",
        "display_name": f"Test Customer {i}",
        "email": f"customer{i}@example.com",
        "tier": "verified",
        "country": "DE",
        "opened_at": "-900d 09:00",
        "balances": {"EUR": "100.00"},
        "restricted": restricted,
        "restriction_reason": "Source of funds review" if restricted else None,
        "upgrade": {"target_tier": "business", "status": "pending", "submitted_at": "-2d 10:00"},
        "support_scenarios": [intent],
    }
    data.update(changes)
    return Customer.model_validate(data)


def _card(i: int, **changes: Any) -> Card:  # noqa: ANN401
    data: dict[str, Any] = {
        "card_ref": f"CRD-{i:04d}",
        "customer_ref": f"CUS-{i:04d}",
        "kind": "physical",
        "last4": "4242",
        "status": "active",
        "issued_at": "-300d 10:00",
        "delivery_status": "delivered",
        "delivery_method": "standard",
    }
    data.update(changes)
    return Card.model_validate(data)


def _tx(ref: int, customer: int, **changes: Any) -> Transaction:  # noqa: ANN401
    data: dict[str, Any] = {
        "reference": f"TX-{ref:06d}",
        "customer_ref": f"CUS-{customer:04d}",
        "type": "incoming_sepa",
        "amount": "100.00",
        "currency": "EUR",
        "status": "completed",
        "requested_at": "-5d 10:00",
        "counterparty": "Acme Client GmbH",
    }
    data.update(changes)
    if "status_history" not in changes:
        data["status_history"] = [{"status": data["status"], "at": data["requested_at"]}]
    return Transaction.model_validate(data)


def _transactions(customer: int) -> list[Transaction]:
    """Six transactions that back every scenario check."""
    base = customer * 10
    card = f"CRD-{customer:04d}"
    return [
        _tx(base + 1, customer, type="incoming_ach", currency="USD", status="pending",
            requested_at="-1d 09:00"),
        _tx(base + 2, customer, type="withdrawal_swift", status="pending", fee="15.00",
            requested_at="-1d 09:00"),
        _tx(base + 3, customer, type="card_payment", status="failed", error_code="ERR-3001",
            card_ref=card, merchant_category="groceries", requested_at="-1d 12:00"),
        _tx(base + 4, customer, type="card_payment", card_ref=card,
            merchant_category="software", requested_at="-10d 12:00"),
        _tx(base + 5, customer, type="withdrawal_sepa", status="failed", error_code="ERR-2001",
            requested_at="0d 09:00"),
        _tx(base + 6, customer, type="conversion", requested_at="-3d 11:00"),
    ]  # fmt: skip


def _operators() -> list[Operator]:
    return [
        Operator(email=f"{role}@tallybridge.example", name=f"Demo {role}", role=role)
        for role in OperatorRole
    ]


def _valid_seed() -> SeedData:
    numbers = range(1, CUSTOMER_COUNT + 1)
    return SeedData(
        customers=[_customer(i) for i in numbers],
        cards=[_card(i) for i in numbers],
        transactions=[tx for i in numbers for tx in _transactions(i)],
        operators=_operators(),
    )


def _replace(seed: SeedData, **lists: Any) -> SeedData:  # noqa: ANN401
    return seed.model_copy(update=lists)


def _replace_tx(seed: SeedData, reference: str, tx: Transaction) -> SeedData:
    return _replace(
        seed, transactions=[tx if t.reference == reference else t for t in seed.transactions]
    )


# --- offsets ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "days", "at"),
    [
        ("-3d 10:15", -3, time(10, 15)),
        ("0d 00:00", 0, time(0, 0)),
        ("+2d 23:59", 2, time(23, 59)),
        ("-1200d 09:00", -1200, time(9, 0)),
    ],
)
def test_parse_offset(offset: str, days: int, at: time) -> None:
    assert parse_offset(offset) == (days, at)


@pytest.mark.parametrize(
    "offset",
    ["-3d", "3d 10:15", "-0d 10:00", "+0d 10:00", "-3d 24:00", "-3d 10:60", "-3d 9:15",
     "-03d 10:15", "-3d  10:15", "-3 d 10:15", "", "-3d 10:15 ", "-12345d 10:00"],
)  # fmt: skip
def test_parse_offset_refuses_other_shapes(offset: str) -> None:
    with pytest.raises(ValueError, match="offset"):
        parse_offset(offset)


def test_resolve_offset_is_utc_and_crosses_month_and_year_ends() -> None:
    assert resolve_offset("-3d 10:15", date(2026, 3, 2)) == datetime(
        2026, 2, 27, 10, 15, tzinfo=UTC
    )
    assert resolve_offset("+1d 08:00", date(2026, 12, 31)) == datetime(2027, 1, 1, 8, 0, tzinfo=UTC)
    assert resolve_offset("0d 15:00", date(2026, 11, 12)).tzinfo is UTC


def test_the_same_anchor_gives_the_same_datetime() -> None:
    anchor = date(2026, 11, 12)
    assert resolve_offset("-14d 09:30", anchor) == resolve_offset("-14d 09:30", anchor)


def test_a_model_refuses_a_bad_offset() -> None:
    with pytest.raises(ValidationError, match="offset"):
        _tx(1, 1, requested_at="three days ago")


# --- the valid dataset ------------------------------------------------------------------------


def test_the_synthetic_dataset_is_valid() -> None:
    assert validate_seed(_valid_seed()) == []


def test_parse_seed_reads_the_four_files() -> None:
    seed = _valid_seed()
    files = {
        "customers.json": _json(seed.customers),
        "cards.json": _json(seed.cards),
        "transactions.json": _json(seed.transactions),
        "operators.json": _json(seed.operators),
    }
    assert parse_seed(files) == seed


def _json(items: list[Any]) -> bytes:
    return ("[" + ",".join(item.model_dump_json() for item in items) + "]").encode()


def test_amounts_keep_their_exact_decimal_value() -> None:
    assert _tx(1, 1, amount="0.10").amount == Decimal("0.10")
    with pytest.raises(ValidationError):
        _tx(1, 1, amount="0.105")
    with pytest.raises(ValidationError):
        _tx(1, 1, amount="0.00")


# --- single-model rules -----------------------------------------------------------------------


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        _customer(1, pan="4111111111111111")


@pytest.mark.parametrize(
    "changes",
    [
        {"restricted": True, "restriction_reason": None},
        {"restricted": False, "restriction_reason": "review"},
        {"restricted": True, "restriction_reason": "review", "support_scenarios": ["fees_fx"]},
        {"support_scenarios": ["fees_fx", "fees_fx"]},
        {"support_scenarios": []},
        {"upgrade": {"target_tier": "verified", "status": "pending", "submitted_at": "-1d 09:00"}},
        {"email": "someone@gmail.com"},
        {"country": "Germany"},
        {"balances": {"EUR": "-1.00"}},
        {"balances": {"CHF": "1.00"}},
    ],
)
def test_customer_rules(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _customer(1, **changes)


@pytest.mark.parametrize(
    "upgrade",
    [
        {"target_tier": "business", "status": "rejected", "submitted_at": "-3d 09:00"},
        {"target_tier": "business", "status": "pending", "submitted_at": "-3d 09:00",
         "error_code": "ERR-4001"},
        {"target_tier": "business", "status": "rejected", "submitted_at": "-3d 09:00",
         "error_code": "ERR-2001"},
    ],
)  # fmt: skip
def test_upgrade_rules(upgrade: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Upgrade.model_validate(upgrade)


@pytest.mark.parametrize(
    "changes",
    [
        {"last4": "424"},
        {"last4": "4111111111111111"},
        {"kind": "virtual"},
        {"delivery_status": None},
    ],
)
def test_card_rules(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _card(1, **changes)


def test_a_virtual_card_has_no_delivery() -> None:
    card = _card(1, kind="virtual", delivery_status=None, delivery_method=None)
    assert card.delivery_status is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "failed"}, "error_code is required"),
        ({"error_code": "ERR-1004"}, "error_code is required"),
        ({"status": "held_for_review", "error_code": "ERR-1004"}, "held_for_review uses"),
        ({"status": "failed", "error_code": "ERR-1999"}, "not in the world file"),
        ({"status": "failed", "error_code": "ERR-2001"}, "does not fit"),
        ({"status": "returned", "error_code": "ERR-1004"}, "only withdrawals"),
        ({"type": "incoming_ach"}, "carries USD"),
        ({"type": "card_payment", "merchant_category": "x"}, "card_ref is set exactly"),
        ({"type": "atm_withdrawal", "card_ref": "CRD-0001", "merchant_category": "x"},
         "merchant_category is set exactly"),
        ({"type": "withdrawal_sepa", "requested_at": "-1d 13:30"}, "15:00 CET"),
        ({"status_history": [{"status": "completed", "at": "-4d 10:00"}]}, "start at"),
        ({"status_history": [{"status": "completed", "at": "-5d 10:00"},
                             {"status": "pending", "at": "-4d 10:00"}]}, "must equal status"),
        ({"status_history": [{"status": "pending", "at": "-5d 10:00"},
                             {"status": "pending", "at": "-5d 10:00"},
                             {"status": "completed", "at": "-6d 10:00"}]}, "time order"),
    ],
)  # fmt: skip
def test_transaction_rules(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _tx(1, 1, **changes)


@pytest.mark.parametrize("at", ["-1d 12:59", "-1d 14:00", "0d 09:00", "0d 18:30"])
def test_sepa_withdrawal_times_outside_the_cutoff_gap_are_accepted(at: str) -> None:
    assert _tx(1, 1, type="withdrawal_sepa", requested_at=at).requested_at == at


def test_restricted_code_fits_withdrawals_and_cards() -> None:
    tx = _tx(1, 1, type="withdrawal_swift", status="failed", error_code="ERR-5010")
    assert tx.error_code == "ERR-5010"


# --- cross-file rules -------------------------------------------------------------------------


def test_duplicates_are_reported() -> None:
    seed = _valid_seed()
    broken = _replace(
        seed,
        customers=[*seed.customers[:-1], _customer(1, external_ref="CUS-0040")],
        operators=[*seed.operators, seed.operators[0]],
    )
    errors = validate_seed(broken)
    assert "duplicate customer email: customer1@example.com" in errors
    assert "duplicate operator email: operator@tallybridge.example" in errors


def test_the_customer_count_is_checked() -> None:
    seed = _valid_seed()
    errors = validate_seed(_replace(seed, customers=seed.customers[:-1]))
    assert "expected 40 customers, found 39" in errors


def test_every_operator_role_is_needed() -> None:
    seed = _valid_seed()
    errors = validate_seed(_replace(seed, operators=seed.operators[:1]))
    assert any(e.startswith("no operator with role") for e in errors)


def test_unknown_references_are_reported() -> None:
    seed = _valid_seed()
    broken = _replace(
        seed,
        cards=[*seed.cards, _card(99)],
        transactions=[*seed.transactions, _tx(999_999, 99)],
    )
    errors = validate_seed(broken)
    assert "CRD-0099: unknown customer CUS-0099" in errors
    assert "TX-999999: unknown customer CUS-0099" in errors


def test_a_card_payment_must_use_the_customers_own_card() -> None:
    seed = _valid_seed()
    tx = _tx(14, 1, type="card_payment", card_ref="CRD-0002", merchant_category="software",
             requested_at="-10d 12:00")  # fmt: skip
    assert "TX-000014: card CRD-0002 is not the customer's card" in validate_seed(
        _replace_tx(seed, "TX-000014", tx)
    )


def test_card_allowance_and_express_delivery() -> None:
    seed = _valid_seed()
    broken = _replace(
        seed,
        cards=[
            *seed.cards,
            _card(41, customer_ref="CUS-0001"),
            _card(42, customer_ref="CUS-0002", status="cancelled"),
            _card(43, customer_ref="CUS-0003", delivery_method="express", status="cancelled"),
        ],
        customers=[
            c if c.external_ref != "CUS-0003" else _customer(3, country="US")
            for c in seed.customers
        ],
    )
    errors = validate_seed(broken)
    assert "CUS-0001: more cards than the verified tier allows" in errors
    assert not any(e.startswith("CUS-0002") for e in errors)  # cancelled cards do not count
    assert "CRD-0043: express delivery is EU only" in errors


def test_tier_rules_for_transactions() -> None:
    seed = _valid_seed()
    starter = _customer(5, tier="starter", upgrade=None)
    customers = [starter if c.external_ref == "CUS-0005" else c for c in seed.customers]
    broken = _replace(
        seed,
        customers=customers,
        cards=[c for c in seed.cards if c.customer_ref != "CUS-0005"],
        transactions=[
            *[t for t in seed.transactions if t.customer_ref != "CUS-0005" or t.card_ref is None],
            _tx(900_001, 5, type="mass_payout", requested_at="-2d 10:00"),
            _tx(900_002, 5, type="withdrawal_sepa", amount="600.00", requested_at="-2d 10:00"),
            _tx(900_003, 5, type="card_payment", card_ref="CRD-0006",
                merchant_category="software", requested_at="-2d 10:00"),
            _tx(900_004, 6, type="withdrawal_sepa", status="failed", error_code="ERR-5010",
                requested_at="-2d 10:00"),
            _tx(900_005, 6, requested_at="-1000d 10:00"),
        ],
    )  # fmt: skip
    errors = validate_seed(broken)
    assert "TX-900001: mass payouts are Business only" in errors
    assert "TX-900002: above the daily withdrawal limit of the tier" in errors
    assert "TX-900003: Starter customers have no card" in errors
    assert "TX-900004: ERR-5010 on a customer who is not restricted" in errors
    assert "TX-900005: requested before the account was opened" in errors


def test_transaction_count_per_customer() -> None:
    seed = _valid_seed()
    broken = _replace(
        seed, transactions=[t for t in seed.transactions if t.customer_ref != "CUS-0001"]
    )
    assert "CUS-0001: 0 transactions, expected 5-40" in validate_seed(broken)


def test_every_intent_needs_three_customers() -> None:
    seed = _valid_seed()
    customers = [
        _customer(i, support_scenarios=["fees_fx"]) if c.support_scenarios == [Intent.OTHER] else c
        for i, c in enumerate(seed.customers, start=1)
    ]
    errors = validate_seed(_replace(seed, customers=customers))
    assert "intent other: 0 customers, expected at least 3" in errors


@pytest.mark.parametrize(
    ("intent", "keep"),
    [
        (Intent.PAYMENT_PENDING, lambda t: t.type != "incoming_ach"),
        (Intent.WITHDRAWAL_ISSUE, lambda t: t.type not in {"withdrawal_swift", "withdrawal_sepa"}),
        (Intent.CARD_DECLINED, lambda t: t.status != "failed" or t.card_ref is None),
        (Intent.FEES_FX, lambda t: t.type != "conversion" and t.fee == 0),
        (Intent.LIMITS, lambda t: t.error_code != "ERR-2001"),
        (Intent.DISPUTE_CHARGEBACK, lambda t: t.status != "completed" or t.card_ref is None),
    ],
)
def test_a_scenario_needs_data_that_fits_it(intent: Intent, keep: Any) -> None:  # noqa: ANN401
    seed = _valid_seed()
    ref = next(c.external_ref for c in seed.customers if c.support_scenarios == [intent])
    filler = [_tx(800_000 + n, int(ref[-4:]), requested_at="-50d 10:00") for n in range(6)]
    txs = [t for t in seed.transactions if t.customer_ref != ref or keep(t)]
    errors = validate_seed(_replace(seed, transactions=[*txs, *filler]))
    assert f"{ref}: data does not fit the {intent} scenario" in errors


def test_card_and_kyc_scenarios_need_a_card_and_an_upgrade() -> None:
    seed = _valid_seed()
    lost = next(c for c in seed.customers if c.support_scenarios == [Intent.CARD_LOST_STOLEN])
    kyc = next(c for c in seed.customers if c.support_scenarios == [Intent.KYC_VERIFICATION])
    broken = _replace(
        seed,
        cards=[
            _card(int(c.customer_ref[-4:]), kind="virtual", delivery_status=None,
                  delivery_method=None) if c.customer_ref == lost.external_ref else c
            for c in seed.cards
        ],
        customers=[
            _customer(int(c.external_ref[-4:]), upgrade=None) if c == kyc else c
            for c in seed.customers
        ],
    )  # fmt: skip
    errors = validate_seed(broken)
    assert f"{lost.external_ref}: data does not fit the card_lost_stolen scenario" in errors
    assert f"{kyc.external_ref}: data does not fit the kyc_verification scenario" in errors


def test_limits_can_be_nearly_used_by_todays_withdrawals() -> None:
    seed = _valid_seed()
    ref = next(c.external_ref for c in seed.customers if c.support_scenarios == [Intent.LIMITS])
    number = int(ref[-4:])
    txs = [t for t in seed.transactions if t.customer_ref != ref or t.error_code != "ERR-2001"]
    today = [
        _tx(700_001, number, type="withdrawal_sepa", amount="5000.00", requested_at="0d 08:00"),
        _tx(700_002, number, type="withdrawal_sepa", amount="3000.00", requested_at="0d 10:00",
            status="pending"),
    ]  # fmt: skip
    assert validate_seed(_replace(seed, transactions=[*txs, *today])) == []
    yesterday = [t.model_copy(update={"requested_at": "-1d 08:00"}) for t in today]
    errors = validate_seed(_replace(seed, transactions=[*txs, *yesterday]))
    assert f"{ref}: data does not fit the limits scenario" in errors


def test_tiers_are_ordered_for_upgrades() -> None:
    assert list(CustomerTier) == [
        CustomerTier.STARTER,
        CustomerTier.VERIFIED,
        CustomerTier.BUSINESS,
    ]
