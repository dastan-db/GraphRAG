# State

## Position

- **Milestone:** v1
- **Current phase:** 01 (complete — Solution Accelerator scaffold, Bible data prep, KG extraction, agent, demo)
- **Next step:** Deploy to Databricks workspace and run end-to-end. Then evaluate and iterate.

## Decisions

- **Pivoted from codebase understanding to document Q&A** (Bible corpus) — aligns with original PRFAQ.
- **Pivoted from Neo4j to Delta tables only** — no external graph DB, fully Databricks-native.
- **Format: Databricks Solution Accelerator** — numbered notebooks, RUNME.py, util/ shared code.
- **Agent pattern: LangGraph ResponsesAgent** with custom @tool functions querying Delta via Spark SQL.
- **Bible books: Genesis, Exodus, Ruth, Matthew, Acts** — chosen for dense cross-book entity/relationship connections.
- **LLM: databricks-meta-llama-3-3-70b-instruct** via Foundation Model API.
- GSD-style execution (context in .planning/, atomic commits).

## Blockers

- None.
