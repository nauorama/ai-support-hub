# ADR-0013: Prompts are versioned files in the repository

- Status: accepted
- Date: 2026-11-12

## Context
Prompts are the most changed part of the system and each change can alter quality. We need to know which prompt produced which answer (FR-16) and to review changes like code.

## Decision
Each prompt is a Markdown file `prompts/<node>/v<N>.md`. A changed prompt is a new file. `config/models.yaml` names the active version. The version is recorded with every model call and run. A prompt change needs an eval-smoke run in CI.

## Alternatives considered
- **Prompt management in the tracing tool.** Changes outside git and outside review; evaluation and production could drift.
- **Prompts as Python strings.** Hard to diff and to read.

## Consequences
Full history and easy rollback. More files over time — old versions that no report refers to can be removed.
