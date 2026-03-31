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
│   ├── bible_registry.py      Complete 66-book KJV Bible metadata
│   ├── extraction/            LLM extraction: prompts, pipeline, dedup
│   ├── agent/                 LangGraph agent: tools, serving, pattern registry
│   ├── evaluation/            Governance scorers, MLflow evaluation, baselines
│   └── app/                   Dash web application (7 pages, backend, assets)
│
├── notebooks/                 DATABRICKS NOTEBOOKS
│   ├── 00–05                  Bible pipeline (config → data → graph → agent → demo → eval)
│   ├── 06–12                  Enron pipeline (data prep → graph → enrichment → agent → eval)
│   ├── 04_Incremental_Ingest  Add books without re-extracting everything
│   ├── 05_Remove_Books        Drop books from the graph
│   └── spikes/                Exploratory/debug notebooks
│
├── scripts/                   CLI utilities (deploy, eval, local test, preflight)
├── tests/                     ALL TESTS
├── deploy/                    DABs resource definitions (pipeline, enron, webapp, MCP)
├── docs/                      Blog posts and standalone documentation
│
├── .execution/                SDE + Drucker discipline (phases, decisions)
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
│                      │  get_source_evidence, get_entity_summary,
│                      │  find_cross_book_entities
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

### Bible Corpus (notebooks 00–05)

| Notebook | Purpose |
|----------|---------|
| `00_Intro_and_Config` | Configuration and setup |
| `01_Data_Prep` | Load Bible text into Delta tables |
| `02_Build_Knowledge_Graph` | `ai_query()` extracts entities and relationships in parallel via `responseFormat` |
| `03_Build_Agent` | Build LangGraph agent, log to MLflow, deploy to Model Serving |
| `04_Query_Demo` | Interactive demo with auditable, multi-hop answers |
| `05_Evaluation` | Governance + quality + cost comparison: GraphRAG vs flat RAG vs direct LLM |
| `04_Incremental_Ingest` | Add new books to the graph without full re-extraction |
| `05_Remove_Books` | Drop books from the graph cleanly |
| `RUNME` | Creates a Databricks Workflow for the full pipeline |

### Enron Email Corpus (notebooks 06–12)

| Notebook | Purpose |
|----------|---------|
| `06_Enron_Data_Prep` | Load Enron emails, participants, and threads into Delta |
| `07_Enron_Build_Knowledge_Graph` | Extract entities and relationships from email corpus |
| `07b–07m` | Enrichment: entity resolution, org hierarchy, communication aggregations, person identity, ontology, corpus coverage, extraction provenance, email classification, data quality, person roles, topic taxonomy, pipeline lineage |
| `08_Enron_Evaluation` | Evidence traceability: 13 scorers, 51-question eval suite |
| `09_Enron_Build_Agent` | Build and deploy the Enron-specific agent |
| `09_Enron_Genie_Spaces` | SQL analytics via Genie Spaces for tabular questions |
| `10_Enron_ABAC_Setup` | Attribute-based access control (row/column security) |
| `11_Enron_ABAC_Demo` | Demonstrate scoped retrieval under ABAC policies |
| `12_Enron_Pattern_Analysis` | Communication pattern and anomaly detection |

### Debug Notebook Workflow

Debug/spike notebooks live in `notebooks/spikes/`. They inline all configuration (no `%run` dependencies) and include diagnostic cells for schema inspection and sample data:

1. **Run the debug notebook interactively** on a cluster to identify and fix runtime errors cell by cell
2. **Incorporate fixes** back into the production notebook in `notebooks/`, which stays clean for headless job execution

## Evaluation: Governance First, Then Quality, Then Cost

Notebook `05_Evaluation` runs a rigorous side-by-side comparison of five configurations using MLflow `genai.evaluate()`:

| Config | Retrieval | Model | What It Proves |
|--------|-----------|-------|----------------|
| GraphRAG + 70B | Graph traversal | Llama 3.3 70B | Auditable reasoning at full quality |
| GraphRAG + 8B | Graph traversal | Llama 3.1 8B | Governance holds with smaller models |
| Flat RAG + 70B | Embedding similarity | Llama 3.3 70B | Best-case flat retrieval (no provenance) |
| Direct LLM + 70B | None | Llama 3.3 70B | Parametric knowledge only (no auditability) |
| Direct External | None | GPT-5.2 | Frontier model baseline |

### Governance Scorers (Bible)

| Scorer | What It Measures |
|--------|-----------------|
| Hallucination Check | Are all claims grounded in the knowledge graph? |
| Citation Completeness | What fraction of factual claims cite a source verse? |
| Provenance Chain | Does the response include a structured audit trail (path, sources, grounding)? |
| Reproducibility | Same query returns same path and citations across runs (Jaccard similarity)? |

### Enron Evidence Traceability Scorers (13 total)

The Enron evaluation uses a richer scorer suite covering evidence fabrication, participant verification, spelling correction transparency, corroboration consistency, citation completeness, and more — see `notebooks/08_Enron_Evaluation.py` for the full set.

## Demo: Bible Knowledge Graph

This accelerator builds a knowledge graph from the **complete King James Bible** — all 66 books (39 Old Testament + 27 New Testament) — the densest, most cross-referencing corpus of people, places, and events available. The Bible is the perfect proxy because lineage is verifiable: "How is Ruth connected to Jesus?" has a definitive, provably correct answer.

## Demo: Enron Email Corpus

The same architecture applies to the **Enron email corpus** — 20,000+ emails from 15 key custodians, demonstrating GraphRAG on real-world corporate communication data. The Enron pipeline adds:

- **Entity resolution** — unified resolver with alias, fuzzy (Levenshtein), and stem matching
- **Communication analytics** — dyad analysis, org hierarchy, person activity, topic taxonomy
- **Evidence traceability** — 13 custom scorers evaluating 51 questions across categories (entity exploration, entity pairs, timelines, keyword search, Genie analytics)
- **Attribute-based access control (ABAC)** — row/column-level security policies on emails, with sensitivity tiers (analyst, executive, legal)
- **Genie Spaces** — SQL-powered analytics for tabular questions (counts, rankings, percentages)
- **20+ agent tools** — including `find_entity`, `find_connections`, `trace_path`, `find_top_contacts`, `get_emails_between`, `search_emails`, `get_communication_timeline`, `get_activity_anomalies`, `get_relationship_evidence`, and more

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

**Architecture docs:** See `docs/` for standalone documentation and `.execution/` for the SDE execution system.

## Tech Stack

- **Databricks** — Unity Catalog, Delta Lake, Model Serving, MLflow
- **LangGraph** — Agent orchestration with tool-calling
- **Foundation Model API** — `databricks-meta-llama-3-3-70b-instruct` (70B) and `databricks-meta-llama-3-1-8b-instruct` (8B)
- **MLflow 3** — Tracing, model logging, and GenAI evaluation with governance scorers via `ResponsesAgent`
- **Dash** (Plotly) — Interactive demo web app with `dash_bootstrap_components` (DARKLY theme)
- **Databricks Asset Bundles** — One-command deployment (`databricks bundle deploy`) for both the web app and pipeline job
