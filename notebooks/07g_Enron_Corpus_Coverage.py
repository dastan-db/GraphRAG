# Databricks notebook source
# MAGIC %md
# MAGIC # 07g — Enron Corpus Coverage
# MAGIC
# MAGIC Build **`corpus_coverage`** snapshot metrics for graph quality and corpus
# MAGIC completeness (threads, entities, relationships, org chart, custodian slice,
# MAGIC summarization).
# MAGIC
# MAGIC **Output table:** `{catalog}.{enron_schema}.corpus_coverage`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Reference tables
enron_schema = config["enron_schema"]
threads_t = config["enron_threads_table"]
entities_t = config["enron_entities_table"]
rels_t = config["enron_relationships_table"]
mentions_t = config["enron_entity_mentions_table"]
emails_t = config["enron_emails_table"]
org_t = config["enron_org_hierarchy_table"]
out_table = config["enron_corpus_coverage_table"]

custodian_pat = "|".join(config["enron_key_custodians"])

# COMMAND ----------

# DBTITLE 1,Compute metrics (single pass SQL)
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_tot_threads AS
SELECT COUNT(DISTINCT thread_id) AS c FROM {threads_t}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_ent_threads AS
SELECT COUNT(DISTINCT thread_id) AS c FROM {mentions_t}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_tot_entities AS
SELECT COUNT(*) AS c FROM {entities_t}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_tot_rels AS
SELECT COUNT(*) AS c FROM {rels_t}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_tot_persons AS
SELECT COUNT(*) AS c FROM {entities_t} WHERE entity_type = 'Person'
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_org_persons AS
SELECT COUNT(DISTINCT o.person_id) AS c
FROM {org_t} o
INNER JOIN {entities_t} e
  ON e.entity_id = o.person_id AND e.entity_type = 'Person'
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_tot_emails AS
SELECT COUNT(*) AS c FROM {emails_t}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_cust_emails AS
SELECT COUNT(*) AS c FROM {emails_t}
WHERE mailbox_path RLIKE '({custodian_pat})'
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _cov_sum_threads AS
SELECT COUNT(*) AS c FROM {threads_t}
WHERE summary IS NOT NULL AND LENGTH(TRIM(summary)) > 0
""")

coverage_sql = f"""
SELECT * FROM (
  SELECT
    'entity_extraction_rate' AS metric_name,
    CAST(e.c AS BIGINT) AS metric_value,
    CAST(t.c AS BIGINT) AS denominator,
    CASE WHEN t.c > 0 THEN 100.0 * e.c / t.c ELSE CAST(0.0 AS DOUBLE) END AS coverage_pct,
    CURRENT_DATE() AS as_of_date
  FROM _cov_ent_threads e CROSS JOIN _cov_tot_threads t

  UNION ALL

  SELECT
    'relationship_density',
    CAST(r.c AS BIGINT),
    CAST(n.c AS BIGINT),
    CASE WHEN n.c > 0 THEN 100.0 * r.c / n.c ELSE CAST(0.0 AS DOUBLE) END,
    CURRENT_DATE()
  FROM _cov_tot_rels r CROSS JOIN _cov_tot_entities n

  UNION ALL

  SELECT
    'org_hierarchy_coverage',
    CAST(o.c AS BIGINT),
    CAST(p.c AS BIGINT),
    CASE WHEN p.c > 0 THEN 100.0 * o.c / p.c ELSE CAST(0.0 AS DOUBLE) END,
    CURRENT_DATE()
  FROM _cov_org_persons o CROSS JOIN _cov_tot_persons p

  UNION ALL

  SELECT
    'custodian_email_coverage',
    CAST(ce.c AS BIGINT),
    CAST(te.c AS BIGINT),
    CASE WHEN te.c > 0 THEN 100.0 * ce.c / te.c ELSE CAST(0.0 AS DOUBLE) END,
    CURRENT_DATE()
  FROM _cov_cust_emails ce CROSS JOIN _cov_tot_emails te

  UNION ALL

  SELECT
    'thread_summarization_rate',
    CAST(s.c AS BIGINT),
    CAST(t.c AS BIGINT),
    CASE WHEN t.c > 0 THEN 100.0 * s.c / t.c ELSE CAST(0.0 AS DOUBLE) END,
    CURRENT_DATE()
  FROM _cov_sum_threads s CROSS JOIN _cov_tot_threads t
) x
"""

spark.sql(f"""
CREATE OR REPLACE TABLE {out_table} AS
{coverage_sql}
""")

n = spark.table(out_table).count()
print(f"corpus_coverage: {n} rows → {out_table}")
spark.table(out_table).show(truncate=False)
