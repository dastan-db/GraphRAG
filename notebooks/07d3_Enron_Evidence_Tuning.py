# Databricks notebook source
# MAGIC %md
# MAGIC # 07d3 — Evidence Tuning Sweep
# MAGIC
# MAGIC Sweeps C1 build-time knobs (K1-K8) for the `org_hierarchy_evidence` table
# MAGIC and logs coverage/precision KPIs to MLflow for plateau detection.
# MAGIC
# MAGIC **Usage:** Set widget values and run all cells. Each run logs to MLflow.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Widgets (knob overrides)
dbutils.widgets.text("min_relevance_threshold", "0.3", "K8: Min Relevance Threshold")
dbutils.widgets.text("mass_mail_threshold", "5", "K6: Mass Mail Threshold")
dbutils.widgets.text("date_proximity_boost", "0.0", "K4: Date Proximity Boost")
dbutils.widgets.text("org_keyword_boost", "0.0", "K7: Org Keyword Boost")
dbutils.widgets.text("max_emails_per_pair", "20", "K3: Max Emails Per Pair")
dbutils.widgets.text("strategy_weight_D", "0.4", "K1d: Strategy D Weight")
dbutils.widgets.text("run_name", "evidence_tuning", "MLflow Run Name")

# COMMAND ----------

# DBTITLE 1,Apply Overrides
import mlflow

ev_cfg = config['evidence_config']
overrides = {
    "min_relevance_threshold": float(dbutils.widgets.get("min_relevance_threshold")),
    "mass_mail_threshold": int(dbutils.widgets.get("mass_mail_threshold")),
    "date_proximity_boost": float(dbutils.widgets.get("date_proximity_boost")),
    "org_keyword_boost": float(dbutils.widgets.get("org_keyword_boost")),
    "max_emails_per_pair": int(dbutils.widgets.get("max_emails_per_pair")),
}

ev_cfg.update(overrides)

strat_d_weight = float(dbutils.widgets.get("strategy_weight_D"))
ev_cfg["strategy_weights"]["D"] = strat_d_weight
overrides["strategy_weight_D"] = strat_d_weight

print("Knob overrides for this run:")
for k, v in overrides.items():
    print(f"  {k}: {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Rebuild Evidence Table with Current Knobs

# COMMAND ----------

# DBTITLE 1,Run Evidence Linking Notebook
dbutils.notebook.run("07d2_Enron_Org_Hierarchy_Evidence", timeout_seconds=600)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Compute KPIs

# COMMAND ----------

# DBTITLE 1,Coverage KPIs
import pyspark.sql.functions as F

EVIDENCE_TABLE = config['enron_org_hierarchy_evidence_table']
ORG_TABLE = config['enron_org_hierarchy_table']

total_pairs = (
    spark.table(ORG_TABLE)
    .filter(F.col("reports_to_id").isNotNull())
    .select("person_id", "reports_to_id").distinct().count()
)

covered_pairs = (
    spark.table(EVIDENCE_TABLE)
    .select("person_id", "reports_to_id").distinct().count()
)

total_evidence = spark.table(EVIDENCE_TABLE).count()

avg_relevance = (
    spark.table(EVIDENCE_TABLE)
    .agg(F.avg("relevance_score").alias("avg"))
    .collect()[0]["avg"]
) or 0.0

coverage_pct = round(covered_pairs / max(total_pairs, 1), 3)

strategy_breakdown = (
    spark.table(EVIDENCE_TABLE)
    .groupBy("evidence_strategy")
    .agg(
        F.count("*").alias("count"),
        F.round(F.avg("relevance_score"), 3).alias("avg_relevance"),
    )
    .collect()
)

print(f"Coverage: {covered_pairs}/{total_pairs} pairs ({coverage_pct:.1%})")
print(f"Total evidence rows: {total_evidence}")
print(f"Average relevance: {avg_relevance:.3f}")
for row in strategy_breakdown:
    print(f"  Strategy {row['evidence_strategy']}: {row['count']} rows, avg={row['avg_relevance']}")

# COMMAND ----------

# DBTITLE 1,Precision@5 Estimate (top evidence per pair)
from pyspark.sql.window import Window

w = Window.partitionBy("person_id", "reports_to_id").orderBy(F.desc("relevance_score"))
top5 = (
    spark.table(EVIDENCE_TABLE)
    .withColumn("rank", F.row_number().over(w))
    .filter(F.col("rank") <= 5)
)

precision_proxy = top5.agg(F.avg("relevance_score").alias("avg_top5_relevance")).collect()[0]["avg_top5_relevance"] or 0.0
print(f"Precision@5 proxy (avg top-5 relevance): {precision_proxy:.3f}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Log to MLflow

# COMMAND ----------

# DBTITLE 1,Log KPIs and Knobs
run_name = dbutils.widgets.get("run_name")

with mlflow.start_run(run_name=run_name, tags={"eval_type": "evidence_tuning"}):
    mlflow.log_metric("C1_evidence_coverage", coverage_pct)
    mlflow.log_metric("C1_total_evidence_rows", total_evidence)
    mlflow.log_metric("C1_avg_relevance", round(avg_relevance, 3))
    mlflow.log_metric("C1_precision_at_5_proxy", round(precision_proxy, 3))
    mlflow.log_metric("C1_covered_pairs", covered_pairs)
    mlflow.log_metric("C1_total_pairs", total_pairs)

    for row in strategy_breakdown:
        mlflow.log_metric(f"C1_strategy_{row['evidence_strategy']}_count", row["count"])

    for k, v in overrides.items():
        mlflow.log_param(f"knob_{k}", v)

print(f"\nMLflow run logged: {run_name}")
print(f"  C1_evidence_coverage: {coverage_pct:.3f}")
print(f"  C1_precision_at_5_proxy: {precision_proxy:.3f}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Plateau Detection

# COMMAND ----------

# DBTITLE 1,Compare with Previous Runs
experiment = mlflow.get_experiment_by_name(mlflow.get_experiment(mlflow.active_run().info.experiment_id).name if mlflow.active_run() else None)

runs = mlflow.search_runs(
    filter_string="tags.eval_type = 'evidence_tuning'",
    order_by=["start_time DESC"],
    max_results=10,
)

if len(runs) >= 2:
    print("=== Recent Tuning Runs ===")
    display_cols = ["run_id", "start_time", "metrics.C1_evidence_coverage",
                    "metrics.C1_precision_at_5_proxy", "metrics.C1_avg_relevance"]
    available = [c for c in display_cols if c in runs.columns]
    print(runs[available].head(5).to_string())

    plateau_window = ev_cfg.get("plateau_window", 2)
    plateau_threshold = ev_cfg.get("plateau_threshold_pp", 2) / 100.0

    if len(runs) >= plateau_window + 1:
        recent_coverage = runs["metrics.C1_evidence_coverage"].head(plateau_window).values
        if all(abs(recent_coverage[i] - recent_coverage[i + 1]) < plateau_threshold
               for i in range(len(recent_coverage) - 1)):
            print(f"\n  PLATEAU DETECTED: Coverage changed < {plateau_threshold:.0%} over last {plateau_window} runs")
        else:
            print(f"\n  Still improving — coverage delta > {plateau_threshold:.0%}")
else:
    print("Not enough runs for plateau detection yet. Run at least 2 sweeps.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Tuning sweep complete. Check MLflow for the run comparison dashboard.
