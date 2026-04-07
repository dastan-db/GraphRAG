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
# MAGIC This legacy notebook demonstrates the original GraphRAG walkthrough on a historical reference corpus. The active product architecture has since narrowed to the Enron use case, but the same graph-retrieval ideas still apply.
# MAGIC
# MAGIC ### Pipeline Overview
# MAGIC
# MAGIC 1. **Data Prep** (Notebook 01): Load source documents into Delta tables
# MAGIC 2. **Knowledge Graph** (Notebook 02): LLM extracts entities and relationships
# MAGIC 3. **Agent** (Notebook 03): Build a LangGraph agent with graph traversal tools
# MAGIC 4. **Demo** (Notebook 04): Ask multi-hop questions, see the agent reason over the graph
# MAGIC 5. **Evaluation** (Notebook 05): Governance evaluation (hallucination, provenance, reproducibility), quality comparison (GraphRAG vs flat RAG, small vs large model), and cost analysis
# MAGIC
# MAGIC ### Applying This Pattern to Your Domain
# MAGIC
# MAGIC This historical corpus is only a stand-in. The same architecture applies wherever entities have dense relationships:
# MAGIC
# MAGIC | Reference Corpus | Code / Architecture | Supply Chain |
# MAGIC |------------------|---------------------|--------------|
# MAGIC | Person → Person | Service → Service (CALLS) | Supplier → Warehouse (SHIPS_TO) |
# MAGIC | Person → Place | Module → Repo (DEPLOYED_TO) | Product → Region (DISTRIBUTED_IN) |
# MAGIC | *"Who communicated most frequently with Kenneth Lay?"* | *"What breaks if we change this schema?"* | *"Which customers are affected by this delay?"* |
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
print(f"Legacy corpus tags: {', '.join(config['bible_books'].keys())}")

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
