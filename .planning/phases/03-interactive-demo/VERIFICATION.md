# Phase 03 Verification: Interactive Demo Web App

**Date:** 2026-02-27 (backfilled)
**Verifier:** Backfill from code inspection and deployed app

## Phase Goals vs. Deliverables

| Goal | Deliverable | Status | Evidence |
|------|-------------|--------|----------|
| 5-page Dash app (R1.15) | `src/app/app.py` + 5 pages in `src/app/pages/` | Pass | Home, How It Works, Architecture, Live Demo, Apply |
| Chat interface (R1.16) | `live_demo.py` with dbc.Input, chat history, callbacks | Pass | Example question buttons, message history, agent query |
| Path visualization (R1.17) | Badge-based entity path display | Pass | Provenance panel parses and displays entity path arrows |
| Provenance display (R1.18) | Path, Sources, Grounding sections in UI | Pass | `agent_client.py` `_parse_provenance()` extracts structured fields |
| Mock mode (R1.19) | `USE_MOCK_BACKEND` env var, `query_agent_mock()` | Pass | Canned Ruth→Jesus response, info alert when active |
| DABs deployment (R1.20) | `databricks.yml` + `deploy/webapp.yml` | Pass | App deployed at graphrag-demo-v2-dev-*.aws.databricksapps.com |
| Pipeline job as DAB resource (R1.21) | `deploy/pipeline_job.yml` with 6 tasks | Pass | Tasks 00-05 with correct dependency chain |

## E2E Test Coverage

`tests/test_demo_app.py` — Playwright E2E tests covering:
- Page navigation (all 5 pages)
- Live Demo: example question click, chat response, provenance panel
- Screenshots captured to `tests/screenshots/`

## MECE Audit

**Mutual exclusivity:** Each page is a separate module in `src/app/pages/`. Backend concerns (agent client, graph client, session store) are in `src/app/backend/`. No overlap between pages or backend modules.

**Collective exhaustiveness:** All 7 phase requirements (R1.15-R1.21) are delivered. The demo app covers the full tell-show-tell narrative: problem (Home), solution (How It Works), architecture (Architecture), live proof (Live Demo), business case (Apply).

## Issues Found

1. **Live agent not connected:** Demo runs in mock mode. Pipeline must be run to populate data and deploy the agent endpoint. Not a Phase 03 blocker — mock mode is the intended fallback.
2. **Test URL hardcoded:** `test_demo_app.py` uses a hardcoded deployed URL. Should be parameterized for different environments.
