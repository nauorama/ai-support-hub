## Task
<!-- Task id and link, e.g. P2-03 -->

## What changed
<!-- Short list. Why, when it is not obvious. -->

## Done when — evidence
<!-- Copy the "Done when" lines of the task and mark each with the proof (test name, command output, number). -->
- [ ]

## Verification output
```
<!-- paste: make lint test (and test-int when relevant; eval-smoke only when the owner asked for it) -->
```

## Evaluation (only for changes to prompts, models, retrieval, graph, guardrails)
<!-- Only when the owner asked for a paid run (ADR-0018): metrics table before / after from make eval-smoke or make eval-report. -->

## Security checklist (only for guardrails, tools/permissions, auth, policy.yaml, logging)
- [ ] No card data, secrets or raw ticket text in logs, traces, errors or fixtures
- [ ] New model calls go through the gateway; new tools go through the permission check
- [ ] New endpoints declare a role and have an authorisation test
- [ ] Attack suite still passes (`make eval-attacks`)
- [ ] Threat model table updated if a threat or control changed

## Documents updated
<!-- Which docs changed, or "none needed". New ADR? -->

## Unsure about
<!-- Anything the reviewer should look at closely. -->
