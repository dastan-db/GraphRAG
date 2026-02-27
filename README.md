# GraphRAG: Auditable AI Reasoning on Databricks

A Databricks Solution Accelerator that demonstrates **GraphRAG** — using a knowledge graph to deliver LLM-powered answers that are **auditable, traceable, and reproducible**. Every answer shows its work: explicit provenance chains, verse-level citations, and grounding indicators.

## The Problem

Enterprise AI has a governance crisis. When an LLM answers a question, no one can prove *why* it said what it said:

- **"Why did the AI recommend this?"** — You can't trace the reasoning path.
- **"Which data led to this outcome?"** — Embedding retrieval is a black box.
- **"I got answer A yesterday, why do I get answer B today?"** — Results aren't reproducible.
- **"Did the AI make this up?"** — You can't distinguish grounded claims from hallucinations.
- **"This decision violates our policy"** — There's no mechanism to audit or enforce.

Standard RAG (retrieval-augmented generation) retrieves flat text chunks by embedding similarity. This improves relevance, but the retrieval step itself is opaque. You cannot explain which relationships in the data led to the answer, or prove that the answer didn't hallucinate connections that don't exist.

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

## Enterprise Use Case

The Bible demo maps directly to regulated enterprise scenarios. Here's the equivalent:

```
Enterprise scenario (credit decision):

Without GraphRAG:
  Q: "Why was this customer denied credit?"
  AI: "Your credit score is too low."
  Compliance: "Which rules triggered this? Show me the policy chain."
  AI: "I can't trace my reasoning."
  Compliance: "Not acceptable for regulated decisions."

With GraphRAG on Databricks:
  Q: "Why was this customer denied credit?"
  AI: "Rule 207.3 triggered because credit score (650) < threshold (700)
       and debt-to-income ratio (45%) > threshold (40%)."

  Provenance:
    Path: Customer → Credit Bureau Feed → Rule 207.3 → Decision (Denied)
    Sources: Customer record #4821, Policy v3.2 §207.3, Credit bureau pull 2026-02-15
    Grounding: All claims backed by knowledge graph traversal

  Compliance: "Auditable. Compliant. Reproducible." ✓
```

## Demo: Bible Knowledge Graph

This accelerator builds a knowledge graph from five books of the King James Bible — chosen for their dense, cross-referencing network of people, places, and events. The Bible is the perfect proxy because lineage is verifiable: "How is Ruth connected to Jesus?" has a definitive, provably correct answer.

| Book | Testament | Why Selected |
|------|-----------|--------------|
| Genesis | OT | Foundational: Abraham, Isaac, Jacob, Joseph, creation, covenant |
| Exodus | OT | Continues Genesis: Moses, plagues, Red Sea, Sinai, the Law |
| Ruth | OT | Bridge book: connects patriarchs to David's lineage (and to Jesus) |
| Matthew | NT | Genealogy traces Jesus back to Abraham/David/Ruth/Boaz |
| Acts | NT | References Abraham, Moses, David; Peter and Paul's missions |

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
| `00_Intro_and_Config` | Configuration and setup |
| `01_Data_Prep` | Load Bible text into Delta tables |
| `02_Build_Knowledge_Graph` | LLM extracts entities and relationships |
| `03_Build_Agent` | Build LangGraph agent with graph traversal tools |
| `04_Query_Demo` | Interactive demo with auditable, multi-hop answers |
| `05_Evaluation` | Governance + quality + cost comparison: GraphRAG vs flat RAG vs direct LLM |
| `RUNME` | Creates a Databricks Workflow for the full pipeline |

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

### Quality Scorers

Correctness, RelevanceToQuery, Grounded Reasoning, Multi-hop Reasoning, Verse Citation.

### Cost Analysis

Compares Databricks Foundation Model API pricing against OpenAI GPT-4. Structured retrieval replaces massive context windows, yielding 90-900x cost savings — making governance not just possible, but economical.

## Applying This Pattern to Your Domain

This demo uses the Bible as a corpus, but the pattern applies directly to any domain with dense entity relationships:

| Bible Domain | Code/Architecture Domain | Supply Chain Domain |
|---|---|---|
| Person (Moses, Paul) | Module, Class, Service | Supplier, Warehouse |
| Place (Egypt, Jerusalem) | Repository, Deployment Target | Region, Distribution Center |
| Event (Exodus, Pentecost) | Release, Incident, Migration | Order, Shipment, Outage |
| FAMILY_OF, ANCESTOR_OF | IMPORTS, INHERITS_FROM | SUPPLIES_TO, SOURCES_FROM |
| TRAVELED_TO | DEPLOYS_TO, CALLS | SHIPS_TO, ROUTES_THROUGH |
| PARTICIPATED_IN | DEPENDS_ON, CONTRIBUTES_TO | PARTICIPATES_IN, FULFILLS |
| *"How is Ruth connected to Jesus?"* | *"What services depend on this schema change?"* | *"Which customers are affected if Supplier X is delayed?"* |
| *"Prove it — show the path and verses"* | *"Prove it — show the dependency chain and commits"* | *"Prove it — show the supply chain and contracts"* |

The value of graph-structured retrieval increases with the density of cross-entity relationships. Flat embedding retrieval breaks down precisely where graph traversal excels: multi-hop reasoning, cross-document connections, and path tracing between entities. And only graph retrieval can **prove** its answer is grounded.

## Privacy and Data Sovereignty

This architecture runs entirely within the Databricks platform — no data leaves the customer's environment:

```
┌─────────────────────────────────────────────────────────┐
│  CUSTOMER'S DATABRICKS WORKSPACE                        │
│                                                         │
│  ┌──────────┐   ┌────────────────┐   ┌──────────────┐  │
│  │ Documents │──▶│ Knowledge Graph │──▶│ LangGraph    │  │
│  │ (UC/Delta)│   │ (Delta Tables) │   │ Agent        │  │
│  └──────────┘   └────────────────┘   └──────┬───────┘  │
│                                              │          │
│                                     ┌────────▼───────┐  │
│                                     │ Foundation     │  │
│                                     │ Model API      │  │
│                                     │ (Mosaic AI)    │  │
│                                     └────────────────┘  │
│                                                         │
│  Everything stays here. No external API calls.          │
│  Every query traced via MLflow. Fully auditable.        │
└─────────────────────────────────────────────────────────┘
```

With Databricks Mosaic AI:
- **Data stays in Unity Catalog** — governed, auditable, never transmitted externally
- **Every decision is traceable** — MLflow traces capture the full reasoning chain
- **Models run on your compute** — Foundation Model API or provisioned throughput endpoints
- **Results are reproducible** — deterministic graph traversal, not probabilistic embedding drift
- **No vendor lock-in** — swap models (Llama, DBRX, Mixtral) without changing the pipeline

## Tech Stack

- **Databricks** — Unity Catalog, Delta Lake, Model Serving, MLflow
- **LangGraph** — Agent orchestration with tool-calling
- **Foundation Model API** — `databricks-meta-llama-3-3-70b-instruct` (70B) and `databricks-meta-llama-3-1-8b-instruct` (8B)
- **MLflow 3** — Tracing, model logging, and GenAI evaluation with governance scorers via `ResponsesAgent`
