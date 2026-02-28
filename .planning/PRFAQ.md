# PRFAQ — GraphRAG

Working Backwards artifact. Written as if GraphRAG has shipped. All requirements and roadmap phases must be traceable to the customer outcome described here.

---

## Five Questions

### 1. Who is the customer?

Data and AI engineers, ML practitioners, compliance teams, product leaders, and software engineering teams at enterprises building LLM-powered applications for regulated or high-stakes domains — financial services, healthcare, legal, government, and any organization where AI decisions must be explainable, auditable, and reproducible. This includes developers and engineering leaders who need to understand code dependencies, trace the impact of changes, and audit AI-assisted code decisions. They work in Python/Databricks environments and need answers they can defend to regulators and stakeholders, not just answers that sound right.

### 2. What is the customer's problem or opportunity?

Enterprise AI today is a black box. When an LLM answers a question, no one can trace *why* it said what it said. Compliance teams cannot audit decisions. Regulators cannot verify that answers are grounded in approved data. The same question asked twice may produce different answers. And when the AI hallucinates, there is no mechanism to detect or prevent it.

Standard RAG (retrieval-augmented generation) retrieves flat text chunks by embedding similarity. This improves relevance, but the retrieval step itself is opaque — you cannot explain *which relationships* in the data led to the answer, or prove that the answer didn't hallucinate connections that don't exist.

The same opacity problem exists in code understanding. When an AI assistant says "this change will break service X," no one can trace *why*. Flat code search — whether embedding-based or keyword — cannot prove dependency chains. A code knowledge graph (symbols as nodes, calls/imports/inherits as edges) makes AI-assisted code reasoning auditable, just as a document knowledge graph makes enterprise Q&A auditable.

The opportunity: if the retrieval layer is structured as a knowledge graph (entities as nodes, relationships as edges, every edge traced to a source document or source file), the AI's reasoning becomes **auditable by design**. Every claim traces to a provenance chain. Every path is reproducible. Every answer can be verified. This applies equally to document corpora and codebases — the architecture is domain-agnostic.

### 3. What is the most critical customer benefit?

**Auditable, traceable, reproducible reasoning.** Every answer includes a structured provenance chain showing exactly which entities, relationships, and source documents contributed. Compliance teams can audit any decision. The same query returns the same path. Hallucinations are structurally prevented because the retrieval is constrained to the graph — the AI can only reason over evidence that exists.

This is not just "better answers." This is the difference between AI that enterprises *can* deploy in regulated environments and AI that they *cannot*.

### 4. How do we know what customers want or need?

- Regulated industries (banking, insurance, healthcare) are blocked from deploying LLM applications because they cannot prove to auditors that answers are grounded, deterministic, and traceable.
- Enterprise AI governance frameworks (EU AI Act, NIST AI RMF, OCC/FFIEC guidance) increasingly require explainability and auditability for AI-assisted decisions.
- Practitioner feedback: "Our legal team won't sign off on an LLM that can't show its reasoning chain."
- Multi-hop QA benchmarks consistently show flat RAG fails on synthesis questions — but more importantly, flat RAG *cannot prove it didn't hallucinate* even when it gets the right answer.
- Microsoft's GraphRAG research (2024) validated graph-based retrieval improves quality — we extend this with provenance, reproducibility, and governance as first-class outcomes.

### 5. What does the customer experience look like?

1. **Ingest:** Point GraphRAG at a data source. Extraction is domain-specific:
   - **Documents** (enterprise knowledge bases, legal texts, regulatory filings): Point at a UC Volume or a list of paths. The pipeline extracts entities and relationships via LLM (`ai_query()` with structured output).
   - **Codebases** (repositories, services, monorepos): Point at a repository. The pipeline parses source files deterministically (Tree-sitter / AST analysis) to extract symbols, call graphs, imports, and inheritance hierarchies. No LLM needed for extraction.
   Everything downstream — graph storage, traversal tools, agent, provenance, governance — is identical regardless of the source domain.
2. **Query:** Ask a question in natural language. GraphRAG traverses the knowledge graph, retrieves connected evidence with full provenance, and passes it to the LLM.
3. **Auditable answers:** The LLM returns an answer with a structured **Provenance** section: the exact entity path traversed, every source citation, and a grounding indicator confirming whether all claims are backed by the graph.
4. **Governance:** Every query is traced via MLflow. Compliance teams can audit who asked what, which data contributed, and whether the answer was fully grounded or partially relied on parametric knowledge.
5. **Reproducibility:** The same query over the same graph returns the same path and citations — deterministic by design.

---

## Press Release

**FOR IMMEDIATE RELEASE**

### GraphRAG: Auditable, Traceable AI Reasoning on Your Data — Built on Databricks

*Every answer shows its work. Every decision can be audited. Every claim is grounded in your data.*

**[City, Date]** — Today we announce GraphRAG, a Databricks-native pipeline that transforms document collections and codebases into queryable knowledge graphs — enabling LLM-powered reasoning that is auditable, traceable, and reproducible by design.

Enterprises deploying AI face a governance crisis: when an LLM makes a recommendation, no one can prove *why*. Compliance teams cannot audit the reasoning. Regulators cannot verify that answers are grounded in approved data. And the same question asked twice may produce different answers. These are not edge cases — they are blockers preventing responsible AI deployment in every regulated industry.

GraphRAG solves this by replacing opaque embedding retrieval with structured graph traversal. Every answer includes a provenance chain showing exactly which entities, relationships, and source documents contributed. The reasoning path is explicit, reproducible, and auditable.

