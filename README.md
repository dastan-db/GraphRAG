# GraphRAG: Auditable AI Reasoning on Databricks

A Databricks Solution Accelerator that demonstrates **GraphRAG** — using a knowledge graph to deliver LLM-powered answers that are **auditable, traceable, and reproducible**. Every answer shows its work: explicit provenance chains, verse-level citations, and grounding indicators.

## Quick Start

1. Clone this repo into your Databricks workspace
2. Attach `RUNME.py` to any cluster (DBR 15.4+) and **Run All** — or run the notebooks in `notebooks/` interactively in order
3. Modify `src/config.py` to change catalog, schema, or LLM endpoint

No manual setup steps required. The pipeline uses `ai_query()` with `responseFormat` for both entity and relationship extraction, and automatically deploys the agent to a Model Serving endpoint.

**Workspace requirements:** Unity Catalog enabled, serverless compute enabled.

## Repository Structure

```
GraphRAG/
├── README.md                  Project overview
├── RUNME.py                   One-command demo (creates Databricks Workflow)
├── pyproject.toml             Python project config + dependencies
├── databricks.yml             DABs deployment manifest
│
├── src/                       ALL PRODUCT SOURCE CODE
│   ├── config.py              Shared config (catalog, schema, endpoints)
│   ├── data/                  Data engineering: loading, schema, Delta ops
│   ├── extraction/            LLM extraction: prompts, pipeline, dedup
│   ├── agent/                 LangGraph agent: tools, state, serving
│   ├── evaluation/            Governance scorers, MLflow evaluation, baselines
│   └── app/                   Dash web application (pages, backend, assets)
│
├── notebooks/                 DATABRICKS NOTEBOOKS (pipeline walkthrough)
│   ├── 00_Intro_and_Config.py
│   ├── 01_Data_Prep.py
│   ├── 02_Build_Knowledge_Graph.py
│   ├── 03_Build_Agent.py
│   ├── 04_Query_Demo.py
│   ├── 05_Evaluation.py
│   └── spikes/                Exploratory/debug notebooks (temporary)
│
├── tests/                     ALL TESTS
├── deploy/                    DABs resource definitions (jobs, apps)
├── docs/                      Documentation (non-planning)
├── data/                      Raw/reference data (small, committed)
│
├── .planning/                 GSD + Drucker discipline (phases, decisions)
└── .cursor/                   Cursor tooling (rules, agents, skills)
```

## The Problem

Enterprise AI has a governance crisis. When an LLM answers a question, no one can prove *why* it said what it said:

- **"Why did the AI recommend this?"** — You can't trace the reasoning path.
- **"Which data led to this outcome?"** — Embedding retrieval is a black box.
- **"I got answer A yesterday, why do I get answer B today?"** — Results aren't reproducible.
- **"Did the AI make this up?"** — You can't distinguish grounded claims from hallucinations.

Standard RAG retrieves flat text chunks by embedding similarity. This improves relevance, but the retrieval step itself is opaque.

## The Solution

GraphRAG replaces opaque embedding retrieval with **structured graph traversal**. Entities and relationships extracted from documents become a knowledge graph. When the LLM answers a question, it traverses explicit paths through this graph — and every answer includes a **provenance chain** showing exactly which entities, relationships, and source documents contributed.

