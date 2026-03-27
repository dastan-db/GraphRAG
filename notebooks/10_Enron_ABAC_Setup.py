# Databricks notebook source
# MAGIC %md
# MAGIC # 10 — Enron ABAC Setup
# MAGIC
# MAGIC Create Unity Catalog row filters, column masks, and ABAC-aware views
# MAGIC that demonstrate attribute-based access control over the Enron knowledge
# MAGIC graph.  Three user tiers see progressively restricted subsets of the graph:
# MAGIC
# MAGIC | Tier | Sensitivity visible | BCC visible |
# MAGIC |---|---|---|
# MAGIC | `legal_team` | all | yes |
# MAGIC | `executive_team` | general, executive_confidential | no (masked) |
# MAGIC | `analyst_team` | general only | no (masked) |
# MAGIC
# MAGIC **Prerequisites:** Run notebooks 06 (data prep with sensitivity column)
# MAGIC and 07 (knowledge graph build) first.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Verify Sensitivity Column Exists
from pyspark.sql import functions as F

emails_df = spark.table(config['enron_emails_table'])
assert 'sensitivity' in emails_df.columns, (
    "Column 'sensitivity' missing from emails table. "
    "Re-run notebook 06 to add the classification step."
)

display(
    emails_df.groupBy("sensitivity")
    .agg(F.count("*").alias("count"))
    .orderBy("sensitivity")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Row Filter Function
# MAGIC
# MAGIC The row filter is a SQL UDF that Unity Catalog evaluates for **every row**
# MAGIC returned from the `emails` table.  It checks the calling user's group
# MAGIC membership via `is_account_group_member()` and compares it against the
# MAGIC row's `sensitivity` value.

# COMMAND ----------

# DBTITLE 1,Create Row Filter Function
schema = config['enron_schema']
catalog = config['catalog']

spark.sql(f"""
    CREATE OR REPLACE FUNCTION {catalog}.{schema}.email_access_filter(sensitivity STRING)
    RETURNS BOOLEAN
    RETURN CASE
        WHEN is_account_group_member('legal_team') THEN TRUE
        WHEN is_account_group_member('executive_team')
            THEN sensitivity IN ('general', 'executive_confidential')
        WHEN is_account_group_member('analyst_team')
            THEN sensitivity = 'general'
        ELSE FALSE
    END
""")
print(f"Created row filter function: {catalog}.{schema}.email_access_filter")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Column Mask Function
# MAGIC
# MAGIC BCC recipients are sensitive metadata — only the legal team should see
# MAGIC them.  Everyone else gets `NULL`.

# COMMAND ----------

# DBTITLE 1,Create Column Mask Function
spark.sql(f"""
    CREATE OR REPLACE FUNCTION {catalog}.{schema}.mask_bcc(bcc ARRAY<STRING>)
    RETURNS ARRAY<STRING>
    RETURN IF(is_account_group_member('legal_team'), bcc, NULL)
""")
print(f"Created column mask function: {catalog}.{schema}.mask_bcc")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Attach Policies to the Emails Table
# MAGIC
# MAGIC `ALTER TABLE ... SET ROW FILTER` and `SET COLUMN MASK` are the
# MAGIC Databricks-native way to enforce ABAC.  Once set, **every SQL query**
# MAGIC against the table — including those from the agent's tools — automatically
# MAGIC respects the policy.

# COMMAND ----------

# DBTITLE 1,Attach Row Filter and Column Mask
emails_table = config['enron_emails_table']

spark.sql(f"""
    ALTER TABLE {emails_table}
    SET ROW FILTER {catalog}.{schema}.email_access_filter
    ON (sensitivity)
""")
print(f"Row filter attached to {emails_table}")

spark.sql(f"""
    ALTER TABLE {emails_table}
    SET COLUMN MASK {catalog}.{schema}.mask_bcc
    ON bcc_recipients
""")
print(f"Column mask attached to {emails_table}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: ABAC-Aware Graph Views
# MAGIC
# MAGIC These views join graph tables back to the filtered `emails` table via
# MAGIC `entity_mentions`.  Because the row filter is applied transparently by
# MAGIC Unity Catalog, these views automatically respect the caller's access
# MAGIC tier — no application-level SQL filters needed.

# COMMAND ----------

# DBTITLE 1,Emails ABAC View (convenience — same as base table with filter active)
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_emails_view']} AS
    SELECT * FROM {config['enron_emails_table']}
""")
print(f"Created {config['enron_abac_emails_view']}")

# COMMAND ----------

# DBTITLE 1,Entity Mentions ABAC View
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_entity_mentions_view']} AS
    SELECT DISTINCT em.*
    FROM {config['enron_entity_mentions_table']} em
    JOIN {config['enron_emails_table']} e
        ON em.message_id = e.message_id
""")
print(f"Created {config['enron_abac_entity_mentions_view']}")

# COMMAND ----------

# DBTITLE 1,Entities ABAC View
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_entities_view']} AS
    SELECT DISTINCT ent.*
    FROM {config['enron_entities_table']} ent
    WHERE EXISTS (
        SELECT 1 FROM {config['enron_abac_entity_mentions_view']} em
        WHERE em.entity_id = ent.entity_id
    )
""")
print(f"Created {config['enron_abac_entities_view']}")

# COMMAND ----------

# DBTITLE 1,Relationships ABAC View
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_relationships_view']} AS
    SELECT r.*
    FROM {config['enron_relationships_table']} r
    WHERE EXISTS (
        SELECT 1 FROM {config['enron_abac_entities_view']} e
        WHERE e.entity_id = r.source_entity
    )
    AND EXISTS (
        SELECT 1 FROM {config['enron_abac_entities_view']} e
        WHERE e.entity_id = r.target_entity
    )
""")
print(f"Created {config['enron_abac_relationships_view']}")

