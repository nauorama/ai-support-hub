"""Account facts from the results of the core read tools (ai-pipeline §4, P7-04).

A tool result is read through a model that lists only the fields the writer may see (unknown
fields are ignored), so whatever else a result carries, e.g. a restriction reason, the
counterparty or the card reference, never becomes a fact. Every fact text is G1-masked. The
same texts are what the research model reads as the tool result, so the model and the writer
see exactly the same account data.

Pure: no database, no network.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from hub.domain.account import AccountFact, CustomerProfile
from hub.guardrails.masking import mask_text

# More facts add little for one answer and make the prompt long.
MAX_ACCOUNT_FACTS = 30


class _Read(BaseModel):
    # `ignore`: a field that is not listed here is dropped, never passed on.
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Upgrade(_Read):
    target_tier: str
    status: str
    submitted_at: datetime
    error_code: str | None = None


class _Verification(_Read):
    current_tier_checks: str
    upgrade: _Upgrade | None = None


class _Limit(_Read):
    name: str
    limit_eur: Decimal
    used: dict[str, Decimal]


class _Profile(_Read):
    tier: str
    country: str
    verification: _Verification
    restricted: bool
    limits: list[_Limit]
    as_of: date


class _Transaction(_Read):
    reference: str
    type: str
    amount: Decimal
    currency: str
    fee: Decimal
    fee_currency: str
    status: str
    error_code: str | None = None
    requested_at: datetime
    updated_at: datetime


class _TransactionList(_Read):
    transactions: list[_Transaction]


class _StatusChange(_Read):
    status: str
    at: datetime


class _TransactionDetail(_Transaction):
    merchant_category: str | None = None
    status_history: list[_StatusChange]
    expected_event: str | None = None
    expected_by: date | None = None
    overdue: bool


class _Card(_Read):
    kind: str
    last4: str
    status: str
    issued_at: datetime
    delivery_status: str | None = None
    delivery_method: str | None = None
    expected_delivery_by: date | None = None
    delivery_overdue: bool = False


class _CardList(_Read):
    cards: list[_Card]


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _money(amount: Decimal, currency: str) -> str:
    return f"{amount:.2f} {currency}"


def profile(result: dict[str, Any]) -> CustomerProfile | None:
    """The profile intake keeps; None when the result cannot be read."""
    try:
        read = _Profile.model_validate(result)
    except ValidationError:
        return None
    return CustomerProfile(tier=read.tier, country=read.country, restricted=read.restricted)


def _profile_texts(read: _Profile) -> list[str]:
    # The restricted flag is not a fact for the writer: a restricted customer is handed off
    # before research (row 1), and the writer never talks about a restriction.
    texts = [
        f"Account: tier {read.tier}, country {read.country}, "
        f"checks of the current tier {read.verification.current_tier_checks}."
    ]
    upgrade = read.verification.upgrade
    if upgrade is not None:
        code = f", error code {upgrade.error_code}" if upgrade.error_code else ""
        texts.append(
            f"Upgrade to tier {upgrade.target_tier}: status {upgrade.status}{code}, "
            f"submitted {_when(upgrade.submitted_at)}."
        )
    for limit in read.limits:
        used = ", ".join(
            _money(amount, currency) for currency, amount in sorted(limit.used.items())
        )
        texts.append(
            f"Limit {limit.name}: {_money(limit.limit_eur, 'EUR')} (EUR equivalent); "
            f"used: {used or 'nothing'} (as of {read.as_of.isoformat()})."
        )
    return texts


def _transaction_text(tx: _Transaction) -> str:
    code = f", error code {tx.error_code}" if tx.error_code else ""
    return (
        f"Transaction {tx.reference}: {tx.type}, {_money(tx.amount, tx.currency)}, "
        f"fee {_money(tx.fee, tx.fee_currency)}, status {tx.status}{code}, "
        f"requested {_when(tx.requested_at)}, last update {_when(tx.updated_at)}."
    )


def _detail_text(tx: _TransactionDetail) -> str:
    parts = [_transaction_text(tx)]
    if tx.merchant_category:
        parts.append(f"Merchant category: {tx.merchant_category}.")
    history = "; ".join(f"{step.status} {_when(step.at)}" for step in tx.status_history)
    if history:
        parts.append(f"Status history: {history}.")
    if tx.expected_event and tx.expected_by:
        overdue = " (overdue)" if tx.overdue else ""
        parts.append(f"Expected {tx.expected_event} by {tx.expected_by.isoformat()}{overdue}.")
    return " ".join(parts)


def _card_text(card: _Card) -> str:
    text = (
        f"Card ending {card.last4} ({card.kind}): status {card.status}, "
        f"issued {_when(card.issued_at)}"
    )
    if card.delivery_status:
        method = f" ({card.delivery_method})" if card.delivery_method else ""
        text += f"; delivery {card.delivery_status}{method}"
        if card.expected_delivery_by:
            text += f", expected by {card.expected_delivery_by.isoformat()}"
        if card.delivery_overdue:
            text += " (overdue)"
    return text + "."


def fact_texts(tool: str, result: dict[str, Any]) -> list[str] | None:
    """The fact texts of one result of a core read tool, G1-masked; None when the result cannot
    be read (it then gives no fact)."""
    try:
        match tool:
            case "core_get_customer":
                texts = _profile_texts(_Profile.model_validate(result))
            case "core_list_transactions":
                listed = _TransactionList.model_validate(result).transactions
                texts = [_transaction_text(tx) for tx in listed] or ["No transactions found."]
            case "core_get_transaction":
                texts = [_detail_text(_TransactionDetail.model_validate(result))]
            case "core_get_cards":
                cards = _CardList.model_validate(result).cards
                texts = [_card_text(card) for card in cards] or ["No cards."]
            case _:
                return None
    except ValidationError:
        return None
    return [mask_text(text) for text in texts]


def add_facts(
    facts: Sequence[AccountFact], tool: str, texts: Sequence[str]
) -> tuple[list[AccountFact], list[AccountFact]]:
    """The facts with the new texts added (`A<n>` in order, a text already there keeps its id,
    at most `MAX_ACCOUNT_FACTS`), and the facts the texts map to, for the tool result."""
    merged = list(facts)
    by_text = {fact.text: fact for fact in merged}
    these: list[AccountFact] = []
    for text in texts:
        fact = by_text.get(text)
        if fact is None:
            if len(merged) >= MAX_ACCOUNT_FACTS:
                break
            fact = AccountFact(fact_id=f"A{len(merged) + 1}", source_tool=tool, text=text)
            merged.append(fact)
            by_text[text] = fact
        these.append(fact)
    return merged, these


def render_facts(facts: Sequence[AccountFact]) -> str:
    """Facts as the writer and the verifier read them: one `Source id:` line per fact, like the
    knowledge-base sections."""
    return "\n\n".join(f"Source id: {fact.fact_id}\n{fact.text}" for fact in facts)
