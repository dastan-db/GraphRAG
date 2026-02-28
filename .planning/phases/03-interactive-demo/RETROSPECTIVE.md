# Phase 03 Retrospective: Interactive Demo Web App

**Phase Goal:** Build and deploy a 5-page interactive demo showing GraphRAG capabilities
**Phase Status:** Complete
**Date Completed:** 2026-02-27

## What Went Well

**Dash + bootstrap_components iteration speed:** Built the entire 5-page app faster than expected. The component library handled layout and styling without custom CSS. Repeatable for future UI work.

**Mock mode as a design decision:** Decoupling the UI from a live agent endpoint meant we could ship the demo without waiting for the pipeline. This proved that frontend and backend can deploy independently — a key architectural learning.

**DABs deployment:** Databricks Asset Bundles handled deployment cleanly. The deploy-test-iterate cycle was fast.

## What Didn't Go As Expected

**Live agent endpoint incomplete:** The pipeline wasn't ready by end of Phase 03. Demo runs in mock mode. Root cause: pipeline execution was an external dependency we couldn't control within the phase. Resolution: deferred to Phase 04. Mock mode works well as a fallback pattern.

## What Would I Tell My Past Self

- Start with mock mode from Day 1 — don't block UI work on backend readiness
- The UI/backend decoupling isn't just a workaround, it's the right architecture
- Dash multi-page routing is straightforward; don't overthink it

## Quantified Learning

| Metric | Target | Actual | Learning |
|--------|--------|--------|----------|
| Requirements delivered | 7 (R1.15-R1.21) | 7 delivered | Full scope with no cuts |
| Timeline | 1 week | ~1 week | Dash + DBC was fast for multi-page apps |
| Unplanned work | 0 | 1 (mock backend) | Mock mode became a permanent feature, not throwaway |

## Fidelity Status

| Component | Current Level | Ready for Promotion? | Notes |
|-----------|--------------|---------------------|-------|
| Dash app (UI) | Level 2 | No (stable) | Working, deployed, maintained |
| Mock mode | Level 2 | No (stable) | Permanent fallback pattern |
| Agent integration | Not started | N/A | Phase 04 — start at Level 1 |

## Key Insights for Phase 04

**Insight 1:** UI and backend deploy independently. Phase 04 should connect the live agent to the existing mock interface — not rebuild the UI.

**Insight 2:** Mock mode is a permanent fallback, not a temporary workaround. The demo should always work even if the agent endpoint is down.

## Specific Adjustments for Phase 04

- Connect live agent endpoint to existing Dash app (don't rebuild UI)
- Keep mock mode as fallback for demos and testing
- Budget time for endpoint integration testing that was deferred from Phase 03
- Start agent work in spike notebooks (Level 1) before extracting to src/

## Recommendations for Future Phases
- Use the mock-first pattern for any phase with external dependencies
- DABs deployment is reliable — keep using it
