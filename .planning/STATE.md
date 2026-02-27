# State

## Position

- **Milestone:** v1
- **Current phase:** 02 (complete — Governance reframe: provenance output, governance scorers, narrative repositioning)
- **Next step:** Deploy to Databricks workspace, run end-to-end, validate governance metrics.

## Decisions

- **Pivoted from codebase understanding to document Q&A** (Bible corpus) — aligns with original PRFAQ.
- **Pivoted from Neo4j to Delta tables only** — no external graph DB, fully Databricks-native.
- **Format: Databricks Solution Accelerator** — numbered notebooks, RUNME.py, util/ shared code.
- **Agent pattern: LangGraph ResponsesAgent** with custom @tool functions querying Delta via Spark SQL.
- **Bible books: Genesis, Exodus, Ruth, Matthew, Acts** — chosen for dense cross-book entity/relationship connections.
- **LLM: databricks-meta-llama-3-3-70b-instruct** via Foundation Model API.
- **Governance-first positioning:** Lead with auditability, traceability, reproducibility; cost savings is secondary.
- **Structured provenance output:** Agent includes Provenance section (path, sources, grounding) in every response.
- **Governance evaluation scorers:** hallucination_check, citation_completeness, provenance_chain, reproducibility.
- GSD-style execution (context in .planning/, atomic commits).

## Blockers

- None.
