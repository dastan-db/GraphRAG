# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Query Demo: Auditable AI Reasoning on the Bible Knowledge Graph
# MAGIC
# MAGIC This notebook demonstrates GraphRAG answering multi-hop questions with **full auditability**: every answer includes a structured **Provenance** section showing the exact entity path traversed, the source verses cited, and whether all claims are grounded in the knowledge graph.
# MAGIC
# MAGIC Watch the agent use its tools — `find_entity`, `find_connections`, `trace_path`, `get_context_verses`, and `get_entity_summary` — then produce answers that an auditor can verify claim by claim.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U mlflow>=3.0 databricks-langchain langgraph>=0.3.4 databricks-agents pydantic --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration, Tools, and Agent
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %run ../src/agent/tools

# COMMAND ----------

# MAGIC %run ../src/agent/agent

# COMMAND ----------

# DBTITLE 1,Helper: Ask and Display
from mlflow.types.responses import ResponsesAgentRequest
import mlflow

def ask(question: str):
    """Ask the GraphRAG agent a question and display the answer."""
    print(f"Question: {question}")
    print("=" * 80)
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": question}]
    )
    result = AGENT.predict(request)
    for item in result.output:
        if hasattr(item, 'text'):
            print(item.text)
        elif hasattr(item, 'name'):
            print(f"\n[Tool call: {item.name}({item.arguments})]")
    print()
    return result

# COMMAND ----------

# MAGIC %md
# MAGIC ## Knowledge Graph Overview
# MAGIC
# MAGIC Before querying, let's see what the knowledge graph contains.

# COMMAND ----------

# DBTITLE 1,Graph Statistics
import pyspark.sql.functions as F

entities_count = spark.table(config['entities_table']).count()
relationships_count = spark.table(config['relationships_table']).count()
verses_count = spark.table(config['verses_table']).count()

print(f"Verses:        {verses_count:,}")
print(f"Entities:      {entities_count:,}")
print(f"Relationships: {relationships_count:,}")

# COMMAND ----------

# DBTITLE 1,Entity Type Distribution
display(
    spark.table(config['entities_table'])
    .groupBy("entity_type")
    .count()
    .orderBy(F.desc("count"))
)

# COMMAND ----------

