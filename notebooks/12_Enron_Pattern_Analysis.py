# Databricks notebook source
# MAGIC %md
# MAGIC # 12 — Enron Pattern Analysis (Learning Loop)
# MAGIC
# MAGIC Analyze MLflow traces from the Enron GraphRAG agent to identify:
# MAGIC 1. Which question patterns are most frequent
# MAGIC 2. Which slow-path questions should be promoted to fast paths
# MAGIC 3. Quality and latency comparisons between fast and slow paths
# MAGIC
# MAGIC **Run cadence:** Weekly (manual or scheduled Databricks job).
# MAGIC
# MAGIC **Output:** Promotion report with candidate patterns for the registry.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import json
import mlflow
import pandas as pd
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Fetch Recent Traces

# COMMAND ----------

# DBTITLE 1,Configuration
EXPERIMENT_NAME = config.get("enron_experiment", "graphrag-enron-agent")
LOOKBACK_DAYS = 7
MIN_CLUSTER_SIZE = 5

# COMMAND ----------

# DBTITLE 1,Fetch Traces from MLflow
experiment = mlflow.get_experiment_by_name(EXPERIMENT_NAME)
if experiment is None:
    experiments = mlflow.search_experiments()
    enron_experiments = [e for e in experiments if "enron" in e.name.lower()]
    if enron_experiments:
        experiment = enron_experiments[0]
        print(f"Using experiment: {experiment.name}")
    else:
        print(f"WARNING: No experiment matching '{EXPERIMENT_NAME}' found.")
        print("Available experiments:")
        for e in experiments[:10]:
            print(f"  - {e.name}")
        dbutils.notebook.exit("No experiment found")

traces = mlflow.search_traces(
    experiment_ids=[experiment.experiment_id],
    max_results=500,
)

print(f"Fetched {len(traces)} traces from '{experiment.name}'")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Parse Trace Metadata

# COMMAND ----------

# DBTITLE 1,Extract Classification Tags from Traces
trace_records = []

for _, trace in traces.iterrows():
    tags = trace.get("tags", {}) or {}
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            tags = {}

    request_text = ""
    try:
        request = trace.get("request", "")
        if isinstance(request, str) and request:
            parsed = json.loads(request)
            if isinstance(parsed, list):
                for msg in parsed:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        request_text = msg.get("content", "")
            elif isinstance(parsed, dict):
                request_text = parsed.get("content", parsed.get("question", ""))
    except (json.JSONDecodeError, TypeError):
        request_text = str(trace.get("request", ""))[:200]

    trace_records.append({
        "trace_id": trace.get("request_id", trace.name if hasattr(trace, "name") else ""),
        "timestamp": trace.get("timestamp_ms", 0),
        "question": request_text[:300],
        "pattern": tags.get("question_pattern", "unknown"),
        "confidence": float(tags.get("pattern_confidence", 0.0)),
        "execution_path": tags.get("execution_path", "unknown"),
        "tool_sequence": tags.get("tool_sequence", ""),
        "latency_ms": trace.get("execution_duration", 0),
        "status": trace.get("status", ""),
    })

df = pd.DataFrame(trace_records)
print(f"Parsed {len(df)} trace records")
display(df.head(10))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Pattern Distribution

# COMMAND ----------

# DBTITLE 1,Question Pattern Frequency
pattern_counts = df["pattern"].value_counts()
print("=== Pattern Distribution ===")
for pattern, count in pattern_counts.items():
    pct = count / len(df) * 100
    print(f"  {pattern:20s}: {count:4d} ({pct:5.1f}%)")

# COMMAND ----------

