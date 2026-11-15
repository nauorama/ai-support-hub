# ADR-0017: IBANs are always partly masked

- Status: accepted
- Date: 2026-11-15
- Supersedes: part of [ADR-0010](0010-masking-before-everything.md) ("Email, phone and IBAN stay in the database"), for IBANs

## Context
ADR-0010 keeps IBANs in the database, and G1 kept any text that formed a valid IBAN. But a card number can form a valid IBAN by chance. Two capital letters, two check digits and a card pass the mod-97 check about once in 97 tries, and so do a card inside a longer IBAN or a card that starts in an IBAN's last group and goes on after it. Hypothesis found `YZ178162840989986` in the P3-01 review. The PR #14 review found more: `DE68 4111 1111 1111 1111 01`, `CZ3437828224631000500000`, `AT77 1234 5678 9012 4111 1111 1111 1111`. G1 and the gateway's outbound scan (G2) use the same function, so both missed them.

## Decision
1. **Recognise an IBAN strictly:** a country code from the ISO 13616 registry, that country's exact length, and the mod-97 check. Source of the table: SWIFT IBAN Registry release 101, via python-stdnum 2.2 `stdnum/iban.dat` (tag 2.2, commit 6070c60, 2026-01-04). Only the country/length table is copied into `guardrails/masking.py`; python-stdnum is not a dependency. Text that looks like an IBAN but is not valid is checked as a card number, like any other digits.
2. **Every valid IBAN is partly masked:** it keeps the country code, the check digits and the last 4 characters: `[IBAN DE89 …3000]`. Card digits inside an IBAN are then never stored, whatever their position.
3. **Digits joined to the IBAN's end:** when digits follow the IBAN with only a separator between them, its last 4 characters are hidden too (`[IBAN AT77 …]`), because they could be the first digits of a card that goes on after it. That following run is masked as a card when it holds a card itself, or when a Luhn-valid 13–19 digit number crosses the IBAN's end.
4. **Counts:** masking counts IBANs as their own type (`IBAN`), apart from card data. The count goes to `messages.masked_entities` and the `guardrail.masked` audit record. The customer's `masked` flag (the "We hid a card number" note) stays card-only.
5. **G2:** the gateway's outbound scan blocks a call that contains a raw IBAN, because G3 should have replaced it. The `llm_calls` row says `blocked: G2 IBAN`, and the `guardrail.blocked` audit record has `reason: "iban"`. Card data wins: a call with both is blocked as `blocked: G2` with `reason: "card_data"`.

## Alternatives considered
- **Only the stricter check** (registry country and length). This fixes `YZ…`, but a card inside a real-country IBAN still passes about once in 97 tries.
- **Hunt for card positions inside an IBAN** (the first version of this ADR): mask an IBAN as a card when its digits, or some part of them, pass Luhn. This was rejected. Each position checked masks about 1 in 10 more real IBANs as cards, and the review still found leaks at positions that were not checked (a card followed by more digits, a card that crosses the IBAN's end). Checking every position masks most IBANs and still loses the IBAN. Partly masking every IBAN removes the whole class of leaks with one simple rule.
- **Mask IBANs completely.** This is safe, but operators lose the country and the last characters, which help them tell a customer's accounts apart.

## Consequences
- No full IBAN is stored, logged or sent to a model. Operators see the country, the check digits and the last 4 characters, and ask the customer when they need more.
- G3 (P3-04) replaces the masked form with a placeholder and restores the masked form, never a full IBAN.
- An IBAN written with other separators (NBSP, dashes) or in lowercase is not recognised as an IBAN; its digits are checked as a card number only.
- The country table must be updated when the registry adds a country. An IBAN from a newer country is checked as digits only.
