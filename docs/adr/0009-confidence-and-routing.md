# ADR-0009: Routing by policy rules first, then a calibrated confidence model

- Status: Superseded in part by ADR-0018 (trained confidence model)
- Date: 2026-11-12

## Context
The system must decide when an answer can go out without a human (FR-9, NFR-4, NFR-5). Language models report confidence poorly, and a model that was tricked by an injection will also report high confidence.

## Decision
Two layers. (1) Deterministic policy rules from `config/policy.yaml` decide all cases that must go to a human. (2) For the rest, a logistic regression trained offline on dev-set features (retrieval scores, groundedness, citations, intent, …) gives a probability that the answer is correct. Weights live in `config/confidence.json`. The threshold is chosen from the risk–coverage curve so that the wrong auto-reply rate meets NFR-4, and can be changed by the support lead. Until the model is trained, auto-reply is off.

## Alternatives considered
- **Ask the model for a confidence number.** Poorly calibrated and open to manipulation.
- **Rules only.** Either too strict (low automation) or too loose.
- **A bigger classifier.** Not enough data (210 dev tickets); logistic regression is explainable and enough.

## Consequences
The automation level is a business setting with a measured risk. The model must be retrained when prompts, models or retrieval change (part of the eval report). Small data means wide error bars — the report shows them, the model is kept small (L2, few features, cross-validation), and offline numbers only open the next step of the staged rollout in the PRD §12.
