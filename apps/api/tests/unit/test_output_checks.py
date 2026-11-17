"""Output checks of the verify step (G7, P5-04): each code check fails and passes."""

from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hub.domain.kb import Visibility
from hub.guardrails import output_checks as oc
from hub.guardrails.output_checks import (
    DraftToCheck,
    InternalText,
    Violation,
    check_draft,
    forbidden_promises,
    looks_english,
    operator_text_violations,
)
from hub.kb.loader import read_kb

from ._llm import ROOT

FILES = read_kb(ROOT / "data" / "kb")
EXTERNAL = [c.text for f in FILES if f.visibility == Visibility.EXTERNAL for c in f.chunks]
INTERNAL = [c.text for f in FILES if f.visibility == Visibility.INTERNAL for c in f.chunks]
GOOD = (
    "A SEPA withdrawal arrives in 1 business day if you request it before 15:00 CET. [1] "
    "You can check the status of the withdrawal in the app. [2]"
)
# A public test card number (Visa test PAN), never a real one.
TEST_PAN = "4111 1111 1111 1111"


def _draft(
    answer: str = GOOD,
    *,
    can_answer: bool = True,
    citations: Sequence[tuple[int, str]] = ((1, "withdrawal-times#0"), (2, "withdrawal-times#3")),
    evidence: Sequence[str] = ("withdrawal-times#0", "withdrawal-times#3"),
) -> DraftToCheck:
    return DraftToCheck(
        can_answer=can_answer,
        answer=answer,
        citations=list(citations),
        evidence_ids=frozenset(evidence),
    )


def test_a_good_draft_passes_every_check() -> None:
    assert check_draft(_draft(), internal=InternalText.build(INTERNAL, EXTERNAL)) == []


# --- 1. can answer, not empty -----------------------------------------------------------------


def test_a_draft_that_cannot_answer_fails_and_stops_the_checks() -> None:
    assert check_draft(_draft("", can_answer=False, citations=())) == [Violation.CANNOT_ANSWER]


def test_an_empty_answer_fails() -> None:
    assert check_draft(_draft("  \n ", citations=())) == [Violation.EMPTY_ANSWER]


# --- 2. markers and citations -----------------------------------------------------------------


def test_a_marker_without_a_citation_fails() -> None:
    found = check_draft(_draft(GOOD + " It is free. [3]"))
    assert found == [Violation.MARKER_WITHOUT_CITATION]


def test_a_citation_of_a_source_the_writer_was_not_given_fails() -> None:
    found = check_draft(_draft(citations=((1, "withdrawal-times#0"), (2, "made-up#9"))))
    assert found == [Violation.UNKNOWN_SOURCE]


# --- 3. at least one citation, facts marked ---------------------------------------------------


def test_an_answer_without_any_citation_fails() -> None:
    found = check_draft(_draft("You can check the status in the app.", citations=()))
    assert found == [Violation.NO_CITATION]


@pytest.mark.parametrize(
    "paragraph",
    [
        "The fee is €1.50.",  # a fee
        "It arrives by 15:00 CET.",  # a time
        "Expect it on 12 November.",  # a date
        "It costs a few euros, about $2.",  # a currency sign and a number
    ],
)
def test_a_paragraph_with_a_number_date_fee_or_time_needs_a_marker(paragraph: str) -> None:
    found = check_draft(_draft(f"{GOOD}\n\n{paragraph}"))
    assert found == [Violation.UNMARKED_FACT]
    assert check_draft(_draft(f"{GOOD}\n\n{paragraph} [1]")) == []


def test_a_paragraph_without_a_number_needs_no_marker() -> None:
    assert check_draft(_draft(f"{GOOD}\n\nWe are sorry for the wait.")) == []


def test_the_marker_number_itself_is_not_a_fact() -> None:
    assert oc.paragraphs("a\n\n \n\nb [1]") == ["a", "b [1]"]
    assert check_draft(_draft("Check the app. [1]", citations=((1, "withdrawal-times#0"),))) == []


# --- 4. card data, placeholders, internal text -------------------------------------------------


@pytest.mark.parametrize(
    "leak",
    [
        f"Your card {TEST_PAN} is fine.",
        "Your card [CARD ****1111] is fine.",
        "Your code [REDACTED] is fine.",
        "Your CVV 123 is fine.",
    ],
)
def test_card_data_fails(leak: str) -> None:
    found = check_draft(_draft(f"{GOOD}\n\n{leak} [1]"))
    assert Violation.CARD_DATA in found


def test_a_failed_mask_counts_as_card_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.guardrails.masking import MaskResult

    monkeypatch.setattr(oc, "mask", lambda text: MaskResult(text="", counts={}, failed=True))
    assert oc.has_card_data("nothing")


def test_an_amount_or_an_iban_is_not_card_data() -> None:
    assert not oc.has_card_data("You sent €1,250.00 to [IBAN DE89 …3000] on 03/11.")