# DBTITLE 1,Execution Path Distribution
path_counts = df["execution_path"].value_counts()
print("\n=== Execution Path Distribution ===")
for path, count in path_counts.items():
    pct = count / len(df) * 100
    print(f"  {path:10s}: {count:4d} ({pct:5.1f}%)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Fast vs Slow Path Comparison

# COMMAND ----------

# DBTITLE 1,Latency Comparison
if "latency_ms" in df.columns and df["latency_ms"].sum() > 0:
    latency_by_path = df.groupby("execution_path")["latency_ms"].agg(["mean", "median", "std", "count"]).round(0)
    print("=== Latency by Execution Path (ms) ===")
    display(latency_by_path)
else:
    print("No latency data available in traces")

# COMMAND ----------

# DBTITLE 1,Latency by Pattern
if "latency_ms" in df.columns and df["latency_ms"].sum() > 0:
    latency_by_pattern = (
        df.groupby(["pattern", "execution_path"])["latency_ms"]
        .agg(["mean", "median", "count"])
        .round(0)
        .sort_values(("mean"), ascending=False)
    )
    print("=== Latency by Pattern and Path ===")
    display(latency_by_pattern)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Slow-Path Promotion Candidates
# MAGIC
# MAGIC Identify frequent slow-path patterns that could be promoted to fast paths.

# COMMAND ----------

# DBTITLE 1,Cluster Slow-Path Questions
slow_df = df[df["execution_path"] == "slow"].copy()

if len(slow_df) == 0:
    print("No slow-path traces found. All questions are using fast paths.")
else:
    slow_patterns = slow_df["pattern"].value_counts()
    print(f"=== Slow-Path Questions by Pattern ({len(slow_df)} total) ===")
    for pattern, count in slow_patterns.items():
        print(f"  {pattern:20s}: {count:4d}")

# COMMAND ----------

# DBTITLE 1,Frequent Tool Sequences in Slow Path
if len(slow_df) > 0:
    tool_seq_counts = slow_df["tool_sequence"].value_counts().head(15)
    print("\n=== Most Common Tool Sequences (Slow Path) ===")
    for seq, count in tool_seq_counts.items():
        if seq:
            print(f"  [{seq}] — {count} times")
else:
    print("No slow-path data to analyze")

# COMMAND ----------

# DBTITLE 1,Promotion Candidates
promotion_candidates = []

if len(slow_df) > 0:
    for pattern, group in slow_df.groupby("pattern"):
        if pattern in ("general", "unknown") and len(group) >= MIN_CLUSTER_SIZE:
            common_seqs = group["tool_sequence"].value_counts()
            for seq, count in common_seqs.items():
                if count >= MIN_CLUSTER_SIZE and seq:
                    avg_latency = group[group["tool_sequence"] == seq]["latency_ms"].mean()
                    sample_questions = group[group["tool_sequence"] == seq]["question"].head(3).tolist()
                    promotion_candidates.append({
                        "pattern": pattern,
                        "tool_sequence": seq,
                        "occurrences": count,
                        "avg_latency_ms": round(avg_latency),
                        "sample_questions": sample_questions,
                    })

        elif pattern not in ("general", "unknown") and len(group) >= MIN_CLUSTER_SIZE:
            avg_latency = group["latency_ms"].mean()
            common_seq = group["tool_sequence"].mode()
            common_seq_str = common_seq.iloc[0] if len(common_seq) > 0 else ""

            promotion_candidates.append({
                "pattern": pattern,
                "tool_sequence": common_seq_str,
                "occurrences": len(group),
                "avg_latency_ms": round(avg_latency),
                "sample_questions": group["question"].head(3).tolist(),
            })

if promotion_candidates:
    print(f"\n=== PROMOTION CANDIDATES ({len(promotion_candidates)}) ===\n")
    for i, candidate in enumerate(promotion_candidates, 1):
        print(f"  {i}. Pattern: {candidate['pattern']}")
        print(f"     Tool sequence: [{candidate['tool_sequence']}]")
        print(f"     Occurrences: {candidate['occurrences']}")
        print(f"     Avg latency: {candidate['avg_latency_ms']}ms")
        print(f"     Sample questions:")
        for q in candidate["sample_questions"]:
            print(f"       - {q}")
        print()

    promo_df = pd.DataFrame(promotion_candidates)
    display(promo_df)
else:
    print("\nNo promotion candidates found (need >= {MIN_CLUSTER_SIZE} occurrences per pattern)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Promotion Report Summary

# COMMAND ----------

# DBTITLE 1,Generate Promotion Report
report_lines = [
    f"# Pattern Promotion Report",
    f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    f"**Traces analyzed:** {len(df)}",
    f"**Period:** Last {LOOKBACK_DAYS} days",
    f"",
    f"## Distribution",
    f"- Fast path: {len(df[df['execution_path'] == 'fast'])} ({len(df[df['execution_path'] == 'fast'])/max(len(df),1)*100:.0f}%)",
    f"- Slow path: {len(slow_df)} ({len(slow_df)/max(len(df),1)*100:.0f}%)",
    f"",
]

if promotion_candidates:
    report_lines.append(f"## Promotion Candidates ({len(promotion_candidates)})")
    report_lines.append("")
    for i, c in enumerate(promotion_candidates, 1):
        report_lines.append(f"### {i}. {c['pattern']}")
        report_lines.append(f"- Tool sequence: `{c['tool_sequence']}`")
        report_lines.append(f"- Occurrences: {c['occurrences']}")
        report_lines.append(f"- Avg latency: {c['avg_latency_ms']}ms")
        report_lines.append("")

    report_lines.extend([
        "## Next Steps",
        "",
        "For each candidate above:",
        "1. Add a new entry to `PATTERN_REGISTRY` in `src/agent/pattern_registry.py`",
        "2. If the pattern needs a new pre-aggregation table, build it in a new notebook",
        "3. If it needs a new composite tool, add it to `src/agent/agent_serving.py`",
        "4. Update the classifier prompt categories if adding a new pattern name",
        "5. Re-export local data, test locally, redeploy",
    ])
else:
    report_lines.append("## No promotion candidates — all patterns are well-served.")

report = "\n".join(report_lines)
print(report)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Pattern analysis complete. Use the promotion candidates above to
# MAGIC decide which slow-path patterns to promote into the fast-path registry.
# MAGIC See `src/agent/pattern_registry.py` for the registry format.
