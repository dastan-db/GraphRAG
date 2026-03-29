# Databricks notebook source
# MAGIC %md
# MAGIC # 07j — Enron Data Quality Report (M4)
# MAGIC
# MAGIC Per-table, per-column null rates and cardinality metrics (refresh_date = run date).
# MAGIC
# MAGIC **Table:** `data_quality_report`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import (
    DateType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)

# COMMAND ----------

# DBTITLE 1,Target tables
catalog = config["catalog"]
schema = config["enron_schema"]
out_t = config["enron_data_quality_report_table"]

KNOWN_TABLES = [
    "entities",
    "relationships",
    "emails",
    "entity_analytics",
    "entity_paths",
    "entity_mentions",
    "entity_aliases",
    "communication_dyads",
    "person_activity",
    "participants",
    "org_hierarchy",
    "investigation_timeline",
    "threads",
]

# COMMAND ----------

# DBTITLE 1,Collect columns from information_schema
cols_df = spark.sql(
    f"""
    SELECT table_name, column_name
    FROM {catalog}.information_schema.columns
    WHERE table_catalog = '{catalog}'
      AND table_schema = '{schema}'
      AND table_name IN ({",".join("'" + t + "'" for t in KNOWN_TABLES)})
    ORDER BY table_name, ordinal_position
    """
)
col_rows = cols_df.collect()

# COMMAND ----------

# DBTITLE 1,Compute metrics per column
schema_metric = StructType(
    [
        StructField("table_name", StringType()),
        StructField("column_name", StringType()),
        StructField("refresh_date", DateType()),
        StructField("total_rows", LongType()),
        StructField("null_count", LongType()),
        StructField("null_rate", DoubleType()),
        StructField("distinct_count", LongType()),
        StructField("cardinality_ratio", DoubleType()),
    ]
)

metric_rows = []
for row in col_rows:
    tname = row.table_name
    cname = row.column_name
    fqn = f"{catalog}.{schema}.{tname}"
    try:
        if not spark.catalog.tableExists(fqn):
            continue
    except Exception:
        continue
    try:
        df = spark.table(fqn)
        if cname not in df.columns:
            continue
        c = F.col(f"`{cname}`")
        agg = df.select(
            F.count(F.lit(1)).alias("total_rows"),
            F.sum(F.when(c.isNull(), F.lit(1)).otherwise(F.lit(0))).alias("null_count"),
            F.countDistinct(c).alias("distinct_count"),
        ).collect()[0]
        total = int(agg.total_rows or 0)
        null_c = int(agg.null_count or 0)
        distinct_c = int(agg.distinct_count or 0)
        non_null = total - null_c
        null_rate = float(1.0 - (non_null / total)) if total > 0 else 0.0
        card_ratio = float(distinct_c / total) if total > 0 else 0.0
        metric_rows.append(
            (tname, cname, None, total, null_c, null_rate, distinct_c, card_ratio)
        )
    except Exception as ex:
        print(f"Skip {fqn}.{cname}: {ex}")

if not metric_rows:
    empty = spark.createDataFrame([], schema_metric)
    empty.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)
    print(f"No metrics computed → {out_t}")
else:
    mdf = spark.createDataFrame(metric_rows, schema_metric)
    mdf = mdf.withColumn("refresh_date", F.current_date())
    mdf.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)
    print(f"data_quality_report: {mdf.count():,} rows → {out_t}")
