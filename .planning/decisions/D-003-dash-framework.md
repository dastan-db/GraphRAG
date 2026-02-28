# D-003: Web Framework — Dash over Streamlit

**Phase:** 03-Interactive-Demo
**Date Decided:** 2026-02-27 (backfilled)
**Reversibility:** Two-way door (UI layer is decoupled from backend)
**Implementation Fidelity:** Level 2 (validated — deployed and working)

## Decision
Use Dash (Plotly) + dash_bootstrap_components for the interactive web app.

## Why This Over Alternatives
Dash gives us multi-page routing without leaving the Databricks ecosystem, better UI control than Streamlit for a 5-page app, and native Plotly visualization integration. Streamlit is faster for single-page prototypes but becomes painful for multi-page routing and custom layouts. We accept more boilerplate in exchange for architectural flexibility.

## Trade-offs Accepted
- More boilerplate than Streamlit
- Smaller community / fewer examples than Streamlit

## Platform Leverage Check
Dash deploys cleanly via Databricks Apps + Asset Bundles. This is a Databricks-native deployment pattern. Streamlit also deploys to Databricks, but Dash's multi-page routing is a better fit for the 5-page demo structure.

## Validation (Filled After Phase 03)
- [x] Did Dash development match time estimate? **Yes.** The 5-page app was built within the estimated ~1 week. dash_bootstrap_components (DARKLY theme) handled styling without custom CSS.
- [x] Multi-page routing straightforward? **Yes.** `dcc.Location` + callback-based routing works cleanly. Each page is a separate module in `src/app/pages/`.
- [x] UI acceptable to customers? **Yes.** The tell-show-tell narrative (problem → solution → architecture → live demo → business case) received positive feedback. DARKLY theme looks professional.
- [x] Chat interface work as expected? **Yes.** dbc.Input + callbacks handle message submission, chat history, and provenance panel display. Example question buttons lower the barrier to engagement.
- [x] Keep Dash for v2, or switch? **Keep Dash.** The multi-page architecture is working well. No reason to switch unless v2 requires real-time streaming UI (which would favor a different framework).

**Date Reviewed:** 2026-02-28
**Outcome:** Success
**Evidence:** App deployed and accessible at graphrag-demo-v2-dev-*.aws.databricksapps.com. E2E tests pass (Playwright). All 5 pages render correctly with navigation.
**Learning:** Dash + DBC is the right choice for multi-page demo apps on Databricks. Streamlit would have been faster for a single-page prototype but painful for the 5-page structure. The boilerplate trade-off is worth it for architectural flexibility.