# DBTITLE 1,Top 15 Most Connected Entities
display(
    spark.sql(f"""
        WITH all_mentions AS (
            SELECT source_entity as entity_id FROM {config['relationships_table']}
            UNION ALL
            SELECT target_entity as entity_id FROM {config['relationships_table']}
        )
        SELECT e.name, e.entity_type, COUNT(*) as connection_count
        FROM all_mentions a
        JOIN {config['entities_table']} e ON a.entity_id = e.entity_id
        GROUP BY e.name, e.entity_type
        ORDER BY connection_count DESC
        LIMIT 15
    """)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Demo Queries
# MAGIC
# MAGIC ### Query 1: Multi-hop Lineage (The Flagship Audit Test)
# MAGIC *"How is Ruth connected to Jesus?"*
# MAGIC
# MAGIC This requires tracing: Ruth → Boaz → Obed → Jesse → David → ... → Jesus.
# MAGIC The agent should use `trace_path` and `find_connections` to build the chain.
# MAGIC
# MAGIC **What to look for:** The **Provenance** section at the end of the response. This is what an enterprise auditor sees — the explicit path, every citation, and the grounding indicator. This is the difference between "the AI said so" and "here's the proof."

# COMMAND ----------

# DBTITLE 1,Q1: How is Ruth connected to Jesus?
result_1 = ask("How is Ruth connected to Jesus? Trace the lineage step by step.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query 2: Cross-Book Entity Overlap
# MAGIC *"Which people appear in both Genesis and Exodus?"*
# MAGIC
# MAGIC The agent should search for entities that appear across both books, identifying
# MAGIC figures like Joseph, Jacob's sons, and God who bridge the two narratives.

# COMMAND ----------

# DBTITLE 1,Q2: People in Both Genesis and Exodus
result_2 = ask("Which people appear in both Genesis and Exodus? List them and explain their role in each book.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query 3: Event Chain
# MAGIC *"Trace the journey of the Israelites from Egypt to the Promised Land."*
# MAGIC
# MAGIC This requires understanding the event sequence: slavery → plagues → Exodus →
# MAGIC Red Sea → wilderness → Sinai → Law. The agent should use multiple tools.

# COMMAND ----------

# DBTITLE 1,Q3: Journey from Egypt to the Promised Land
result_3 = ask("Trace the journey of the Israelites from Egypt to the Promised Land. What were the key events and who led them?")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query 4: Cross-Testament Connections
# MAGIC *"What role does Moses play across the Old and New Testament?"*
# MAGIC
# MAGIC Moses is central in Exodus and heavily referenced in Matthew and Acts.
# MAGIC The agent should trace his presence across both testaments.

# COMMAND ----------

# DBTITLE 1,Q4: Moses Across Testaments
result_4 = ask("What role does Moses play across the Old and New Testament books in our knowledge graph? Cite specific verses.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query 5: Comparative Analysis
# MAGIC *"Compare the leadership styles of Moses and Paul."*
# MAGIC
# MAGIC This requires profiling two entities and synthesizing a comparison — a true
# MAGIC multi-entity reasoning task.

# COMMAND ----------

# DBTITLE 1,Q5: Moses vs Paul Leadership
result_5 = ask("Compare the leadership styles of Moses and Paul based on their actions and relationships in the knowledge graph.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query 6: Place-Based Reasoning
# MAGIC *"What significant events happened in Egypt across the Bible?"*
# MAGIC
# MAGIC Egypt spans Genesis (Joseph), Exodus (slavery/plagues), Matthew (flight), and Acts (references).

# COMMAND ----------

# DBTITLE 1,Q6: Egypt Across the Bible
result_6 = ask("What significant events happened in Egypt across all the books in our knowledge graph?")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## MLflow Traces
# MAGIC
# MAGIC Every agent interaction is automatically traced by MLflow. Check the **Experiments** tab
# MAGIC in the sidebar to see full traces including:
# MAGIC - Each tool call and its results
# MAGIC - LLM prompts and completions
# MAGIC - End-to-end latency and token counts

# COMMAND ----------

# DBTITLE 1,Recent Traces
active_run = mlflow.active_run()
if active_run:
    experiment_id = active_run.info.experiment_id
    print(f"Experiment ID: {experiment_id}")
    print(f"View traces in the MLflow UI: Experiments > {experiment_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC
# MAGIC ## Summary
# MAGIC
# MAGIC This demo showed how GraphRAG delivers what enterprise AI requires: **auditable, traceable, reproducible reasoning.**
# MAGIC
# MAGIC | Governance Property | Flat RAG | GraphRAG |
# MAGIC |---|---|---|
# MAGIC | **Provenance chain** | No structured path — retrieval is opaque | Explicit entity path with relationship types and source citations |
# MAGIC | **Auditability** | Cannot trace why the answer was given | Every claim links to a verse; the full reasoning chain is in MLflow traces |
# MAGIC | **Reproducibility** | Embedding similarity can drift | Deterministic graph traversal returns the same path every time |
# MAGIC | **Hallucination detection** | Cannot distinguish grounded from invented claims | Grounding indicator flags when claims come from outside the graph |
# MAGIC | **Multi-hop reasoning** | Misses intermediate connections | Traces the full path through the knowledge graph |
# MAGIC | **Cross-document synthesis** | Returns chunks from one source only | Traverses relationships across all five books structurally |
# MAGIC
# MAGIC Every answer is **grounded** in the knowledge graph, **traceable** to specific Bible verses, and **reproducible** on demand.
# MAGIC
# MAGIC ---
# MAGIC &copy; 2026 Databricks, Inc. All rights reserved.
