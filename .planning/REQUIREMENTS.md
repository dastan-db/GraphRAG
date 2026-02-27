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

## v2 (later)

- Vector Search integration for hybrid retrieval (graph + semantic)
- Automated policy enforcement (block answers violating defined rules)
- Incremental ingestion (add new books without re-extracting)
- Production ops: scheduled re-extraction, monitoring
- Generalize to non-biblical document corpora

## Out of scope

- UI, multi-tenant auth, real-time streaming ingestion.
