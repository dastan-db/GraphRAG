# GraphRAG: Graph-Powered RAG on Databricks

A Databricks Solution Accelerator that demonstrates **GraphRAG** — using a knowledge graph to power LLM-based Q&A that reasons across relationships, not just individual passages.

## The Problem

Standard RAG retrieves flat text chunks by embedding similarity. It loses the relationships between entities that give answers their meaning. Questions requiring multi-hop reasoning — "How is Ruth connected to Jesus?" — get wrong or incomplete answers.

## The Solution

GraphRAG adds a graph layer: entities and relationships extracted from documents become a structured index. An LLM-powered agent traverses this graph to find connected evidence before synthesizing an answer.

## Demo: Bible Knowledge Graph

This accelerator builds a knowledge graph from five books of the King James Bible — chosen for their dense, cross-referencing network of people, places, and events:

| Book | Testament | Why Selected |
|------|-----------|--------------|
| Genesis | OT | Foundational: Abraham, Isaac, Jacob, Joseph, creation, covenant |
| Exodus | OT | Continues Genesis: Moses, plagues, Red Sea, Sinai, the Law |
| Ruth | OT | Bridge book: connects patriarchs to David's lineage (and to Jesus) |
| Matthew | NT | Genealogy traces Jesus back to Abraham/David/Ruth/Boaz |
| Acts | NT | References Abraham, Moses, David; Peter and Paul's missions |

## Pipeline

| Notebook | Purpose |
|----------|---------|
| `00_Intro_and_Config` | Configuration and setup |
| `01_Data_Prep` | Load Bible text into Delta tables |
| `02_Build_Knowledge_Graph` | LLM extracts entities and relationships |
| `03_Build_Agent` | Build LangGraph agent with graph traversal tools |
| `04_Query_Demo` | Interactive demo with multi-hop questions |
| `RUNME` | Creates a Databricks Workflow for the full pipeline |

## Getting Started

1. Clone this repo into your Databricks workspace
2. Attach `RUNME.py` to any cluster (DBR 15.4+) and Run All — or run notebooks interactively in order
3. Modify `util/config.py` to change catalog, schema, or LLM endpoint

## Architecture

```
Bible Text (KJV)
    │
    ▼
┌─────────────────────┐
│  01: Data Prep       │  Load into Delta (books, chapters, verses)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  02: Knowledge Graph │  LLM extracts entities (Person, Place, Event...)
│                      │  and relationships (FAMILY_OF, LOCATED_IN...)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  03: Agent           │  LangGraph agent with graph traversal tools:
│                      │  find_entity, find_connections, trace_path,
│                      │  get_context_verses, get_entity_summary
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  04: Query Demo      │  "How is Ruth connected to Jesus?"
│                      │  Multi-hop reasoning over the knowledge graph
└─────────────────────┘
```

## Tech Stack

- **Databricks** — Unity Catalog, Delta Lake, Model Serving, MLflow
- **LangGraph** — Agent orchestration with tool-calling
- **Foundation Model API** — `databricks-meta-llama-3-3-70b-instruct`
- **MLflow 3** — Tracing and model logging via `ResponsesAgent`