With GraphRAG on Databricks, enterprises can:

- **Audit every AI decision** — Each answer includes a structured provenance section: the entity path traversed, the source documents cited, and a grounding indicator.
- **Trace lineage end-to-end** — From the question, through graph traversal, to the specific data that informed the answer. MLflow traces capture every step.
- **Reproduce results deterministically** — The same query over the same knowledge graph returns the same path and citations. No probabilistic drift.
- **Prevent hallucinations structurally** — The LLM reasons over graph-retrieved evidence, not parametric guesses. Claims not backed by the graph are flagged.
- **Understand code impact with traceable dependency chains** — AI-assisted code reasoning backed by deterministic graph traversal over parsed repositories. "What breaks if I change this function?" answered with a provable dependency path.
- **Evaluate governance metrics** — Built-in MLflow scorers measure hallucination rate, citation completeness, provenance quality, and reproducibility alongside traditional quality metrics.

"We couldn't deploy our AI advisor because compliance required us to explain every recommendation. With GraphRAG, every answer traces back to the source data. Our auditors signed off in a week," said a member of an early design review.

GraphRAG is built entirely on Databricks-native components: Unity Catalog, Delta Lake, Model Serving, and MLflow. Data never leaves the customer's workspace. No external APIs, no separate graph databases, no vendor lock-in.

And because structured retrieval replaces massive context windows, the cost per query drops by 90% or more compared to external LLM API pricing — making governance not just possible, but economical.

---

## FAQ

**Q: How does GraphRAG make AI auditable?**
A: Every answer includes a Provenance section with the explicit entity path the agent traversed, every source citation used as evidence, and a grounding indicator. MLflow traces capture the full reasoning chain for compliance review.

**Q: Can I reproduce the same answer for the same question?**
A: Yes. Graph traversal is deterministic — the same query over the same graph returns the same path and citations. This is fundamentally different from embedding-based retrieval, where results can shift with index updates or model changes.

**Q: How does it prevent hallucinations?**
A: The agent reasons over graph-retrieved evidence, not parametric knowledge. The system prompt instructs the agent to explicitly flag when information comes from outside the knowledge graph. Evaluation scorers measure hallucination rate automatically.

**Q: Does this require a graph database like Neo4j?**
A: Not for v1. The knowledge graph is stored in Delta tables in Unity Catalog — no additional infrastructure or licenses needed. As the graph scales, the backend can evolve: Lakebase (Postgres-compatible OLTP) for lower-latency serving, or a dedicated graph engine (Memgraph, Neo4j, FalkorDB) for richer traversal patterns. The graph schema and agent layer remain the same regardless of backend.

**Q: How does extraction differ for documents vs codebases?**
A: Documents use LLM extraction (`ai_query()` with structured output) because relationships in text are semantic — the LLM must interpret that "the LORD said unto Moses" means SPOKE_TO. Codebases use deterministic parsing (Tree-sitter, AST analysis) because relationships are structural — calls, imports, and inheritance are explicit in the syntax. The graph schema, traversal tools, agent, and governance layers are identical for both domains.

**Q: Why is the Bible used as the demo corpus?**
A: The Bible is a proxy for large, densely cross-referenced document collections and codebases. It has verifiable ground truth — genealogies, geographic movements, event sequences — that lets us validate the provenance chain works correctly before applying the pattern to enterprise documents or code repositories. The entity/relationship density (hundreds of interconnected people, places, and events across books) mirrors the complexity of real-world enterprise knowledge bases and large codebases.

**Q: How does it compare to Microsoft's GraphRAG?**
A: We're inspired by the same research. Our implementation adds three capabilities the original doesn't address: (1) structured provenance output for auditability, (2) governance-focused evaluation scorers, and (3) native integration with Databricks Unity Catalog, MLflow, and Model Serving.

**Q: What kinds of questions does GraphRAG handle better than flat RAG?**
A: Multi-hop questions (e.g. "How is Ruth connected to Jesus?"), cross-document synthesis, relationship tracing, and any question where the answer depends on connections between entities rather than a single passage. Critically, GraphRAG can *prove* its answer is grounded — flat RAG cannot.

**Q: What about policy enforcement?**
A: The provenance chain enables policy enforcement: downstream systems can validate that the reasoning path complies with business rules before surfacing the answer to users. This is a foundation for automated compliance checks.

**Q: How accurate is the entity/relationship extraction?**
A: Extraction quality depends on the LLM used and the domain. We provide evaluation tooling (MLflow scorers) to measure extraction quality, retrieval quality, and governance metrics separately, so users can tune each.

**Q: What about cost?**
A: Because structured retrieval replaces large context windows, cost per query drops significantly. On Databricks Foundation Model APIs, GraphRAG queries cost 90-900x less than equivalent OpenAI GPT-4 queries. But cost is a secondary benefit — the primary value is governance.

**Q: Can I bring my own LLM?**
A: Yes. Any model served through Databricks Model Serving (including external models via the AI Gateway) can be used for extraction and answer generation.

**Q: What is out of scope for v1?**
A: Codebase ingestion via deterministic parsing, Lakebase or dedicated graph engine backends, a UI, multi-tenant auth, real-time streaming ingestion, automated policy enforcement rules, and production ops tooling are out of scope for v1. The focus is a clean, well-tested pipeline with governance-first evaluation using the Bible corpus as a proxy dataset.
