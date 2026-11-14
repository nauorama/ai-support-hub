# ADR-0017: A card number wins over an IBAN in masking

- Status: accepted
- Date: 2026-11-14
- Supersedes: part of [ADR-0010](0010-masking-before-everything.md) ("IBAN stay[s] in the database"), for IBANs whose digits read as a card number

## Context
ADR-0010 keeps IBANs in the database. G1 kept any text that looked like an IBAN (two capital letters, two check digits, mod-97 check). A card number typed after two capital letters passes that check about once in 97 tries and was then stored and sent to the model unmasked: Hypothesis found `YZ178162840989986` in the P3-01 review. G1 and the gateway's outbound scan (G2) use the same function, so both missed it.

## Decision
1. **Stricter IBAN check.** An IBAN must have a country code from the ISO 13616 registry and exactly that country's length. Source: SWIFT IBAN Registry release 101, via python-stdnum 2.2 `stdnum/iban.dat` (tag 2.2, 2026-01-04); only the country/length table is copied into `guardrails/masking.py`, python-stdnum is not a dependency.
2. **A card number wins.** A valid IBAN is still masked as a card when one of its digit runs, as a whole or without the two check digits it starts with, is a Luhn-valid 13–19 digit number.

## Alternatives considered
- **Only the stricter check.** Fixes `YZ…`, but `AT12 4111 1111 1111 1111` style text with a real country code still passes about once in 97 tries.
- **Mask an IBAN when any 13–19 digit part of it passes Luhn.** Masks most IBANs; ADR-0010 would be empty.

## Consequences
No card number is kept because it also looks like an IBAN. Some real IBANs are masked as `[CARD ****nnnn]`: those whose digits form a 13–19 digit run that passes Luhn (roughly 1 in 10 to 1 in 5; e.g. the registry examples for AT, DK and FI). Information is lost, nothing leaks; the operator asks the customer again. The table must be updated when the registry adds a country.
