# GraphRAG: Auditable AI Reasoning on Databricks

A Databricks Solution Accelerator that demonstrates **GraphRAG** — using a knowledge graph to deliver LLM-powered answers that are **auditable, traceable, and reproducible**. Every answer shows its work: explicit provenance chains, source-level citations, and grounding indicators.

The primary demo corpus is the **Enron email dataset** — 20,000+ real corporate emails from 15 key custodians — demonstrating GraphRAG on the kind of data enterprises actually need to reason over: organizational communication, reporting structures, investigation timelines, and access-controlled documents.

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
│   ├── agent/                 Thin serving shim + core Enron agent modules
│   ├── evaluation/            Shipped governance scorers, question bank, active baselines
│   ├── runtime/               Thin orchestrator, contracts, response parsing
│   └── app/                   Dash web application (5 pages, backend, assets)
│
├── notebooks/                 DATABRICKS NOTEBOOKS
│   ├── 06–12                  Enron pipeline (data prep → graph → enrichment → agent → eval)
│   ├── 00–05                  Legacy notebooks retained for historical reference only
│   └── spikes/                Exploratory/debug notebooks
│
├── scripts/                   CLI utilities (deploy, eval, local test, preflight)
├── tests/                     ALL TESTS
├── deploy/                    DABs resource definitions (pipeline and webapp)
├── docs/                      Blog posts and standalone documentation
│
├── .execution/                SDE + Drucker discipline (phases, decisions)
└── .cursor/                   Cursor tooling (rules, agents, skills)
```

Customer-facing product code lives in `src/`. The active architecture is Enron-first and intentionally narrow: core graph/evidence tools, a thin runtime wrapper, governed evaluation flows, and the Dash app.

## The Problem

Enterprise AI has a governance crisis. When an LLM answers a question, no one can prove *why* it said what it said:

- **"Why did the AI recommend this?"** — You can't trace the reasoning path.
- **"Which data led to this outcome?"** — Embedding retrieval is a black box.
- **"I got answer A yesterday, why do I get answer B today?"** — Results aren't reproducible.
- **"Did the AI make this up?"** — You can't distinguish grounded claims from hallucinations.

Standard RAG retrieves flat text chunks by embedding similarity. This improves relevance, but the retrieval step itself is opaque — especially dangerous for compliance investigations, legal discovery, and regulatory audit where every claim must be traceable to source evidence.

## The Solution

GraphRAG replaces opaque embedding retrieval with **structured graph traversal**. Entities and relationships extracted from documents become a knowledge graph. When the LLM answers a question, it traverses explicit paths through this graph — and every answer includes a **provenance chain** showing exactly which entities, relationships, and source documents contributed.

```
Without GraphRAG (traditional RAG):
  Q: "What was Kenneth Lay's relationship with Andrew Fastow?"
  A: "They were both Enron executives..."
  Investigator: "Show me the evidence. Which emails? What dates?"
  System: ¯\_(ツ)_/¯

With GraphRAG:
  Q: "What was Kenneth Lay's relationship with Andrew Fastow?"
  A: "Kenneth Lay (CEO) had direct email communication with Andrew Fastow (CFO).
     Fastow reported to Lay on LJM partnership structures..."

  Provenance:
    Path: Kenneth Lay → Andrew Fastow (COMMUNICATED_WITH, 47 direct emails)
          → LJM Partnership (MANAGED_BY, Fastow)
    Sources: 12 direct emails (2000-08 to 2001-10), org hierarchy evidence
    Evidence: email-id-4523 (2001-03-14), email-id-7891 (2001-08-22), ...
    Grounding: All claims backed by email evidence in the knowledge graph

  Investigator: "Perfect. Auditable. Traceable. Reproducible." ✓
```

## Architecture

```
Enron Emails (20,000+)
    │
    ▼