```
Without GraphRAG (traditional RAG):
  Q: "How is Ruth connected to Jesus?"
  A: "Ruth is mentioned in Matthew's genealogy..."
  Auditor: "Prove it. Show the path."
  System: ¯\_(ツ)_/¯

With GraphRAG:
  Q: "How is Ruth connected to Jesus?"
  A: "Ruth married Boaz, who fathered Obed..."

  Provenance:
    Path: Ruth → Boaz (MARRIED_TO, Ruth 4:13) → Obed (FATHER_OF, Ruth 4:17)
          → Jesse (FATHER_OF, Ruth 4:22) → David → ... → Jesus (Matthew 1:16)
    Sources: Ruth 4:13, Ruth 4:17, Ruth 4:22, Matthew 1:5-6, Matthew 1:16
    Grounding: All claims backed by knowledge graph traversal

  Auditor: "Perfect. Auditable. Traceable. Reproducible." ✓
```

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
│  02: Knowledge Graph │  ai_query() extracts entities and relationships
│                      │  in parallel via responseFormat structured output
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  03: Agent           │  LangGraph agent with graph traversal tools:
│                      │  find_entity, find_connections, trace_path,
│                      │  get_context_verses, get_entity_summary
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  04: Query Demo      │  Auditable Q&A with structured provenance
│                      │  Multi-hop reasoning over the knowledge graph
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  05: Evaluation      │  Governance metrics: hallucination rate,
│                      │  citation completeness, provenance quality,
│                      │  reproducibility — plus quality and cost
└─────────────────────┘
```

## Pipeline

| Notebook | Purpose |
|----------|---------|
| `notebooks/00_Intro_and_Config` | Configuration and setup |
| `notebooks/01_Data_Prep` | Load Bible text into Delta tables |
| `notebooks/02_Build_Knowledge_Graph` | `ai_query()` extracts entities and relationships in parallel via `responseFormat` |
| `notebooks/03_Build_Agent` | Build LangGraph agent, log to MLflow, deploy to Model Serving |
| `notebooks/04_Query_Demo` | Interactive demo with auditable, multi-hop answers |
| `notebooks/05_Evaluation` | Governance + quality + cost comparison: GraphRAG vs flat RAG vs direct LLM |
| `RUNME` | Creates a Databricks Workflow for the full pipeline |

### Debug Notebook Workflow

Debug/spike notebooks live in `notebooks/spikes/`. They inline all configuration (no `%run` dependencies) and include diagnostic cells for schema inspection and sample data:

1. **Run the debug notebook interactively** on a cluster to identify and fix runtime errors cell by cell
2. **Incorporate fixes** back into the production notebook in `notebooks/`, which stays clean for headless job execution

## Evaluation: Governance First, Then Quality, Then Cost

Notebook `05_Evaluation` runs a rigorous side-by-side comparison of four configurations on 20 ground-truth questions using MLflow `genai.evaluate()`:

| Config | Retrieval | Model | What It Proves |
|--------|-----------|-------|----------------|
| GraphRAG + 70B | Graph traversal | Llama 3.3 70B | Auditable reasoning at full quality |
| GraphRAG + 8B | Graph traversal | Llama 3.1 8B | Governance holds with smaller models |
| Flat RAG + 70B | Embedding similarity | Llama 3.3 70B | Best-case flat retrieval (no provenance) |
| Direct LLM | None | Llama 3.3 70B | Parametric knowledge only (no auditability) |

### Governance Scorers

| Scorer | What It Measures |
|--------|-----------------|
| Hallucination Check | Are all claims grounded in the knowledge graph? |
| Citation Completeness | What fraction of factual claims cite a source verse? |
| Provenance Chain | Does the response include a structured audit trail (path, sources, grounding)? |
| Reproducibility | Same query returns same path and citations across runs? |

## Demo: Bible Knowledge Graph

This accelerator builds a knowledge graph from five books of the King James Bible — chosen for their dense, cross-referencing network of people, places, and events. The Bible is the perfect proxy because lineage is verifiable: "How is Ruth connected to Jesus?" has a definitive, provably correct answer.

| Book | Testament | Why Selected |
|------|-----------|--------------|
| Genesis | OT | Foundational: Abraham, Isaac, Jacob, Joseph, creation, covenant |
| Exodus | OT | Continues Genesis: Moses, plagues, Red Sea, Sinai, the Law |
| Ruth | OT | Bridge book: connects patriarchs to David's lineage (and to Jesus) |
| Matthew | NT | Genealogy traces Jesus back to Abraham/David/Ruth/Boaz |
| Acts | NT | References Abraham, Moses, David; Peter and Paul's missions |

## Applying This Pattern to Your Domain

This demo uses the Bible as a corpus, but the pattern applies directly to any domain with dense entity relationships:

| Bible Domain | Code/Architecture Domain | Supply Chain Domain |
|---|---|---|
| Person (Moses, Paul) | Module, Class, Service | Supplier, Warehouse |
| Place (Egypt, Jerusalem) | Repository, Deployment Target | Region, Distribution Center |
| Event (Exodus, Pentecost) | Release, Incident, Migration | Order, Shipment, Outage |
| FAMILY_OF, ANCESTOR_OF | IMPORTS, INHERITS_FROM | SUPPLIES_TO, SOURCES_FROM |
| *"How is Ruth connected to Jesus?"* | *"What services depend on this schema change?"* | *"Which customers are affected if Supplier X is delayed?"* |

**Extraction is domain-specific; the architecture is not.** For document corpora, entities and relationships are extracted via LLM (`ai_query()` with structured output). For codebases, entities and relationships are extracted deterministically via Tree-sitter / AST analysis. Everything downstream — graph storage, traversal tools, agent, provenance, and governance — is identical.

## Development

**Setup:**
```bash
pip install -e ".[dev]"
```

**Deployment:**
```bash
databricks bundle deploy --target dev
```

**Architecture docs:** See `docs/` for standalone documentation and `.planning/SYSTEM.md` for the execution system.

## Tech Stack

- **Databricks** — Unity Catalog, Delta Lake, Model Serving, MLflow
- **LangGraph** — Agent orchestration with tool-calling
- **Foundation Model API** — `databricks-meta-llama-3-3-70b-instruct` (70B) and `databricks-meta-llama-3-1-8b-instruct` (8B)
- **MLflow 3** — Tracing, model logging, and GenAI evaluation with governance scorers via `ResponsesAgent`
- **Dash** (Plotly) — Interactive demo web app with `dash_bootstrap_components` (DARKLY theme)
- **Databricks Asset Bundles** — One-command deployment (`databricks bundle deploy`) for both the web app and pipeline job