# COMMAND ----------

# DBTITLE 1,Entity Paths ABAC View
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_entity_paths_view']} AS
    SELECT p.*
    FROM {config['enron_entity_paths_table']} p
    WHERE EXISTS (
        SELECT 1 FROM {config['enron_abac_entities_view']} e
        WHERE e.entity_id = p.source_id
    )
    AND EXISTS (
        SELECT 1 FROM {config['enron_abac_entities_view']} e
        WHERE e.entity_id = p.target_id
    )
""")
print(f"Created {config['enron_abac_entity_paths_view']}")

# COMMAND ----------

# DBTITLE 1,Entity Analytics ABAC View
spark.sql(f"""
    CREATE OR REPLACE VIEW {config['enron_abac_entity_analytics_view']} AS
    SELECT ea.*
    FROM {config['enron_entity_analytics_table']} ea
    WHERE EXISTS (
        SELECT 1 FROM {config['enron_abac_entities_view']} e
        WHERE e.entity_id = ea.entity_id
    )
""")
print(f"Created {config['enron_abac_entity_analytics_view']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Verify Setup
# MAGIC
# MAGIC Show the count of entities, relationships, and paths visible through
# MAGIC the ABAC views (as the current user).

# COMMAND ----------

# DBTITLE 1,ABAC View Counts (Current User's Tier)
import pyspark.sql.functions as F

views = {
    "emails_abac": config['enron_abac_emails_view'],
    "entities_abac": config['enron_abac_entities_view'],
    "relationships_abac": config['enron_abac_relationships_view'],
    "entity_paths_abac": config['enron_abac_entity_paths_view'],
    "entity_analytics_abac": config['enron_abac_entity_analytics_view'],
}

rows = []
for name, view in views.items():
    cnt = spark.table(view).count()
    rows.append((name, cnt))
    print(f"  {name}: {cnt:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Detach Policies (Cleanup)
# MAGIC
# MAGIC Uncomment the cells below to remove the row filter and column mask
# MAGIC when you no longer need them for the demo.

# COMMAND ----------

# DBTITLE 1,Detach Row Filter (uncomment to run)
# spark.sql(f"ALTER TABLE {config['enron_emails_table']} DROP ROW FILTER")
# print("Row filter detached")

# COMMAND ----------

# DBTITLE 1,Detach Column Mask (uncomment to run)
# spark.sql(f"ALTER TABLE {config['enron_emails_table']} ALTER COLUMN bcc_recipients DROP COLUMN MASK")
# print("Column mask detached")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Setup complete. Proceed to **11_Enron_ABAC_Demo** for the side-by-side
# MAGIC tier comparison demo.
