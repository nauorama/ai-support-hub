"""The real seed files in `data/seed/`: valid, repeatable dates, and correct around the SEPA
cut-off (15:00 CET) in winter and in summer."""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hub.domain.enums import Intent
from hub.domain.seed import (
    MIN_CUSTOMERS_PER_INTENT,
    SeedData,
    TransactionStatus,
    TransactionType,
    resolve_offset,
    validate_seed,
)
from hub.seed import read_seed

ROOT = Path(__file__).resolve().parents[4]
SEED_DIR = ROOT / "data" / "seed"
CET = ZoneInfo("Europe/Bratislava")  # the support timezone of docs/ai-pipeline.md §6
WINTER = date(2026, 1, 14)  # CET, UTC+1
SUMMER = date(2026, 7, 15)  # CEST, UTC+2
SEPA_CUTOFF_HOUR = 15
# A pending SEPA withdrawal requested yesterday after the cut-off (named in the content brief).
AFTER_CUTOFF_REFERENCE = "TX-000900"


def _seed() -> SeedData:
    return read_seed(SEED_DIR)


def _all_dates(seed: SeedData, anchor: date) -> list[datetime]:
    offsets: list[str] = [c.opened_at for c in seed.customers]
    offsets += [c.upgrade.submitted_at for c in seed.customers if c.upgrade is not None]
    offsets += [c.issued_at for c in seed.cards]
    for tx in seed.transactions:
        offsets += [tx.requested_at, *(change.at for change in tx.status_history)]
    return [resolve_offset(offset, anchor) for offset in offsets]


def test_the_seed_files_are_valid() -> None:
    assert validate_seed(_seed()) == []


def test_every_intent_has_at_least_three_customers() -> None:
    seed = _seed()
    for intent in Intent:
        customers = [c for c in seed.customers if intent in c.support_scenarios]
        assert len(customers) >= MIN_CUSTOMERS_PER_INTENT, intent


def test_two_loads_with_a_fixed_anchor_give_the_same_dates() -> None:
    anchor = date(2026, 11, 12)
    assert _all_dates(_seed(), anchor) == _all_dates(_seed(), anchor)


def test_the_anchor_moves_every_date_by_the_same_amount() -> None:
    seed = _seed()
    first = _all_dates(seed, date(2026, 11, 12))
    later = _all_dates(seed, date(2026, 11, 19))
    assert {b - a for a, b in zip(first, later, strict=True)} == {later[0] - first[0]}


def test_the_after_cutoff_sepa_withdrawal_is_after_15_cet_in_winter_and_summer() -> None:
    tx = next(t for t in _seed().transactions if t.reference == AFTER_CUTOFF_REFERENCE)
    assert tx.type is TransactionType.WITHDRAWAL_SEPA
    assert tx.status is TransactionStatus.PENDING
    for anchor in (WINTER, SUMMER):
        local = resolve_offset(tx.requested_at, anchor).astimezone(CET)
        assert local.date() == anchor - timedelta(days=1)
        assert local.hour >= SEPA_CUTOFF_HOUR, (anchor, local)


def test_no_seed_event_is_on_the_anchor_day_or_later() -> None:
    # Checked for the latest possible moment of the anchor day in UTC: still before it starts.
    seed = _seed()
    anchor = date(2026, 11, 12)
    start_of_anchor_day = datetime(2026, 11, 12, tzinfo=CET)
    assert all(moment < start_of_anchor_day for moment in _all_dates(seed, anchor))


def test_fixed_fees_on_usd_and_gbp_transactions_are_in_eur() -> None:
    fixed = {TransactionType.WITHDRAWAL_SWIFT, TransactionType.WITHDRAWAL_SEPA,
             TransactionType.ATM_WITHDRAWAL}  # fmt: skip
    for tx in _seed().transactions:
        if tx.type in fixed and tx.fee > 0:
            assert (tx.fee_currency or tx.currency) == "EUR", tx.reference


def test_every_sepa_withdrawal_is_on_the_same_side_of_the_cutoff_all_year() -> None:
    for tx in _seed().transactions:
        if tx.type is not TransactionType.WITHDRAWAL_SEPA:
            continue
        sides = {
            resolve_offset(tx.requested_at, anchor).astimezone(CET).hour >= SEPA_CUTOFF_HOUR
            for anchor in (WINTER, SUMMER)
        }
        assert len(sides) == 1, tx.reference


def test_no_card_number_beyond_the_last_four_digits() -> None:
    for card in _seed().cards:
        assert len(card.last4) == 4
