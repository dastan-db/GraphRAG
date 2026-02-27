# PRFAQ — GraphRAG

Working Backwards artifact. Written as if GraphRAG has shipped. All requirements and roadmap phases must be traceable to the customer outcome described here.

---

## Five Questions

### 1. Who is the customer?

Data and AI engineers, ML practitioners, and product teams at companies building document-heavy LLM applications — e.g. internal knowledge bases, regulatory document Q&A, research assistants. They work in Python/Databricks environments and are comfortable with SQL and LLM APIs, but frustrated with the quality ceiling of flat, chunk-based retrieval.

### 2. What is the customer's problem or opportunity?

Standard RAG retrieves flat text chunks by embedding similarity. This approach loses the **relationships** between entities (people, concepts, events, documents) that give answers their meaning. Customers get answers that are locally accurate but lack broader reasoning — wrong attributions, missed cross-document connections, hallucinated context.

The opportunity: if the retrieval layer is structured as a graph (entities as nodes, relationships as edges), the LLM can reason over connected evidence rather than disconnected passages.

### 3. What is the most critical customer benefit?

**Higher-quality, more trustworthy answers** on complex, multi-hop questions — without requiring the customer to become a graph database expert. The graph is built and queried automatically; the customer's interface is the same: ingest documents, ask questions, get answers.

### 4. How do we know what customers want or need?

- Multi-hop QA benchmarks consistently show flat RAG fails on questions requiring synthesis across multiple documents or reasoning about relationships (e.g. HotpotQA, MuSiQue).
- Practitioner feedback on LangChain/LlamaIndex forums: "My RAG answers single-document questions well but fails on anything that requires cross-referencing."
- Microsoft's GraphRAG research (2024) validated that graph-based retrieval improves answer quality on community-level questions compared to naive RAG.
- The customers we design for are already running Databricks; they have Delta tables, UC, and vector search available — they want a pipeline that wires these together without requiring a separate graph DB.

### 5. What does the customer experience look like?

1. **Ingest:** Point GraphRAG at a set of documents (e.g. a UC Volume or a list of paths). The pipeline extracts entities and relationships using an LLM, builds a knowledge graph, and stores it alongside a vector index.
2. **Query:** Ask a question in natural language. GraphRAG retrieves graph-aware context (relevant subgraphs + supporting passages) and passes it to the LLM.
3. **Answers:** The LLM returns an answer grounded in connected evidence, with source traceability (which documents, which entities, which relationships contributed).
4. **Iteration:** Customers can inspect the graph, add documents, and re-query — all through a Python API or a Databricks notebook.

---

## Press Release

**FOR IMMEDIATE RELEASE**

### GraphRAG: Bring Structured Reasoning to Your Document Q&A on Databricks

*Graph-powered retrieval that answers the questions flat RAG can't.*

**[City, Date]** — Today we announce GraphRAG, a Python-native pipeline for Databricks that transforms document collections into queryable knowledge graphs — enabling LLM-powered Q&A that reasons across relationships, not just individual passages.

Standard RAG pipelines answer questions well when the answer lives in a single passage. They fall short when users ask questions that require synthesizing information across multiple documents or reasoning about how entities relate to each other. GraphRAG solves this by adding a graph layer: entities and relationships extracted from documents become a structured index that the retrieval step traverses before calling the LLM.

With GraphRAG, teams using Databricks can:

- **Ingest any document set** into a knowledge graph stored in Unity Catalog Delta tables — no separate graph database required.
- **Query with natural language** and receive answers grounded in graph-aware context, including multi-hop reasoning.
- **Inspect and extend** the graph incrementally as new documents arrive.
- **Evaluate answer quality** using MLflow traces and built-in evaluation harnesses.

"We were getting 80% of the way there with vector search alone," said a member of an early design review. "The last 20% — the questions that actually matter in our domain — required reasoning across documents. GraphRAG closes that gap."

GraphRAG is built on Databricks-native components: Databricks Vector Search, Unity Catalog, Databricks Model Serving (for LLM calls), and MLflow for evaluation and tracing. It requires no additional infrastructure.

To get started: install the Python package, point it at a Databricks workspace, and run `graphrag.ingest(docs)` followed by `graphrag.query("your question")`.

---

## FAQ

**Q: Does this require a graph database like Neo4j?**  
A: No. The knowledge graph is stored in Delta tables in Unity Catalog. No additional infrastructure or licenses needed.

**Q: How does it compare to Microsoft's GraphRAG?**  
A: We're inspired by the same research. Our implementation is purpose-built for the Databricks ecosystem — using UC, Vector Search, and Model Serving — rather than requiring a standalone setup. We also integrate with MLflow for evaluation out of the box.

**Q: What kinds of questions does GraphRAG handle better than flat RAG?**  
A: Multi-hop questions (e.g. "What projects did Alice lead that involved the same vendor as Bob's 2023 initiative?"), community/cluster-level questions (e.g. "Summarize the main themes across all regulatory filings"), and relationship questions (e.g. "Which decisions in document A contradict claims in document B?").

**Q: How accurate is the entity/relationship extraction?**  
A: Extraction quality depends on the LLM used and the domain. We provide evaluation tooling (MLflow scorers) to measure extraction quality and retrieval quality separately, so users can tune both.

**Q: What happens when new documents arrive?**  
A: Incremental ingestion is supported. New entities and relationships are merged into the existing graph; the vector index is updated accordingly.

**Q: Can I bring my own LLM?**  
A: Yes. Any model served through Databricks Model Serving (including external models via the AI Gateway) can be used for extraction and answer generation.

**Q: What is out of scope for v1?**  
A: A UI, multi-tenant auth, real-time streaming ingestion, and production ops tooling are out of scope for v1. The focus is a clean, well-tested Python API and pipeline.
