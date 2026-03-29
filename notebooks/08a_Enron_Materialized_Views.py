# Databricks notebook source
# MAGIC %md
# MAGIC # 08a — Enron materialized views (serving layer)
# MAGIC
# MAGIC Pre-join / pre-aggregate **materialized views** in `graphrag_enron` for lower-latency reads.
# MAGIC
# MAGIC Requires a SQL warehouse / serverless SQL that supports `CREATE MATERIALIZED VIEW` for your catalog.

# COMMAND ----------

# DBTITLE 1,Load configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Build MVs
catalog = config["catalog"]
schema = config["enron_schema"]
fqn = f"{catalog}.{schema}"

entities = f"{fqn}.entities"
relationships = f"{fqn}.relationships"
entity_mentions = f"{fqn}.entity_mentions"
emails = f"{fqn}.emails"
org_hierarchy = f"{fqn}.org_hierarchy"
data_quality = f"{fqn}.data_quality_report"
entity_analytics = f"{fqn}.entity_analytics"
person_activity = f"{fqn}.person_activity"
topic_taxonomy = f"{fqn}.topic_taxonomy"

spark.sql(
    f"""
CREATE OR REPLACE MATERIALIZED VIEW {fqn}.mv_entity_profiles AS
WITH rel_src AS (
  SELECT source_entity AS entity_id, COUNT(*) AS rel_out
  FROM {relationships}
  GROUP BY source_entity
),
rel_tgt AS (
  SELECT target_entity AS entity_id, COUNT(*) AS rel_in
  FROM {relationships}
  GROUP BY target_entity
),
mentions AS (
  SELECT entity_id, COUNT(*) AS mention_count
  FROM {entity_mentions}
  GROUP BY entity_id
),
email_counts AS (
  SELECT sender AS entity_id, COUNT(*) AS email_count
  FROM {emails}
  WHERE sender IS NOT NULL
  GROUP BY sender
),
act AS (
  SELECT person_id AS entity_id, SUM(emails_sent) AS emails_sent_total
  FROM {person_activity}
  GROUP BY person_id
),
oh AS (
  SELECT person_id, title AS org_title, department AS org_department,
         ROW_NUMBER() OVER (PARTITION BY person_id ORDER BY effective_from DESC NULLS LAST) AS rn
  FROM {org_hierarchy}
)
SELECT
  e.entity_id,
  e.name,
  e.entity_type,
  e.description,
  e.first_mention_thread,
  e.first_mention_subject,
  ea.pagerank,
  ea.in_degree,
  ea.out_degree,
  ea.total_degree,
  COALESCE(rs.rel_out, CAST(0 AS BIGINT)) + COALESCE(rt.rel_in, CAST(0 AS BIGINT)) AS relationship_count,
  COALESCE(m.mention_count, CAST(0 AS BIGINT)) AS mention_count,
  COALESCE(ec.email_count, CAST(0 AS BIGINT)) AS email_count_direct,
  COALESCE(a.emails_sent_total, CAST(0 AS BIGINT)) AS emails_sent_from_activity,
  oh.org_title,
  oh.org_department
FROM {entities} e
LEFT JOIN {entity_analytics} ea ON e.entity_id = ea.entity_id
LEFT JOIN rel_src rs ON e.entity_id = rs.entity_id
LEFT JOIN rel_tgt rt ON e.entity_id = rt.entity_id
LEFT JOIN mentions m ON e.entity_id = m.entity_id
LEFT JOIN email_counts ec ON e.entity_id = ec.entity_id
LEFT JOIN act a ON e.entity_id = a.entity_id
LEFT JOIN oh ON e.entity_id = oh.person_id AND oh.rn = 1
"""
)
print(f"Created {fqn}.mv_entity_profiles")

spark.sql(
    f"""
CREATE OR REPLACE MATERIALIZED VIEW {fqn}.mv_corpus_stats AS
SELECT
  (SELECT COUNT(*) FROM {emails}) AS total_emails,
  (SELECT COUNT(*) FROM {entities}) AS total_entities,
  (SELECT MIN(date) FROM {emails}) AS min_email_date,
  (SELECT MAX(date) FROM {emails}) AS max_email_date,
  (SELECT COUNT(*) FROM {topic_taxonomy}) AS topic_taxonomy_rows,
  (SELECT topic_label FROM {topic_taxonomy}
     WHERE level = 1
     ORDER BY thread_count DESC NULLS LAST
     LIMIT 1) AS top_topic_by_threads
"""
)
print(f"Created {fqn}.mv_corpus_stats")

spark.sql(
    f"""
CREATE OR REPLACE MATERIALIZED VIEW {fqn}.mv_quality_summary AS
WITH ranked AS (
  SELECT
    table_name,
    refresh_date,
    total_rows,
    null_rate,
    cardinality_ratio,
    ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY refresh_date DESC NULLS LAST) AS rn
  FROM {data_quality}
)
SELECT table_name, refresh_date, total_rows, null_rate, cardinality_ratio
FROM ranked
WHERE rn = 1
"""
)
print(f"Created {fqn}.mv_quality_summary")

# COMMAND ----------

# DBTITLE 1,Row counts
for mv in ("mv_entity_profiles", "mv_corpus_stats", "mv_quality_summary"):
    c = spark.table(f"{fqn}.{mv}").count()
    print(f"{mv}: {c:,} rows")
