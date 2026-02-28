# Phase 02 Retrospective: Governance Reframe

**Phase Goal:** Add structured provenance output to agent, build governance evaluation scorers, reframe project narrative around auditability
**Phase Status:** Complete
**Date Completed:** 2026-02-27 (backfilled)

## What Went Well

**Provenance output as a system prompt pattern:** Adding the structured Provenance section (Path, Sources, Grounding) to the agent's system prompt was surprisingly effective. The LLM consistently produces the requested format without fine-tuning or output parsing — just prompt engineering. This is a repeatable pattern for any structured-output requirement.

**Governance scorers as MLflow custom scorers:** The `@scorer` decorator made it straightforward to build domain-specific governance metrics (hallucination_check, citation_completeness, provenance_chain). Each scorer is self-contained and composable — they slot into `mlflow.genai.evaluate()` alongside built-in scorers with no friction.

**Narrative pivot landed cleanly:** Reframing from "better answers" to "auditable answers" clarified every downstream decision. The PRFAQ, README, and evaluation notebook now tell a coherent governance story. This is a one-time cost that pays off in every future conversation about the project.

## What Didn't Go As Expected

**Reproducibility test architecture:** The reproducibility validation (same query → same path) can't be expressed as a standard MLflow scorer because it requires multiple runs of the same query. Implemented as a standalone utility (`run_reproducibility_test()`) rather than part of `GOVERNANCE_SCORERS`. Not a problem — just an architectural nuance to document.

**No formal Drucker artifacts created during this phase:** The governance reframe was executed rapidly as a narrative and code pivot. No CONTRIBUTION-GATE, VERIFICATION, or EVAL-REPORT were written at the time. This retrospective is a backfill.

## What Would I Tell My Past Self

- The provenance section format (Path, Sources, Grounding) works — commit to it early and build the parsers/scorers around it
- Governance scorers are cheap to write but expensive to skip — build them alongside the feature, not after
- Write the retrospective immediately after the phase, not as a backfill weeks later

## Quantified Learning

| Metric | Target | Actual | Learning |
|--------|--------|--------|----------|
| Requirements delivered | 5 (R1.10-R1.14) | 5 delivered | Clean scope, no creep |
| Governance scorers | 4 | 3 scorers + 1 utility | Reproducibility doesn't fit scorer interface — utility is fine |
| Unplanned work | 0 | 1 (PRFAQ rewrite) | Narrative reframe touched more docs than expected |

## Fidelity Status

| Component | Start Level | End Level | Promoted? | Notes |
|-----------|------------|-----------|-----------|-------|
| Provenance output (system prompt) | Level 1 | Level 2 | Yes | Proven in spike, extracted to src/agent/agent.py |
| Governance scorers | Level 1 | Level 2 | Yes | Proven in spike, extracted to src/evaluation/evaluation.py |
| Reproducibility test | Level 1 | Level 2 | Yes | Utility in evaluation.py, used by 05_Evaluation.py |
| PRFAQ / narrative | N/A | N/A | N/A | Documentation artifact, not code |

Spike notebooks from this phase: None remaining (all promoted to src/).

## Key Insights for Phase 03

**Insight 1:** The governance story is the product differentiator. Phase 03's demo app should lead with "every answer is auditable" — not "better answers."

**Insight 2:** Mock mode will be essential. The agent endpoint may not be live when the demo app ships. Design the demo to work without a live backend from day one.

## Specific Adjustments for Phase 03

- Change: Lead demo narrative with governance, not quality. Rationale: This is what enterprises care about. Expected impact: Clearer value proposition in the demo.
- Change: Build mock mode as a first-class feature. Rationale: Phase 02 showed backend readiness is an external dependency. Expected impact: Demo ships on time regardless of pipeline status.
