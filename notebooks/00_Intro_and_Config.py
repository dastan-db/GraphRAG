# Databricks notebook source
# MAGIC %md
# MAGIC # GraphRAG: Graph-Powered RAG on Databricks
# MAGIC
# MAGIC ## Introduction
# MAGIC
# MAGIC Standard RAG retrieves flat text chunks by embedding similarity. This approach loses the **relationships** between entities — people, places, events, concepts — that give answers their meaning. Answers end up locally accurate but lack broader reasoning: wrong attributions, missed cross-document connections, hallucinated context.
# MAGIC
# MAGIC **GraphRAG** adds a graph layer: entities and relationships extracted from documents become a structured index. An LLM-powered agent traverses this graph to answer questions that require multi-hop reasoning across the corpus.
# MAGIC
# MAGIC ### This Demo
# MAGIC
# MAGIC We demonstrate GraphRAG using five books from the **King James Bible** — chosen for their dense, cross-referencing network of people, places, and events:
# MAGIC
# MAGIC | Book | Testament | Chapters | Why Selected |
# MAGIC |------|-----------|----------|--------------|
# MAGIC | **Genesis** | OT | 50 | Foundational: Abraham, Isaac, Jacob, Joseph, creation, covenant |
# MAGIC | **Exodus** | OT | 40 | Continues Genesis: Moses, plagues, Law, Sinai |
# MAGIC | **Ruth** | OT | 4 | Bridge book: connects patriarchs to David's lineage (→ Jesus) |
# MAGIC | **Matthew** | NT | 28 | Genealogy traces Jesus to Abraham/David/Ruth; constant OT quotes |
# MAGIC | **Acts** | NT | 28 | References Abraham, Moses, David; Peter and Paul's missions |
# MAGIC
# MAGIC ### Pipeline Overview
# MAGIC
# MAGIC 1. **Data Prep** (Notebook 01): Load Bible text into Delta tables
# MAGIC 2. **Knowledge Graph** (Notebook 02): LLM extracts entities and relationships
# MAGIC 3. **Agent** (Notebook 03): Build a LangGraph agent with graph traversal tools
# MAGIC 4. **Demo** (Notebook 04): Ask multi-hop questions, see the agent reason over the graph
# MAGIC 5. **Evaluation** (Notebook 05): Governance evaluation (hallucination, provenance, reproducibility), quality comparison (GraphRAG vs flat RAG, small vs large model), and cost analysis
# MAGIC
# MAGIC ### Applying This Pattern to Your Domain
# MAGIC
# MAGIC The Bible is a proxy corpus. This same architecture applies wherever entities have dense relationships:
# MAGIC
# MAGIC | Bible | Code / Architecture | Supply Chain |
# MAGIC |-------|---------------------|--------------|
# MAGIC | Person → Person (FAMILY_OF) | Service → Service (CALLS) | Supplier → Warehouse (SHIPS_TO) |
# MAGIC | Person → Place (TRAVELED_TO) | Module → Repo (DEPLOYED_TO) | Product → Region (DISTRIBUTED_IN) |
# MAGIC | *"How is Ruth connected to Jesus?"* | *"What breaks if we change this schema?"* | *"Which customers are affected by this delay?"* |
# MAGIC
# MAGIC The insight: **structure (graph) matters more than model size** for multi-hop reasoning tasks. A small model with graph retrieval can match a large model without it — at a fraction of the cost.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration Values
# MAGIC
# MAGIC The following settings control the accelerator. Modify `src/config.py` to change catalog, schema, or LLM endpoint.

# COMMAND ----------

print(f"Catalog:        {config['catalog']}")
print(f"Schema:         {config['schema']}")
print(f"LLM Endpoint:   {config['llm_endpoint']}")
print(f"Bible Books:    {', '.join(config['bible_books'].keys())}")

# COMMAND ----------

# DBTITLE 1,Pass Config to Downstream Tasks
try:
    dbutils.jobs.taskValues.set('catalog', config['catalog'])
    dbutils.jobs.taskValues.set('schema', config['schema'])
    dbutils.jobs.taskValues.set('llm_endpoint', config['llm_endpoint'])
except:
    pass

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC &copy; 2026 Databricks, Inc. All rights reserved.