┌─────────────────────┐
│  06: Data Prep       │  Load emails, participants, threads into Delta
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  07: Knowledge Graph │  ai_query() extracts entities and relationships;
│  07b–07m: Enrichment │  entity resolution, org hierarchy, communication
│                      │  analytics, and supporting pipeline tables
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  09: Agent           │  LangGraph agent with focused tools:
│                      │  find_entity, find_connections, trace_path,
│                      │  find_top_contacts, get_emails_between,
│                      │  search_emails, get_communication_timeline,
│                      │  get_relationship_evidence, and quantitative analytics
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  08: Evaluation      │  Evidence traceability: 13 scorers,
│                      │  51-question eval suite across 5 categories
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  10–12: Governance   │  ABAC (row/column security), scoped retrieval,
│                      │  communication pattern and anomaly detection
└─────────────────────┘
```

## Pipeline

### Enron Email Corpus (notebooks 06–12) — Primary

| Notebook | Purpose |
|----------|---------|
| `06_Enron_Data_Prep` | Load Enron emails, participants, and threads into Delta |
| `07_Enron_Build_Knowledge_Graph` | Extract entities and relationships from email corpus |
| `07b–07m` | Enrichment: entity resolution, org hierarchy, communication aggregations, person identity, ontology, corpus coverage, extraction provenance, email classification, data quality, person roles, topic taxonomy, pipeline lineage |
| `08_Enron_Evaluation` | Evidence traceability: 13 scorers, 51-question eval suite |
| `09_Enron_Build_Agent` | Build and deploy the Enron-specific agent with PDES architecture |
| `09_Enron_Genie_Spaces` | SQL analytics via Genie Spaces for tabular questions |
| `10_Enron_ABAC_Setup` | Attribute-based access control (row/column security) |
| `11_Enron_ABAC_Demo` | Demonstrate scoped retrieval under ABAC policies |
| `12_Enron_Pattern_Analysis` | Communication pattern and anomaly detection |

### Legacy Notebooks (00–05)

The earlier Bible workflow is no longer part of the active architecture, demo surface, or governed evaluation loop. Those notebooks remain in the repo only as historical reference while the core product stays focused on the Enron use case.

### Debug Notebook Workflow

Debug/spike notebooks live in `notebooks/spikes/`. They inline all configuration (no `%run` dependencies) and include diagnostic cells for schema inspection and sample data:

1. **Run the debug notebook interactively** on a cluster to identify and fix runtime errors cell by cell
2. **Incorporate fixes** back into the production notebook in `notebooks/`, which stays clean for headless job execution

## Evaluation

### Enron Evidence Traceability (13 scorers, 51 questions)

The primary evaluation uses 13 custom scorers across 51 questions in 5 categories (entity exploration, entity pairs, timelines, keyword search, Genie analytics):

| Scorer Category | What It Measures |
|----------------|-----------------|
| Evidence fabrication | Does the response fabricate email evidence that doesn't exist? |
| Participant verification | Are email participants accurately identified from the graph? |
| Corroboration consistency | Do multiple evidence sources tell a consistent story? |
| Citation completeness | Does the response cite specific emails, dates, and threads? |
| Spelling correction transparency | Are name corrections (e.g., "Skiling" → "Skilling") disclosed? |

The agent uses a **Plan-Decompose-Execute-Synthesize (PDES)** architecture with 6 MECE question primitives: `entity_explore`, `entity_pair`, `timeline`, `keyword_search`, `genie_analytics`, and `general`.

## Key Capabilities

- **Unified entity resolution** — `ResolvedEntity` dataclass with 4-stage cascade (exact → alias → fuzzy/Levenshtein → stem) shared across all tools
- **Focused investigative toolset** — `find_entity`, `find_connections`, `trace_path`, `find_top_contacts`, `get_emails_between`, `search_emails`, `get_communication_timeline`, `get_relationship_evidence`, `get_source_evidence`, and related quantitative analytics
- **Communication analytics** — dyad analysis, org hierarchy, person activity, and Genie-backed tabular analytics
- **Genie Spaces** — SQL-powered analytics for tabular questions (counts, rankings, percentages)
- **Attribute-based access control (ABAC)** — row/column-level security with sensitivity tiers (analyst, executive, legal)
- **Tool latency instrumentation** — MLflow span tracing with per-tool SLA thresholds

## Applying This Pattern to Your Domain

The Enron email corpus demonstrates the pattern on corporate communication data, but the architecture applies to any domain with dense entity relationships:

| Corporate Communication | Code/Architecture | Supply Chain | Financial Services |
|---|---|---|---|
| Person (Lay, Skilling) | Module, Class, Service | Supplier, Warehouse | Client, Counterparty |
| Organization (Enron, LJM) | Repository, Cluster | Region, Distribution Center | Market, Jurisdiction |
| Event (Investigation, Meeting) | Release, Incident | Order, Shipment | Trade, Filing |
| COMMUNICATED_WITH | IMPORTS, DEPENDS_ON | SUPPLIES_TO | COUNTERPARTY_TO |
| *"Who communicated most with Kenneth Lay?"* | *"What services depend on this schema change?"* | *"Which customers are affected if Supplier X is delayed?"* | *"What is our exposure chain to this counterparty?"* |

**Extraction is domain-specific; the architecture is not.** For document corpora, entities and relationships are extracted via LLM (`ai_query()` with structured output). For codebases, entities and relationships are extracted deterministically via Tree-sitter / AST analysis. Everything downstream — graph storage, traversal tools, agent, provenance, and governance — is identical.

## Development

**Setup:**
```bash
pip install -e ".[dev]"
```

For **`GRAPHRAG_BACKEND=lakebase`** (Lakebase Autoscaling / PostgreSQL), install the pool driver:

```bash
pip install -e ".[dev,lakebase]"
```

**Lakebase schema and data (Databricks workspace):**

1. **Unity Catalog** — Build Delta tables first: Enron graph (`notebooks/07_Enron_Build_Knowledge_Graph.py`), communication aggregations (`07c_Enron_Communication_Aggregations.py`), etc., so `relationships` (with `edge_count`, `source_threads`), `communication_dyads`, and `person_activity` exist in your catalog.
2. **Sync to Lakebase** — Create tables, migrate older schemas, and load from the warehouse:

```bash
python scripts/setup_lakebase.py --enron
```

Reload data after pipeline or script changes:

```bash
python scripts/setup_lakebase.py --enron --refresh
```

Batch sync from the workspace is also available in `notebooks/08_Enron_Lakebase_Sync.py`. See `scripts/setup_lakebase.py --help` for `--indexes-only`, `--rls-only`, and `--teardown`.

**Data parity (Delta vs DuckDB vs Lakebase):** after export and/or `--refresh`, compare row counts across all three:

```bash
python scripts/check_data_parity.py --corpus enron
# Compare only sources you have: e.g. --skip-lakebase if Lakebase is down
```

If DuckDB counts are **low** (e.g. stuck at ~64k) while Delta/Lakebase match, re-run **`export_local_data.py`** — the exporter now follows **all** Statement API result chunks (not just the first).
**`enron.threads`** is created and loaded with **manifest DDL** (same columns as Delta, including optional `summary` / `key_topics`). If **`threads`** load failed under an older fixed schema, run once in Lakebase: `DROP TABLE IF EXISTS enron.threads;` then **`python scripts/setup_lakebase.py --enron --refresh`**.

**Shared runtime modes:**
- `local-fast`: `GRAPHRAG_BACKEND=local`, direct runtime, fastest edit/test loop
- `local-integration`: `GRAPHRAG_BACKEND=lakebase` (default for remote data) or `databricks` (SQL warehouse / Statement Execution API), same orchestrator surface without a serving deploy
- `deployed`: endpoint transport against `graphrag-enron-agent`

**Local validation:**
```bash
python scripts/test_local.py "Who communicated most frequently with Kenneth Lay?"
python scripts/validate_local.py --corpus enron
python scripts/validate_parity.py --llm databricks
python scripts/preflight.py --parity
```

The public serving entrypoint remains `src/agent/agent_serving.py`, but it now acts as a thin compatibility loader over `src/agent/_agent_core.py`. For day-to-day navigation, prefer `src/agent/_agent_core.py`, `src/agent/enron_tools.py`, `src/agent/enron_analytics_tools.py`, `src/agent/pattern_registry.py`, and `src/runtime/`.

**Deployment:**
```bash
databricks bundle deploy --target dev
```

**Architecture docs:** See `docs/` for standalone documentation and `.execution/` for the SDE execution system.

## Tech Stack

- **Databricks** — Unity Catalog, Delta Lake, Model Serving, MLflow, Genie Spaces
- **LangGraph** — Agent orchestration with tool-calling
- **Foundation Model API** — `databricks-gpt-5-4-nano` for synthesis and `databricks-llama-4-maverick` for tool-calling / general reasoning
- **MLflow 3** — Tracing, model logging, and GenAI evaluation with governance scorers via `ResponsesAgent`
- **Dash** (Plotly) — Interactive demo web app with `dash_bootstrap_components` (DARKLY theme)
- **Databricks Asset Bundles** — One-command deployment (`databricks bundle deploy`) for both the web app and pipeline job
