# Databricks notebook source
# MAGIC %md
# MAGIC # 08b — Enron metric views (KPIs)
# MAGIC
# MAGIC **Unity Catalog metric views** (`CREATE VIEW ... WITH METRICS LANGUAGE YAML`) — DBR 17.2+ / SQL warehouse with metric view support.
# MAGIC
# MAGIC If `WITH METRICS` is not enabled, fall back cells use regular views with documented metric semantics.

# COMMAND ----------

# DBTITLE 1,Load configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Parameters
catalog = config["catalog"]
schema = config["enron_schema"]
fqn = f"{catalog}.{schema}"

person_activity = f"{fqn}.person_activity"
email_classification = f"{fqn}.email_classification"
communication_dyads = f"{fqn}.communication_dyads"
org_hierarchy = f"{fqn}.org_hierarchy"
person_identity = f"{fqn}.person_identity"

# COMMAND ----------

# DBTITLE 1,Metric views (YAML) — try CREATE OR REPLACE VIEW ... WITH METRICS
try:
    spark.sql(
        f"""
CREATE OR REPLACE VIEW {fqn}.metric_emails_per_person
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  comment: "Total emails sent per person (weekly rows in person_activity summed by person)"
  source: {person_activity}
  dimensions:
    - name: person_id
      expr: person_id
  measures:
    - name: emails_per_person
      expr: SUM(emails_sent)
$$
"""
    )
    print(f"OK metric view: {fqn}.metric_emails_per_person")
except Exception as e:
    print(f"Metric view not available (emails_per_person): {e}")

try:
    spark.sql(
        f"""
CREATE OR REPLACE VIEW {fqn}.metric_response_and_depth
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  comment: "Reply depth and reply vs root-email ratio from email_classification"
  source: {email_classification}
  dimensions:
    - name: email_bucket
      expr: CASE WHEN reply_depth = 0 THEN 'original' ELSE 'reply' END
  measures:
    - name: email_count
      expr: COUNT(1)
    - name: avg_thread_depth
      expr: AVG(CAST(reply_depth AS DOUBLE))
$$
"""
    )
    print(f"OK metric view: {fqn}.metric_response_and_depth")
except Exception as e:
    print(f"Metric view not available (response/depth): {e}")

# Cross-division metric needs email → entity_id → org_hierarchy; YAML joins are brittle here — use vw_metric_cross_division_rate below.

# COMMAND ----------

# DBTITLE 1,Fallback: plain views + docs (always safe)
spark.sql(
    f"""
CREATE OR REPLACE VIEW {fqn}.vw_metric_emails_per_person AS
SELECT
  person_id,
  SUM(emails_sent) AS emails_per_person
FROM {person_activity}
GROUP BY person_id
"""
)
spark.sql(
    f"""
CREATE OR REPLACE VIEW {fqn}.vw_metric_response_rate AS
SELECT
  SUM(CASE WHEN reply_depth > 0 THEN 1 ELSE 0 END) AS reply_emails,
  SUM(CASE WHEN reply_depth = 0 THEN 1 ELSE 0 END) AS original_emails,
  CASE WHEN SUM(CASE WHEN reply_depth = 0 THEN 1 ELSE 0 END) = 0 THEN NULL
       ELSE SUM(CASE WHEN reply_depth > 0 THEN 1 ELSE 0 END)
            / SUM(CASE WHEN reply_depth = 0 THEN 1 ELSE 0 END)
  END AS response_rate
FROM {email_classification}
"""
)
spark.sql(
    f"""
CREATE OR REPLACE VIEW {fqn}.vw_metric_avg_thread_depth AS
SELECT AVG(CAST(reply_depth AS DOUBLE)) AS avg_thread_depth
FROM {email_classification}
"""
)
spark.sql(
    f"""
CREATE OR REPLACE VIEW {fqn}.vw_metric_cross_division_rate AS
WITH email_entity AS (
  SELECT entity_id, explode(email_addresses) AS email_address
  FROM {person_identity}
),
dyad_dept AS (
  SELECT
    d.person_a,
    d.person_b,
    MAX(ha.department) AS dept_a,
    MAX(hb.department) AS dept_b
  FROM {communication_dyads} d
  LEFT JOIN email_entity ea ON d.person_a = ea.email_address
  LEFT JOIN {org_hierarchy} ha ON ea.entity_id = ha.person_id
  LEFT JOIN email_entity eb ON d.person_b = eb.email_address
  LEFT JOIN {org_hierarchy} hb ON eb.entity_id = hb.person_id
  GROUP BY d.person_a, d.person_b
)
SELECT
  COUNT_IF(dept_a IS NOT NULL AND dept_b IS NOT NULL AND dept_a <> dept_b) AS cross_division_dyads,
  COUNT_IF(dept_a IS NOT NULL AND dept_b IS NOT NULL) AS dyads_with_both_depts,
  CASE WHEN COUNT_IF(dept_a IS NOT NULL AND dept_b IS NOT NULL) = 0 THEN NULL
       ELSE 100.0 * COUNT_IF(dept_a IS NOT NULL AND dept_b IS NOT NULL AND dept_a <> dept_b)
            / COUNT_IF(dept_a IS NOT NULL AND dept_b IS NOT NULL)
  END AS cross_division_communication_pct
FROM dyad_dept
"""
)

print("Fallback views ready:")
print(f"  {fqn}.vw_metric_emails_per_person")
print(f"  {fqn}.vw_metric_response_rate (one row)")
print(f"  {fqn}.vw_metric_avg_thread_depth (one row)")
print(f"  {fqn}.vw_metric_cross_division_rate (one row)")
