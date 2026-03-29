# Databricks notebook source
# MAGIC %md
# MAGIC # 07h — Enron Extraction Provenance (M1)
# MAGIC
# MAGIC Retroactive provenance for the extraction pipeline: one row per thread per
# MAGIC extraction step (`entity_extraction`, `relationship_extraction`,
# MAGIC `thread_summarization`).
# MAGIC
# MAGIC **Table:** `extraction_provenance`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Build extraction_provenance
threads_t = config["enron_threads_table"]
mentions_t = config["enron_entity_mentions_table"]
rels_t = config["enron_relationships_table"]
out_t = config["enron_extraction_provenance_table"]
llm = config["llm_endpoint"].replace("'", "''")
small_llm = config.get("small_llm_endpoint", config["llm_endpoint"]).replace("'", "''")

spark.sql(f"""
CREATE OR REPLACE TABLE {out_t} AS
WITH entity_counts AS (
  SELECT thread_id, COUNT(DISTINCT entity_id) AS cnt
  FROM {mentions_t}
  GROUP BY thread_id
),
rel_exploded AS (
  SELECT e.thread_id
  FROM {rels_t} r
  LATERAL VIEW OUTER EXPLODE(r.source_threads) e AS thread_id
  WHERE r.source_threads IS NOT NULL AND SIZE(r.source_threads) > 0
),
rel_counts AS (
  SELECT thread_id, COUNT(*) AS cnt
  FROM rel_exploded
  GROUP BY thread_id
)
SELECT * FROM (
  SELECT
    uuid() AS extraction_id,
    t.thread_id,
    'entity_extraction' AS step,
    '{llm}' AS model_endpoint,
    'corporate_entity_v1' AS prompt_template_version,
    CAST(LENGTH(COALESCE(t.thread_text, '')) AS INT) AS input_char_count,
    CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 6000 THEN 6000 ELSE NULL END AS input_truncated_at,
    CAST(COALESCE(ec.cnt, 0) AS INT) AS output_entity_count,
    CAST(0 AS INT) AS output_rel_count,
    CAST(NULL AS STRING) AS error_message,
    CAST(NULL AS BIGINT) AS latency_ms,
    CURRENT_TIMESTAMP() AS created_at
  FROM {threads_t} t
  LEFT JOIN entity_counts ec ON t.thread_id = ec.thread_id

  UNION ALL

  SELECT
    uuid() AS extraction_id,
    t.thread_id,
    'relationship_extraction' AS step,
    '{llm}' AS model_endpoint,
    'corporate_relationship_v1' AS prompt_template_version,
    CAST(LENGTH(COALESCE(t.thread_text, '')) AS INT) AS input_char_count,
    CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 6000 THEN 6000 ELSE NULL END AS input_truncated_at,
    CAST(0 AS INT) AS output_entity_count,
    CAST(COALESCE(rc.cnt, 0) AS INT) AS output_rel_count,
    CAST(NULL AS STRING) AS error_message,
    CAST(NULL AS BIGINT) AS latency_ms,
    CURRENT_TIMESTAMP() AS created_at
  FROM {threads_t} t
  LEFT JOIN rel_counts rc ON t.thread_id = rc.thread_id

  UNION ALL

  SELECT
    uuid() AS extraction_id,
    t.thread_id,
    'thread_summarization' AS step,
    '{small_llm}' AS model_endpoint,
    'thread_summary_v1' AS prompt_template_version,
    CAST(LENGTH(COALESCE(t.thread_text, '')) AS INT) AS input_char_count,
    CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 4000 THEN 4000 ELSE NULL END AS input_truncated_at,
    CAST(NULL AS INT) AS output_entity_count,
    CAST(NULL AS INT) AS output_rel_count,
    CAST(NULL AS STRING) AS error_message,
    CAST(NULL AS BIGINT) AS latency_ms,
    CURRENT_TIMESTAMP() AS created_at
  FROM {threads_t} t
  WHERE t.summary IS NOT NULL
) q
""")

cnt = spark.table(out_t).count()
print(f"extraction_provenance: {cnt:,} rows → {out_t}")
