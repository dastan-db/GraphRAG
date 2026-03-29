# Databricks notebook source
# MAGIC %md
# MAGIC # 07m — Enron Pipeline Lineage (M7)
# MAGIC
# MAGIC Curated DAG of table-to-table transformations (documentation / governance).
# MAGIC
# MAGIC **Table:** `pipeline_lineage`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Write pipeline_lineage
out_t = config["enron_pipeline_lineage_table"]

rows = [
    ("emails", "threads", "07_Data_Prep", "Aggregation by thread_id into thread_text and metadata"),
    ("emails", "participants", "06_Data_Prep", "Extract sender and recipient addresses"),
    ("threads", "entities", "07_KG", "ai_query entity extraction from thread text"),
    ("threads", "relationships", "07_KG", "ai_query relationship extraction"),
    ("entities", "entity_aliases", "07_KG", "Entity resolution and canonical alias mapping"),
    (
        "entities",
        "entity_analytics",
        "07b",
        "Graph centrality over entities with entity_aliases resolution",
    ),
    (
        "entities",
        "entity_paths",
        "07b",
        "BFS shortest paths over resolved entity graph (uses entity_aliases)",
    ),
    (
        "threads",
        "entity_mentions",
        "07_KG",
        "Thread–entity join for per-email mention traceability",
    ),
    (
        "emails",
        "communication_dyads",
        "07c",
        "Sender/recipient pair aggregation (uses participants expansion)",
    ),
    (
        "emails",
        "person_activity",
        "07c",
        "Per-person activity aggregation (uses participants expansion)",
    ),
    (
        "entity_aliases",
        "person_identity",
        "07e",
        "Identity resolution joining entity_aliases with participants",
    ),
    (
        "participants",
        "person_identity",
        "07e",
        "Identity resolution joining entity_aliases with participants",
    ),
    ("emails", "email_classification", "07i", "Heuristic email type and metadata flags"),
    (
        "threads",
        "extraction_provenance",
        "07h",
        "Retroactive provenance built from threads, entities, and relationships",
    ),
]

import pyspark.sql.functions as F
from pyspark.sql.types import StringType, StructField, StructType

sch = StructType(
    [
        StructField("source_table", StringType()),
        StructField("target_table", StringType()),
        StructField("transformation_step", StringType()),
        StructField("sql_description", StringType()),
    ]
)

rdf = spark.createDataFrame(rows, sch).withColumn("last_run_at", F.current_timestamp())
rdf.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)

print(f"pipeline_lineage: {rdf.count()} rows → {out_t}")
