# Databricks notebook source
# MAGIC %md
# MAGIC # 07c — Enron Communication Aggregations
# MAGIC
# MAGIC Build pre-aggregated communication tables from the raw `emails` table.
# MAGIC These tables enable fast-path queries for communication pattern questions
# MAGIC without scanning the full email corpus at inference time.
# MAGIC
# MAGIC **Tables created:**
# MAGIC - `communication_dyads` — weekly sender/recipient pair counts (TO/CC/BCC)
# MAGIC - `person_activity` — weekly per-person activity summary

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StringType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Communication Dyads
# MAGIC
# MAGIC For each sender-recipient pair per week, count emails by channel (TO, CC, BCC).

# COMMAND ----------

# DBTITLE 1,Build Communication Dyads Table
emails_table = config['enron_emails_table']
enron_schema = config['enron_schema']
dyads_table = f"{config['catalog']}.{enron_schema}.communication_dyads"

spark.sql(f"""
    CREATE OR REPLACE TABLE {dyads_table} AS
    SELECT
        person_a,
        person_b,
        DATE_TRUNC('week', email_date) AS period,
        COUNT(*) AS total_count,
        SUM(CASE WHEN channel = 'to' THEN 1 ELSE 0 END) AS to_count,
        SUM(CASE WHEN channel = 'cc' THEN 1 ELSE 0 END) AS cc_count,
        SUM(CASE WHEN channel = 'bcc' THEN 1 ELSE 0 END) AS bcc_count
    FROM (
        SELECT sender AS person_a, EXPLODE(to_recipients) AS person_b,
               'to' AS channel, date AS email_date
        FROM {emails_table}
        WHERE to_recipients IS NOT NULL AND SIZE(to_recipients) > 0

        UNION ALL

        SELECT sender AS person_a, EXPLODE(cc_recipients) AS person_b,
               'cc' AS channel, date AS email_date
        FROM {emails_table}
        WHERE cc_recipients IS NOT NULL AND SIZE(cc_recipients) > 0

        UNION ALL

        SELECT sender AS person_a, EXPLODE(bcc_recipients) AS person_b,
               'bcc' AS channel, date AS email_date
        FROM {emails_table}
        WHERE bcc_recipients IS NOT NULL AND SIZE(bcc_recipients) > 0
    ) expanded
    WHERE person_a IS NOT NULL AND person_b IS NOT NULL
      AND person_a != person_b
    GROUP BY person_a, person_b, DATE_TRUNC('week', email_date)
""")

dyad_count = spark.table(dyads_table).count()
print(f"Communication dyads: {dyad_count:,} rows → {dyads_table}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Person Activity (Weekly)
# MAGIC
# MAGIC Aggregate per-person weekly activity: emails sent, received, unique contacts,
# MAGIC BCC usage, after-hours emails, and weekend emails.

# COMMAND ----------

# DBTITLE 1,Build Person Activity Table
activity_table = f"{config['catalog']}.{enron_schema}.person_activity"

spark.sql(f"""
    CREATE OR REPLACE TABLE {activity_table} AS
    WITH sent AS (
        SELECT
            sender AS person_id,
            DATE_TRUNC('week', date) AS period,
            COUNT(*) AS emails_sent,
            SUM(CASE WHEN SIZE(COALESCE(bcc_recipients, ARRAY())) > 0 THEN 1 ELSE 0 END) AS bcc_emails_sent,
            SUM(CASE WHEN HOUR(date) < 7 OR HOUR(date) >= 19 THEN 1 ELSE 0 END) AS after_hours_sent,
            SUM(CASE WHEN DAYOFWEEK(date) IN (1, 7) THEN 1 ELSE 0 END) AS weekend_sent
        FROM {emails_table}
        WHERE sender IS NOT NULL
        GROUP BY sender, DATE_TRUNC('week', date)
    ),
    contacts AS (
        SELECT
            sender AS person_id,
            DATE_TRUNC('week', date) AS period,
            COUNT(DISTINCT contact) AS unique_contacts_sent
        FROM (
            SELECT sender, date, EXPLODE(
                CONCAT(
                    COALESCE(to_recipients, ARRAY()),
                    COALESCE(cc_recipients, ARRAY()),
                    COALESCE(bcc_recipients, ARRAY())
                )
            ) AS contact
            FROM {emails_table}
            WHERE sender IS NOT NULL
        )
        GROUP BY sender, DATE_TRUNC('week', date)
    ),
    received AS (
        SELECT
            recipient AS person_id,
            DATE_TRUNC('week', date) AS period,
            COUNT(*) AS emails_received
        FROM (
            SELECT EXPLODE(to_recipients) AS recipient, date FROM {emails_table}
            UNION ALL
            SELECT EXPLODE(cc_recipients) AS recipient, date FROM {emails_table}
            UNION ALL
            SELECT EXPLODE(bcc_recipients) AS recipient, date FROM {emails_table}
        )
        WHERE recipient IS NOT NULL
        GROUP BY recipient, DATE_TRUNC('week', date)
    )
    SELECT
        COALESCE(s.person_id, r.person_id) AS person_id,
        COALESCE(s.period, r.period) AS period,
        COALESCE(s.emails_sent, 0) AS emails_sent,
        COALESCE(r.emails_received, 0) AS emails_received,
        COALESCE(c.unique_contacts_sent, 0) AS unique_contacts_sent,
        COALESCE(s.bcc_emails_sent, 0) AS bcc_emails_sent,
        COALESCE(s.after_hours_sent, 0) AS after_hours_count,
        COALESCE(s.weekend_sent, 0) AS weekend_count
    FROM sent s
    FULL OUTER JOIN received r
        ON s.person_id = r.person_id AND s.period = r.period
    LEFT JOIN contacts c
        ON COALESCE(s.person_id, r.person_id) = c.person_id
       AND COALESCE(s.period, r.period) = c.period
"""
)

activity_count = spark.table(activity_table).count()
print(f"Person activity: {activity_count:,} rows → {activity_table}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Summary Statistics

# COMMAND ----------

# DBTITLE 1,Dyad Stats
print("=== Communication Dyads ===")
dyads_df = spark.table(dyads_table)
display(
    dyads_df
    .groupBy("person_a")
    .agg(
        F.sum("total_count").alias("total_emails"),
        F.countDistinct("person_b").alias("unique_contacts"),
        F.sum("bcc_count").alias("bcc_emails"),
    )
    .orderBy(F.desc("total_emails"))
    .limit(15)
)

# COMMAND ----------

# DBTITLE 1,Activity Stats
print("=== Person Activity ===")
activity_df = spark.table(activity_table)
display(
    activity_df
    .groupBy("person_id")
    .agg(
        F.sum("emails_sent").alias("total_sent"),
        F.sum("emails_received").alias("total_received"),
        F.sum("after_hours_count").alias("after_hours"),
        F.sum("weekend_count").alias("weekend"),
    )
    .orderBy(F.desc("total_sent"))
    .limit(15)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Communication aggregation tables built. These are consumed by the
# MAGIC adaptive agent's fast-path execution plans for communication-pattern
# MAGIC questions.