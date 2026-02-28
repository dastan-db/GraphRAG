# D-003: Web Framework — Dash over Streamlit

**Phase:** 03-Interactive-Demo
**Date Decided:** [YYYY-MM-DD — backfill]
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

## Validation (Fill After Phase 03)
- [ ] Did Dash development match time estimate?
- [ ] Multi-page routing straightforward?
- [ ] UI acceptable to customers?
- [ ] Chat interface work as expected?
- [ ] Keep Dash for v2, or switch?

**Date Reviewed:**
**Outcome:**
**Learning:**
