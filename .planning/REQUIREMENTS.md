# Requirements

## v1 (current milestone)

| ID   | Requirement                                       | Status    |
|------|---------------------------------------------------|-----------|
| R1.1 | Load Bible KJV text into Delta tables             | Done      |
| R1.2 | LLM entity extraction (Person, Place, Event, etc.)| Done      |
| R1.3 | LLM relationship extraction between entities      | Done      |
| R1.4 | Entity deduplication and verse-level mentions      | Done      |
| R1.5 | Graph traversal tools (5 tools querying Delta)     | Done      |
| R1.6 | LangGraph agent with tool-calling                  | Done      |
| R1.7 | MLflow model logging and optional deployment       | Done      |
| R1.8 | Interactive demo with multi-hop questions           | Done      |
| R1.9 | RUNME.py workflow orchestrator                     | Done      |

## v2 (later)

- Vector Search integration for hybrid retrieval (graph + semantic)
- Evaluation harness with MLflow scorers
- Incremental ingestion (add new books without re-extracting)
- Production ops: scheduled re-extraction, monitoring
- Generalize to non-biblical document corpora

## Out of scope

- UI, multi-tenant auth, real-time streaming ingestion.
