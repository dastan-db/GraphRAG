# Requirements

## v1 (current milestone)

### Data & Graph

| ID   | Requirement                                       | Status    |
|------|---------------------------------------------------|-----------|
| R1.1 | Load Bible KJV text into Delta tables             | Done      |
| R1.2 | LLM entity extraction (Person, Place, Event, etc.)| Done      |
| R1.3 | LLM relationship extraction between entities      | Done      |
| R1.4 | Entity deduplication and verse-level mentions      | Done      |

### Agent & Tools

| ID   | Requirement                                       | Status    |
|------|---------------------------------------------------|-----------|
| R1.5 | Graph traversal tools (5 tools querying Delta)     | Done      |
| R1.6 | LangGraph agent with tool-calling                  | Done      |
| R1.7 | MLflow model logging and optional deployment       | Done      |
| R1.8 | Interactive demo with multi-hop questions           | Done      |
| R1.9 | RUNME.py workflow orchestrator                     | Done      |

### Governance & Auditability

| ID    | Requirement                                                        | Status    |
|-------|---------------------------------------------------------------------|-----------|
| R1.10 | Structured provenance output in agent responses (path, sources, grounding) | Done |
| R1.11 | Hallucination detection scorer (flags claims not grounded in graph) | Done      |
| R1.12 | Citation completeness scorer (ratio of cited to total claims)       | Done      |
| R1.13 | Provenance chain scorer (structured audit trail present in output)  | Done      |
| R1.14 | Reproducibility validation (same query → same path across runs)     | Done      |

### Interactive Demo Web App

| ID    | Requirement                                                        | Status    |
|-------|---------------------------------------------------------------------|-----------|
| R1.15 | 5-page Dash web app with dash_bootstrap_components (Home, How It Works, Architecture, Live Demo, Apply) | Done |
| R1.16 | Live Demo page with chat interface calling agent endpoint           | Done      |
| R1.17 | Path visualization (badge-based entity path display in Dash)         | Done      |
| R1.18 | Provenance display (path, sources, grounding) in demo UI            | Done      |
| R1.19 | Mock mode for demo without live agent endpoint                      | Done      |
| R1.20 | Databricks Asset Bundle for one-command deployment                  | Done      |
| R1.21 | Pipeline job as DAB resource (notebooks 00-05)                      | Done      |

## v2 (later)

### Dual-Domain Extraction

| ID    | Requirement                                                                  | Status  |
|-------|------------------------------------------------------------------------------|---------|
| R2.1  | Deterministic code ingestion: Tree-sitter / AST parsing for symbols, calls, imports, inheritance | Planned |
| R2.2  | Pluggable extraction interface (LLM-based for text, deterministic for code, same graph schema output) | Planned |

### Graph Backend Evolution

| ID    | Requirement                                                                  | Status  |
|-------|------------------------------------------------------------------------------|---------|
| R2.3  | Lakebase as OLTP graph serving layer (entities/edges/neighborhood caches in Postgres-compatible tables) | Planned |
| R2.4  | Evaluation of dedicated graph engines (Memgraph, Neo4j, FalkorDB) for richer traversal patterns | Planned |

### Retrieval Enhancements

| ID    | Requirement                                                                  | Status  |
|-------|------------------------------------------------------------------------------|---------|
| R2.5  | Vector Search integration for hybrid retrieval (graph + semantic)            | Planned |
| R2.6  | Incremental ingestion (add new documents/repos without full re-extraction)   | Planned |

### Operational

| ID    | Requirement                                                                  | Status  |
|-------|------------------------------------------------------------------------------|---------|
| R2.7  | Lakebase for persistent chat history (schema ready, needs instance provisioning) | Planned |
| R2.8  | Automated policy enforcement (block answers violating defined rules)         | Planned |
| R2.9  | Production ops: scheduled re-extraction, monitoring                          | Planned |
| R2.10 | Generalize pipeline to arbitrary document corpora and code repositories      | Planned |

## Out of scope

- Multi-tenant auth, real-time streaming ingestion.
