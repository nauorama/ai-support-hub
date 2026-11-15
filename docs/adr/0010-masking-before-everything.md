# ADR-0010: Mask card data before storage and before any model call

- Status: Superseded in part by ADR-0017 (IBAN rule)
- Date: 2026-11-12

## Context
Customers paste card numbers into chat. Card data must not be stored or sent to third parties (FR-3, NFR-6).

## Decision
The API masks card numbers (pattern + Luhn check), CVV, PIN and 2FA codes as the first step, before the message is saved or logged. Only the masked text exists after that. The LLM gateway scans every outgoing request again and blocks the call on a hit. Logs and traces pass through the same function. Email, phone and IBAN stay in the database but are replaced with placeholders in model input.

## Alternatives considered
- **Mask only before the model call, store the raw text.** Then the database and backups hold card data.
- **Microsoft Presidio for all PII.** Heavier (NLP models); regex + Luhn covers the main risk exactly. Presidio can be added if tests show other PII types passing.
- **Trust the provider's zero-retention setting.** Does not cover our own storage and logs.

## Consequences
Operators never see the full number either (they do not need it). False positives are possible (a long reference number that passes Luhn) — accepted; the last 4 digits stay visible.
