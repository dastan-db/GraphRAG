# Roadmap

## Milestone: v1

| Phase | Name                                    | Status    | Notes                                                    |
|-------|-----------------------------------------|-----------|----------------------------------------------------------|
| 01    | Solution Accelerator (full loop)        | complete  | Bible KG + agent + demo, Databricks-native               |
| 02    | Governance Reframe                      | complete  | Provenance output, governance scorers, narrative reframe |
| 03    | Interactive Demo Web App                | complete  | 5-page Streamlit app deployed as Databricks App via DABs |

## Milestone: v2

| Phase | Name                                    | Status  | Notes                                                    |
|-------|-----------------------------------------|---------|----------------------------------------------------------|
| 04    | Code Domain Ingestion                   | planned | Tree-sitter/AST parsing, pluggable extraction interface, code-specific entity types (Module, Class, Function) and relationship types (CALLS, IMPORTS, INHERITS) |
| 05    | Lakebase Graph Serving                  | planned | Mirror serving slice into Lakebase, indexed OLTP lookups, benchmark against current Spark SQL tools |
| 06    | Hybrid Retrieval                        | planned | Vector Search integration, graph + semantic combined retrieval |
| 07    | Graph Engine Evaluation                 | planned | Benchmark Memgraph/Neo4j/FalkorDB for complex traversal patterns at scale |

## Backlog

- Connect live agent endpoint to demo (replace mock mode after pipeline run)
- Lakebase for persistent chat history
- Automated policy enforcement rules
- Production ops: scheduled re-extraction, monitoring
