"""The core mock's seed schemas agree with the Hub's (`hub.domain.seed`, P2-05).

`support_tools` must not import `hub` (design §8), so it keeps its own copy of the shapes. This
test may import both: it is not part of either package.
"""

from datetime import date
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel

from hub.domain import seed as hub_seed
from hub.domain.enums import CustomerTier
from support_tools import seed as core_seed
from support_tools.server import DEFAULT_SEED_DIR

MODEL_PAIRS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (hub_seed.Customer, core_seed.SeedCustomer),
    (hub_seed.Upgrade, core_seed.SeedUpgrade),
    (hub_seed.Card, core_seed.SeedCard),
    (hub_seed.Transaction, core_seed.SeedTransaction),
    (hub_seed.StatusChange, core_seed.SeedStatusChange),
]

ENUM_PAIRS: list[tuple[type[StrEnum], type[StrEnum]]] = [
    (CustomerTier, core_seed.Tier),
    (hub_seed.Currency, core_seed.Currency),
    (hub_seed.UpgradeStatus, core_seed.UpgradeStatus),
    (hub_seed.CardKind, core_seed.CardKind),
    (hub_seed.CardStatus, core_seed.CardStatus),
    (hub_seed.DeliveryStatus, core_seed.DeliveryStatus),
    (hub_seed.TransactionType, core_seed.TransactionType),
    (hub_seed.TransactionStatus, core_seed.TransactionStatus),
]


@pytest.mark.parametrize(("hub_model", "core_model"), MODEL_PAIRS)
def test_the_models_have_the_same_fields(
    hub_model: type[BaseModel], core_model: type[BaseModel]
) -> None:
    assert set(core_model.model_fields) == set(hub_model.model_fields)
    for name, field in core_model.model_fields.items():
        assert field.is_required() == hub_model.model_fields[name].is_required(), name
    assert core_model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(("hub_enum", "core_enum"), ENUM_PAIRS)
def test_the_enums_have_the_same_values(hub_enum: type[StrEnum], core_enum: type[StrEnum]) -> None:
    assert {member.value for member in core_enum} == {member.value for member in hub_enum}


def test_the_reference_formats_agree() -> None:
    for name in ("customer_ref", "card_ref"):
        assert _pattern(core_seed.SeedCard, name) == _pattern(hub_seed.Card, name)
    assert _pattern(core_seed.SeedTransaction, "reference") == _pattern(
        hub_seed.Transaction, "reference"
    )
    assert _pattern(hub_seed.Customer, "external_ref") == core_seed.CUSTOMER_REF_PATTERN
    assert _pattern(hub_seed.Transaction, "reference") == core_seed.TRANSACTION_REF_PATTERN


def _pattern(model: type[BaseModel], field: str) -> str:
    for item in model.model_fields[field].metadata:
        pattern = getattr(item, "pattern", None)
        if pattern is not None:
            return str(pattern)
    raise AssertionError(f"{model.__name__}.{field} has no pattern")


def test_both_read_the_same_seed_and_resolve_the_same_dates() -> None:
    files = {
        name: (Path(DEFAULT_SEED_DIR) / name).read_bytes()
        for name in ("customers.json", "cards.json", "transactions.json", "operators.json")
    }
    hub = hub_seed.parse_seed(files)
    core = core_seed.load_seed(DEFAULT_SEED_DIR)
    assert [c.external_ref for c in core.customers] == [c.external_ref for c in hub.customers]
    assert [c.card_ref for c in core.cards] == [c.card_ref for c in hub.cards]
    assert [t.reference for t in core.transactions] == [t.reference for t in hub.transactions]
    anchor = date(2026, 11, 12)
    for tx in hub.transactions:
        assert core_seed.resolve_offset(tx.requested_at, anchor) == hub_seed.resolve_offset(
            tx.requested_at, anchor
        )
