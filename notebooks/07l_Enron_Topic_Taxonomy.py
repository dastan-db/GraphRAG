# Databricks notebook source
# MAGIC %md
# MAGIC # 07l — Enron Topic Taxonomy (M6)
# MAGIC
# MAGIC Hierarchical topic rollup from `threads.key_topics` with heuristic parent
# MAGIC categories and usage counts.
# MAGIC
# MAGIC **Table:** `topic_taxonomy`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import re

import pyspark.sql.functions as F
from pyspark.sql.types import StringType

# COMMAND ----------

# DBTITLE 1,Parent category from topic label
PARENT_EXPR = """
CASE
  WHEN topic_lower RLIKE 'california|energy|power|electricity|gas|pipeline' THEN 'Energy'
  WHEN topic_lower RLIKE 'enron stock|accounting|financial|earnings|debt' THEN 'Finance'
  WHEN topic_lower RLIKE 'litigation|sec|ferc|investigation|regulation' THEN 'Legal'
  WHEN topic_lower RLIKE 'broadband|trading|weather|risk' THEN 'Operations'
  WHEN topic_lower RLIKE 'compensation|benefits|organizational|layoff' THEN 'HR'
  WHEN topic_lower RLIKE 'political|government|lobbying|media' THEN 'External'
  ELSE 'Other'
END
"""


def _slug(s: str) -> str:
    if not s:
        return ""
    t = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return t[:200] if t else ""


slugify_topic = F.udf(_slug, StringType())

# COMMAND ----------

# DBTITLE 1,Build topic_taxonomy
threads_t = config["enron_threads_table"]
mentions_t = config["enron_entity_mentions_table"]
out_t = config["enron_topic_taxonomy_table"]

base = (
    spark.table(threads_t)
    .filter(F.col("key_topics").isNotNull() & (F.size(F.col("key_topics")) > 0))
    .select("thread_id", F.explode("key_topics").alias("topic_label"))
    .filter(F.length(F.trim(F.col("topic_label"))) > 0)
    .withColumn("topic_lower", F.lower(F.trim(F.col("topic_label"))))
    .withColumn("parent_label", F.expr(PARENT_EXPR))
    .withColumn(
        "parent_topic_id",
        F.concat(
            F.lit("cat_"),
            F.lower(F.regexp_replace(F.col("parent_label"), r"\s+", "_")),
        ),
    )
)

leaf_agg = base.groupBy("topic_label", "parent_label", "parent_topic_id").agg(
    F.countDistinct("thread_id").alias("thread_count")
)

em = spark.table(mentions_t).select("thread_id", "entity_id").distinct()
ent_by_topic = (
    base.select("thread_id", "topic_label").distinct()
    .join(em, "thread_id", "inner")
    .groupBy("topic_label")
    .agg(F.countDistinct("entity_id").alias("entity_count"))
)

leaf = (
    leaf_agg.join(ent_by_topic, "topic_label", "left")
    .withColumn("entity_count", F.coalesce(F.col("entity_count"), F.lit(0)))
    .withColumn("topic_id", F.concat(F.lit("topic_"), slugify_topic(F.col("topic_label"))))
    .withColumn("level", F.lit(1))
    .select(
        "topic_id",
        "topic_label",
        "parent_topic_id",
        "level",
        "thread_count",
        "entity_count",
    )
)

parents = (
    leaf.groupBy("parent_topic_id")
    .agg(F.sum("thread_count").alias("thread_count"), F.sum("entity_count").alias("entity_count"))
    .join(
        leaf.select("parent_topic_id", "parent_label").distinct(),
        "parent_topic_id",
        "inner",
    )
    .select(
        F.col("parent_topic_id").alias("topic_id"),
        F.col("parent_label").alias("topic_label"),
        F.lit(None).cast("string").alias("parent_topic_id"),
        F.lit(0).alias("level"),
        "thread_count",
        "entity_count",
    )
)

out_df = parents.unionByName(leaf)
out_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)

n = spark.table(out_t).count()
print(f"topic_taxonomy: {n:,} rows → {out_t}")
