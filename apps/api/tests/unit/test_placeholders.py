"""PII placeholders (G3, security §3; ADR-0017 for IBANs)."""

import pytest

from hub.guardrails.masking import mask
from hub.guardrails.placeholders import PiiReplacement, replace_pii, restore_pii


def round_trip(text: str) -> PiiReplacement:
    result = replace_pii(text)
    assert restore_pii(result.text, result.mapping) == text
    return result


def test_email_phone_and_iban_are_replaced_and_restored() -> None:
    text = "Mail anna.k+tb@example.co.uk, call +49 30 1234 5678. IBAN [IBAN DE89 \u20263000]."
    result = round_trip(text)
    assert result.text == "Mail <EMAIL_1>, call <PHONE_1>. IBAN <IBAN_1>."
    assert result.mapping == {
        "<EMAIL_1>": "anna.k+tb@example.co.uk",
        "<PHONE_1>": "+49 30 1234 5678",
        "<IBAN_1>": "[IBAN DE89 \u20263000]",
    }


def test_a_raw_iban_masked_by_g1_restores_to_the_masked_form_never_the_full_iban() -> None:
    masked = mask("Please send it to DE89 3704 0044 0532 0130 00 today.").text
    result = round_trip(masked)
    assert result.text == "Please send it to <IBAN_1> today."
    restored = restore_pii(result.text, result.mapping)
    assert "[IBAN DE89 \u20263000]" in restored
    assert "3704" not in restored


def test_the_masked_iban_without_last_characters_is_replaced_too() -> None:
    result = round_trip("To [IBAN AT77 \u2026] [CARD ****] please")
    assert result.text == "To <IBAN_1> [CARD ****] please"


def test_the_same_value_gets_the_same_placeholder_and_numbers_count_per_type() -> None:
    result = round_trip("a@b.io, c@d.io, a@b.io, 0151 23456789, 0151 23456789")
    assert result.text == "<EMAIL_1>, <EMAIL_2>, <EMAIL_1>, <PHONE_1>, <PHONE_1>"
    assert len(result.mapping) == 3


@pytest.mark.parametrize(
    "phone",
    [
        "+49 30 1234 5678",
        "+491511234567",
        "+44 20 7946 0958",
        "+1 (555) 123-4567",
        "0049 151 2345 6789",
        "+421 2 1234 5678",
        "0151 23456789",
        "030 12345678",
        "(030) 1234567",
        "020 7946 0958",
        "(555) 123-4567",
        "555-123-4567",
    ],
)
def test_phone_formats_are_replaced(phone: str) -> None:
    assert round_trip(f"Call me on {phone} please.").text == "Call me on <PHONE_1> please."


@pytest.mark.parametrize(
    "text",
    [
        "On 01.12.2026 at 10:00 I paid 1,200.50 EUR.",
        "Dates 01/12/2026 10:00 and 01-12-2026 10:00.",
        "ISO date 2026-11-16, time 08:00-20:00.",
        "Error ERR-3007 and ERR-2001.",
        "Card [CARD ****1111] and [CARD ****].",
        "Amounts 0.50, 1 200 000 EUR, 12345678.",
        "Amount 1 000 000 000 and 5-0151-2345-6789.",
        "Ticket 0b74d664-3640-4097-8741-e5b0ef08c44e.",
        "Short numbers 0123 and 01234567.",
        "Not an email: user@localhost, @handle, a@b.",
        "Something like +1 or 00 49?",
        "IBAN-like text [IBAN DE89 3000] without the ellipsis.",
    ],
)
def test_text_without_pii_stays_as_it_is(text: str) -> None:
    result = replace_pii(text)
    assert result.text == text
    assert result.mapping == {}


def test_a_placeholder_the_customer_typed_is_escaped_and_never_restored() -> None:
    result = replace_pii("I typed <EMAIL_1> and <PHONE_2>; mail me at a@b.io")
    assert result.text == "I typed &lt;EMAIL_1&gt; and &lt;PHONE_2&gt;; mail me at <EMAIL_1>"
    assert result.mapping == {"<EMAIL_1>": "a@b.io"}
    restored = restore_pii(result.text, result.mapping)
    assert restored == "I typed &lt;EMAIL_1&gt; and &lt;PHONE_2&gt;; mail me at a@b.io"


def test_a_run_mapping_is_continued_across_texts() -> None:
    first = replace_pii("Mail a@b.io, call 0151 23456789")
    second = replace_pii("Old a@b.io, new c@d.io, and [IBAN DE89 \u20263000]", first.mapping)
    assert second.text == "Old <EMAIL_1>, new <EMAIL_2>, and <IBAN_1>"
    assert second.mapping == {
        "<EMAIL_1>": "a@b.io",
        "<PHONE_1>": "0151 23456789",
        "<EMAIL_2>": "c@d.io",
        "<IBAN_1>": "[IBAN DE89 \u20263000]",
    }
    assert first.mapping == {"<EMAIL_1>": "a@b.io", "<PHONE_1>": "0151 23456789"}
    both = f"{first.text} | {second.text}"
    assert restore_pii(both, second.mapping) == (
        "Mail a@b.io, call 0151 23456789 | Old a@b.io, new c@d.io, and [IBAN DE89 \u20263000]"
    )


def test_numbering_continues_after_the_highest_number_in_the_mapping() -> None:
    result = replace_pii("x@y.io", {"<EMAIL_7>": "a@b.io"})
    assert result.text == "<EMAIL_8>"


def test_a_mapping_key_that_is_not_a_placeholder_is_refused_without_showing_values() -> None:
    with pytest.raises(ValueError, match="not a placeholder") as error:
        replace_pii("x", {"EMAIL_1": "a@b.io"})
    assert "a@b.io" not in str(error.value)


def test_restore_leaves_unknown_placeholders_and_never_expands_a_value() -> None:
    mapping = {"<EMAIL_1>": "<PHONE_1>"}
    assert restore_pii("<EMAIL_1> <PHONE_1> <IBAN_9>", mapping) == "<PHONE_1> <PHONE_1> <IBAN_9>"


def test_an_email_wins_over_a_phone_inside_it() -> None:
    result = round_trip("Write to 015123456789@example.com")
    assert result.text == "Write to <EMAIL_1>"


def test_the_mapping_is_not_in_the_repr() -> None:
    result = replace_pii("a@b.io")
    assert "a@b.io" not in repr(result)
    assert "a@b.io" not in str(result)


def test_empty_text() -> None:
    assert replace_pii("") == PiiReplacement(text="", mapping={})
