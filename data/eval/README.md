# Evaluation data

Files in this folder:

- `tickets.dev.jsonl`: exactly 20 evaluation tickets (eval plan §3). One JSON object per line.
- `attacks.jsonl`: 16 attacks (written in P8-01, reviewed by the owner).

All data is **synthetic and about a fictional company** (Tallybridge). The customers are the invented seed customers in `data/seed/`. Names, amounts and card numbers are not real. Card numbers are public test numbers only.

## How the tickets are made

1. The `content-author` subagent writes the tickets from `docs/tallybridge-world.md`, the knowledge base in `data/kb/` and the seed data in `data/seed/`. No paid model call is used.
2. Every ticket belongs to a seed customer. The ticket must agree with that customer's seed data: transactions, withdrawals, cards, balances, error codes. Dates are relative to the anchor date ("yesterday" means an offset of one day in the seed).
3. Labels (intent, flags, gold documents, key facts, escalation, expected reason) are proposals. Each key fact must be stated in the world file, in an external article or in the seed data. Key facts never contain internal content or a restriction reason. A gold document is always an external article or `error-codes#<code>`.
4. The `reviewer` subagent checks every label against the world file and the knowledge base in the pull request. There is no human gate for tickets (H4 was removed by ADR-0018), so a wrong label is possible. The owner reviews the attacks (H6), not the tickets.

## How `make data-check` validates

`make data-check` checks each line against the schema in `hub_evals/schemas.py` (unknown fields are refused). It then checks that every `customer_ref` is a seed customer, that every gold document is an external article or a row of `error_codes.csv`, and that any card number is a public test number. Only when every line is valid does it check the counts: exactly 20 tickets, every intent at least once, at least 6 `hard` tickets, at least 3 `must_handoff` tickets.

## No test set

There is no test file and no dev/test split (ADR-0018). The 20 tickets are both the tuning set and the milestone set, so the numbers are optimistic. They are a demo check, not a proof. See `docs/eval-plan.md`.

## When a ticket fails in a run

Check "data label wrong" first (eval plan §7). A label error is fixed in the dataset and written to `data/eval/CHANGELOG.md` with a short note.
