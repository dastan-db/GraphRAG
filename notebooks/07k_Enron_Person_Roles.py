# Databricks notebook source
# MAGIC %md
# MAGIC # 07k — Enron Person Role Timeline (M5)
# MAGIC
# MAGIC Unified role history from `org_hierarchy` (undated snapshot rows) plus
# MAGIC curated SEC-style executive tenure segments.
# MAGIC
# MAGIC **Table:** `person_role_timeline`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import DateType, StringType, StructField, StructType

# COMMAND ----------

# DBTITLE 1,Build person_role_timeline
org_t = config["enron_org_hierarchy_table"]
out_t = config["enron_person_role_timeline_table"]

org_df = (
    spark.table(org_t)
    .select(
        F.col("person_id").alias("entity_id"),
        F.col("title"),
        F.col("department"),
        F.col("reports_to_id").alias("reports_to"),
        F.lit(None).cast(DateType()).alias("effective_from"),
        F.lit(None).cast(DateType()).alias("effective_to"),
        F.lit("org_hierarchy").alias("source"),
    )
)

sec_schema = StructType(
    [
        StructField("entity_id", StringType()),
        StructField("title", StringType()),
        StructField("department", StringType()),
        StructField("reports_to", StringType()),
        StructField("effective_from", StringType()),
        StructField("effective_to", StringType()),
        StructField("source", StringType()),
    ]
)

sec_rows = [
    (
        "kenneth_lay",
        "Chairman & CEO",
        "Enron Corp",
        None,
        "1986-01-01",
        "2001-01-23",
        "sec_filing",
    ),
    (
        "kenneth_lay",
        "Chairman",
        "Enron Corp",
        None,
        "2001-01-23",
        "2002-01-23",
        "sec_filing",
    ),
    (
        "jeff_skilling",
        "CEO",
        "Enron Corp",
        None,
        "2001-02-12",
        "2001-08-14",
        "sec_filing",
    ),
    (
        "andrew_fastow",
        "CFO",
        "Enron Corp",
        None,
        "1998-01-01",
        "2001-10-24",
        "sec_filing",
    ),
    (
        "rebecca_mark",
        "CEO Enron International / Azurix",
        "Enron International",
        None,
        "1991-01-01",
        "2000-08-01",
        "sec_filing",
    ),
]

sec_df = (
    spark.createDataFrame(sec_rows, sec_schema)
    .withColumn("effective_from", F.col("effective_from").cast(DateType()))
    .withColumn("effective_to", F.col("effective_to").cast(DateType()))
)

combined = org_df.unionByName(sec_df)
combined.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)

n = spark.table(out_t).count()
print(f"person_role_timeline: {n:,} rows → {out_t}")