@pytest.mark.parametrize(
    "placeholder", ["<EMAIL_2>", "<PHONE_1>", "{{customer_name}}", "[customer name]", "[Amount]"]
)
def test_a_placeholder_left_in_the_text_fails(placeholder: str) -> None:
    found = check_draft(
        _draft(f"Hi {placeholder}, check the app. [1]", citations=((1, "a"),), evidence=("a",))
    )
    assert found == [Violation.PLACEHOLDER]


def test_text_copied_from_an_internal_chunk_fails() -> None:
    internal = InternalText.build(INTERNAL, EXTERNAL)
    copied = " ".join(oc.words(INTERNAL[0])[:20])
    found = check_draft(_draft(f"{GOOD}\n\n{copied}. [1]"), internal=internal)
    assert Violation.INTERNAL_TEXT in found


def test_no_external_chunk_counts_as_internal_text() -> None:
    internal = InternalText.build(INTERNAL, EXTERNAL)
    assert internal.hashes
    assert not [text for text in EXTERNAL if internal.found_in(text)]


def test_wording_shared_with_an_external_chunk_is_not_internal() -> None:
    shared = "one two three four five six seven eight nine"
    assert InternalText.build([shared]).found_in(shared)
    assert not InternalText.build([shared], [shared]).found_in(shared)
    assert not InternalText.build(["too short to count"]).found_in("too short to count")


# --- 5. forbidden promises --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("We will issue you a full refund today.", "refund"),
        ("I've refunded your payment.", "refund"),
        ("Your refund has been approved.", "refund"),
        ("We will increase your daily card limit.", "limit_increase"),
        ("Your withdrawal limit has been raised.", "limit_increase"),
        ("We\u2019ll unfreeze your account now.", "unfreeze"),
        ("Your card has been unblocked.", "unfreeze"),
        ("The restriction has been lifted.", "unfreeze"),
        ("Your account is restricted because of a compliance review.", "restriction_reason"),
        ("The reason for the restriction is unusual activity.", "restriction_reason"),
        ("You should sue the merchant.", "legal_advice"),
        ("You're legally entitled to the money.", "legal_advice"),
        ("You do not need to pay income tax on it.", "legal_advice"),
    ],
)
def test_a_forbidden_promise_fails(text: str, name: str) -> None:
    assert forbidden_promises(text) == [name]
    found = check_draft(_draft(f"{GOOD}\n\n{text} [1]"))
    assert found == [f"{Violation.FORBIDDEN_PROMISE}:{name}"]


@pytest.mark.parametrize(
    "text",
    [
        "You can ask for a refund from the merchant.",
        "You can ask us to raise your limit in the app.",
        "We cannot give legal or tax advice.",
        "When a payment fails, the money goes back to your balance.",
    ],
)
def test_a_process_description_is_not_a_promise(text: str) -> None:
    assert forbidden_promises(text) == []


def test_no_external_chunk_makes_a_forbidden_promise() -> None:
    assert [text for text in EXTERNAL if forbidden_promises(text)] == []


def test_the_promise_patterns_can_be_passed_in() -> None:
    assert forbidden_promises("free coffee", {"coffee": (r"\bcoffee\b",)}) == ["coffee"]


# --- 6. length and language -------------------------------------------------------------------


def test_an_answer_over_1200_characters_fails() -> None:
    sentence = "You can check the status of the withdrawal in the app. [1] "
    long = sentence * (oc.MAX_LENGTH // len(sentence) + 1)
    assert check_draft(_draft(long)) == [Violation.TOO_LONG]
    assert check_draft(_draft(sentence * (oc.MAX_LENGTH // len(sentence)))) == []


@pytest.mark.parametrize(
    "text",
    [
        "Ihre SEPA-Auszahlung kommt in der Regel an einem Werktag an, bitte prüfen Sie die App.",
        "Tu retirada SEPA llega en un día hábil, por favor revisa la app y espera un poco. [1]",
        "Votre retrait SEPA arrive en un jour ouvré, merci de vérifier dans l'app pour le suivi.",
        "Ваш вывод средств поступит в течение одного рабочего дня. [1]",
    ],
)
def test_an_answer_in_another_language_fails(text: str) -> None:
    assert check_draft(_draft(text, citations=((1, "a"),), evidence=("a",))) == [
        Violation.NOT_ENGLISH
    ]


def test_english_short_texts_and_fee_tables_pass_the_language_check() -> None:
    assert looks_english(GOOD)
    assert looks_english("OK [1]")
    assert looks_english("€1.50 [1]")
    assert looks_english("Card fee per year €0; per ATM €2; per FX €0.50; per SWIFT €15.")
    assert all(looks_english(text) for text in EXTERNAL)


# --- operator text ----------------------------------------------------------------------------


def test_operator_text_gets_the_card_check_only() -> None:
    assert operator_text_violations(f"Card {TEST_PAN}") == [Violation.CARD_DATA]
    assert operator_text_violations("We will refund you in German: Rückerstattung.") == []


@given(st.text(max_size=300))
def test_the_checks_never_raise_on_any_text(text: str) -> None:
    check_draft(_draft(text, citations=((1, "a"),), evidence=("a",)), internal=InternalText())
