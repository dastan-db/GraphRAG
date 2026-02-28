# State

## Position

- **Milestone:** v1
- **Current phase:** 03 (complete — Interactive Demo Web App: 5-page Dash app deployed via DABs)
- **Next step:** Run pipeline job to populate data, deploy agent to Model Serving, switch demo from mock to live mode.

## Decisions

- **Bible corpus is a proxy dataset.** Architecture is domain-agnostic; the Bible validates the graph traversal / agent / provenance / governance layers. Code domain ingestion (Tree-sitter / AST) is planned for v2.
- **Extraction is domain-specific, architecture is not.** LLM extraction (`ai_query()`) for text corpora (enterprise KBs, legal docs, regulatory filings); deterministic parsing (Tree-sitter, AST analysis) for codebases. Graph schema, traversal tools, agent, and evaluation are shared.
- **Graph backend evolution path:** Delta tables (v1, current) → Lakebase OLTP (v2 Phase 05) → dedicated graph engine such as Memgraph, Neo4j, or FalkorDB (v2 Phase 07, if needed).
- **Pivoted from Neo4j to Delta tables for v1** — no external graph DB, fully Databricks-native. Graph engine evaluation deferred to v2.
- **Format: Databricks Solution Accelerator** — notebooks in `notebooks/`, RUNME.py at root, shared code in `src/`.
- **Agent pattern: LangGraph ResponsesAgent** with custom @tool functions querying Delta via Spark SQL.
- **Bible books: Genesis, Exodus, Ruth, Matthew, Acts** — chosen for dense cross-book entity/relationship connections.
- **LLM: databricks-meta-llama-3-3-70b-instruct** via Foundation Model API.
- **Governance-first positioning:** Lead with auditability, traceability, reproducibility; cost savings is secondary.
- **Structured provenance output:** Agent includes Provenance section (path, sources, grounding) in every response.
- **Governance evaluation scorers:** hallucination_check, citation_completeness, provenance_chain, reproducibility.
- **Web app framework: Dash (Plotly)** with dash_bootstrap_components (DARKLY theme), multi-page with dcc.Location routing, chat UI with dbc.Input/callbacks.
- **Web app structure: tell-show-tell** — Pages 1-3 (problem/solution/architecture), Page 4 (live demo), Page 5 (business application).
- **Deployment: Databricks Asset Bundles** — databricks.yml + deploy/*.yml, single `databricks bundle deploy`.
- **Mock mode:** Demo page works without live agent endpoint; set USE_MOCK_BACKEND=false when endpoint is ready.
- **Debug-first notebook workflow:** When building or modifying pipeline notebooks, always produce a companion `XX_Interactive_Debug.py` with inlined config and diagnostic cells. User runs interactively on a cluster, fixes errors cell by cell, then agent incorporates fixes back into the production notebook. Debug notebooks stay in the repo as runnable references.
- GSD-style execution (context in .planning/, atomic commits).

## Deployed Resources

- **App URL:** https://graphrag-demo-v2-dev-7474658617709921.aws.databricksapps.com
- **Bundle target:** dev (DEFAULT profile)
- **Workspace:** https://fevm-serverless-8e8gyh.cloud.databricks.com

## Blockers

- None.
