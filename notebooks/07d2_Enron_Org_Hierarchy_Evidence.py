# Databricks notebook source
# MAGIC %md
# MAGIC # 07d2 — Org Hierarchy Evidence Linking
# MAGIC
# MAGIC Links curated `org_hierarchy` claims to corroborating emails using four strategies:
# MAGIC - **Strategy A** — Direct communication (sender/recipient header match)
# MAGIC - **Strategy B** — Entity co-mention in threads (via `entity_mentions`)
# MAGIC - **Strategy C** — Graph edge cross-reference (`REPORTS_TO`/`MANAGES` in `relationships`)
# MAGIC - **Strategy D** — Keyword co-occurrence in email bodies
# MAGIC
# MAGIC Output: `org_hierarchy_evidence` table — each row links a hierarchy claim to a
# MAGIC specific email with a relevance score. All knobs are parameterized via `config['evidence_config']`.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Imports and Knobs
import pyspark.sql.functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, FloatType, DateType,
)
from functools import reduce

ev_cfg = config['evidence_config']
STRATEGY_WEIGHTS = ev_cfg['strategy_weights']
SNIPPET_LENGTH = ev_cfg['snippet_length']
MAX_EMAILS_PER_PAIR = ev_cfg['max_emails_per_pair']
DATE_PROXIMITY_BOOST = ev_cfg['date_proximity_boost']
DATE_PROXIMITY_WINDOW = ev_cfg['date_proximity_window_days']
RECIPIENT_WEIGHTS = ev_cfg['recipient_type_weights']
MASS_MAIL_THRESHOLD = ev_cfg['mass_mail_threshold']
ORG_KEYWORD_BOOST = ev_cfg['org_keyword_boost']
MIN_RELEVANCE = ev_cfg['min_relevance_threshold']

CATALOG = config['catalog']
ENRON_SCHEMA = config['enron_schema']
ORG_TABLE = config['enron_org_hierarchy_table']
EMAILS_TABLE = config['enron_emails_table']
MENTIONS_TABLE = config['enron_entity_mentions_table']
RELS_TABLE = config['enron_relationships_table']
EVIDENCE_TABLE = config['enron_org_hierarchy_evidence_table']

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Load distinct hierarchy pairs

# COMMAND ----------

# DBTITLE 1,Hierarchy Pairs with Effective Dates
hierarchy_pairs = (
    spark.table(ORG_TABLE)
    .filter(F.col("reports_to_id").isNotNull())
    .select(
        F.col("person_id"),
        F.col("reports_to_id"),
        F.col("name").alias("person_name"),
        F.col("effective_from"),
        F.col("effective_to"),
    )
    .distinct()
)

pair_count = hierarchy_pairs.count()
print(f"Hierarchy pairs to link: {pair_count}")
display(hierarchy_pairs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Strategy A — Direct Communication

# COMMAND ----------

# DBTITLE 1,Strategy A: Header Match (sender <-> recipient)
emails_df = spark.table(EMAILS_TABLE).select(
    "message_id", "thread_id", "sender", "subject", "date", "body",
    "to_recipients", "cc_recipients", "bcc_recipients",
)

manager_names = (
    spark.table(ORG_TABLE)
    .filter(F.col("reports_to_id").isNotNull())
    .select(
        F.col("reports_to_id"),
        F.col("reports_to_id").alias("manager_id_lookup"),
    )
    .join(
        spark.table(ORG_TABLE).select(
            F.col("person_id").alias("mgr_pid"),
            F.col("name").alias("manager_name"),
        ),
        F.col("reports_to_id") == F.col("mgr_pid"),
        "left",
    )
    .select("reports_to_id", "manager_name")
    .distinct()
)

hierarchy_with_names = hierarchy_pairs.join(
    manager_names, on="reports_to_id", how="left"
)

def _email_slug(name_col):
    """Convert person_id slug to an email-matching pattern."""
    return F.concat(F.regexp_replace(name_col, "_", "."), F.lit("@enron.com"))

strat_a_forward = (
    emails_df.alias("e")
    .join(hierarchy_with_names.alias("h"), F.col("e.sender").contains(F.col("h.person_id")), "inner")
    .filter(
        F.array_contains(F.col("e.to_recipients"), _email_slug(F.col("h.reports_to_id")))
        | F.col("e.sender").contains(F.col("h.reports_to_id"))
    )
    .select(
        F.col("h.person_id"),
        F.col("h.reports_to_id"),
        F.col("e.message_id"),
        F.col("e.thread_id"),
        F.lit("A").alias("evidence_strategy"),
        F.lit(float(STRATEGY_WEIGHTS["A"])).alias("base_score"),
        F.substring(F.col("e.body"), 1, SNIPPET_LENGTH).alias("snippet"),
        F.col("e.sender"),
        F.col("e.date"),
        F.col("e.subject"),
        F.coalesce(F.size("e.to_recipients"), F.lit(0)).alias("recipient_count"),
        F.col("h.effective_from"),
        F.col("h.effective_to"),
    )
)

strat_a_reverse = (
    emails_df.alias("e")
    .join(hierarchy_with_names.alias("h"), F.col("e.sender").contains(F.col("h.reports_to_id")), "inner")
    .filter(
        F.array_contains(F.col("e.to_recipients"), _email_slug(F.col("h.person_id")))
        | F.col("e.sender").contains(F.col("h.person_id"))
    )
    .select(
        F.col("h.person_id"),
        F.col("h.reports_to_id"),
        F.col("e.message_id"),
        F.col("e.thread_id"),
        F.lit("A").alias("evidence_strategy"),
        F.lit(float(STRATEGY_WEIGHTS["A"])).alias("base_score"),
        F.substring(F.col("e.body"), 1, SNIPPET_LENGTH).alias("snippet"),
        F.col("e.sender"),
        F.col("e.date"),
        F.col("e.subject"),
        F.coalesce(F.size("e.to_recipients"), F.lit(0)).alias("recipient_count"),
        F.col("h.effective_from"),
        F.col("h.effective_to"),
    )
)

strat_a = strat_a_forward.unionByName(strat_a_reverse).dropDuplicates(["message_id", "person_id", "reports_to_id"])
print(f"Strategy A candidates: {strat_a.count()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Strategy B — Entity Co-Mention in Threads

# COMMAND ----------

# DBTITLE 1,Strategy B: Both entities mentioned in same thread
mentions = spark.table(MENTIONS_TABLE).select("entity_id", "message_id", "thread_id")

strat_b = (
    mentions.alias("m1")
    .join(mentions.alias("m2"),
          (F.col("m1.message_id") == F.col("m2.message_id"))
          & (F.col("m1.entity_id") != F.col("m2.entity_id")),
          "inner")
    .join(hierarchy_pairs.alias("h"),
          (F.col("m1.entity_id").contains(F.col("h.person_id")))
          & (F.col("m2.entity_id").contains(F.col("h.reports_to_id"))),
          "inner")
    .join(emails_df.alias("e"), F.col("m1.message_id") == F.col("e.message_id"), "inner")
    .select(
        F.col("h.person_id"),
        F.col("h.reports_to_id"),
        F.col("e.message_id"),
        F.col("m1.thread_id"),
        F.lit("B").alias("evidence_strategy"),
        F.lit(float(STRATEGY_WEIGHTS["B"])).alias("base_score"),
        F.substring(F.col("e.body"), 1, SNIPPET_LENGTH).alias("snippet"),
        F.col("e.sender"),
        F.col("e.date"),
        F.col("e.subject"),
        F.coalesce(F.size("e.to_recipients"), F.lit(0)).alias("recipient_count"),
        F.col("h.effective_from"),
        F.col("h.effective_to"),
    )
    .dropDuplicates(["message_id", "person_id", "reports_to_id"])
)
print(f"Strategy B candidates: {strat_b.count()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Strategy C — Graph Edge Cross-Reference

# COMMAND ----------

# DBTITLE 1,Strategy C: REPORTS_TO/MANAGES edges with source_threads
rels = spark.table(RELS_TABLE).filter(
    F.col("relationship_type").isin("REPORTS_TO", "MANAGES", "SUPERVISED_BY")
)

strat_c_base = (
    rels.alias("r")
    .join(hierarchy_pairs.alias("h"),
          (F.col("r.source_entity").contains(F.col("h.person_id"))
           & F.col("r.target_entity").contains(F.col("h.reports_to_id")))
          | (F.col("r.source_entity").contains(F.col("h.reports_to_id"))
             & F.col("r.target_entity").contains(F.col("h.person_id"))),
          "inner")
    .select(
        F.col("h.person_id"),
        F.col("h.reports_to_id"),
        F.explode_outer(F.col("r.source_threads")).alias("thread_id"),
        F.col("h.effective_from"),
        F.col("h.effective_to"),
    )
    .filter(F.col("thread_id").isNotNull())
)

strat_c = (
    strat_c_base
    .join(emails_df.alias("e"),
          strat_c_base["thread_id"] == F.col("e.thread_id"),
          "inner")
    .select(
        strat_c_base["person_id"],
        strat_c_base["reports_to_id"],
        F.col("e.message_id"),
        F.col("e.thread_id"),
        F.lit("C").alias("evidence_strategy"),
        F.lit(float(STRATEGY_WEIGHTS["C"])).alias("base_score"),
        F.substring(F.col("e.body"), 1, SNIPPET_LENGTH).alias("snippet"),
        F.col("e.sender"),
        F.col("e.date"),
        F.col("e.subject"),
        F.coalesce(F.size("e.to_recipients"), F.lit(0)).alias("recipient_count"),
        strat_c_base["effective_from"],
        strat_c_base["effective_to"],
    )
    .dropDuplicates(["message_id", "person_id", "reports_to_id"])
)
print(f"Strategy C candidates: {strat_c.count()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Strategy D — Keyword Co-Occurrence

# COMMAND ----------

# DBTITLE 1,Strategy D: Both names appear in email body
person_names = (
    spark.table(ORG_TABLE)
    .select("person_id", "name")
    .distinct()
)

pairs_with_both_names = (
    hierarchy_pairs.alias("h")
    .join(person_names.alias("pn"),
          F.col("h.person_id") == F.col("pn.person_id"), "left")
    .select(
        F.col("h.person_id"),
        F.col("h.reports_to_id"),
        F.col("pn.name").alias("subordinate_name"),
        F.col("h.effective_from"),
        F.col("h.effective_to"),
    )
    .join(person_names.alias("mn"),
          F.col("h.reports_to_id") == F.col("mn.person_id"), "left")
    .select(
        "person_id", "reports_to_id", "subordinate_name",
        F.col("mn.name").alias("manager_name_full"),
        "effective_from", "effective_to",
    )
)

strat_d_parts = []
for row in pairs_with_both_names.collect():
    sub_name = row["subordinate_name"]
    mgr_name = row["manager_name_full"]
    if not sub_name or not mgr_name:
        continue
    sub_last = sub_name.split()[-1] if sub_name else ""
    mgr_last = mgr_name.split()[-1] if mgr_name else ""
    if not sub_last or not mgr_last:
        continue

    matched = (
        emails_df
        .filter(
            F.lower(F.col("body")).contains(sub_last.lower())
            & F.lower(F.col("body")).contains(mgr_last.lower())
        )
        .limit(MAX_EMAILS_PER_PAIR)
        .select(
            F.lit(row["person_id"]).alias("person_id"),
            F.lit(row["reports_to_id"]).alias("reports_to_id"),
            F.col("message_id"),
            F.col("thread_id"),
            F.lit("D").alias("evidence_strategy"),
            F.lit(float(STRATEGY_WEIGHTS["D"])).alias("base_score"),
            F.substring(F.col("body"), 1, SNIPPET_LENGTH).alias("snippet"),
            F.col("sender"),
            F.col("date"),
            F.col("subject"),
            F.coalesce(F.size("to_recipients"), F.lit(0)).alias("recipient_count"),
            F.lit(row["effective_from"]).cast(DateType()).alias("effective_from"),
            F.lit(row["effective_to"]).cast(DateType()).alias("effective_to"),
        )
    )
    strat_d_parts.append(matched)

if strat_d_parts:
    strat_d = reduce(lambda a, b: a.unionByName(b), strat_d_parts)
    strat_d = strat_d.dropDuplicates(["message_id", "person_id", "reports_to_id"])
    print(f"Strategy D candidates: {strat_d.count()}")
else:
    strat_d = spark.createDataFrame([], strat_a.schema)
    print("Strategy D candidates: 0")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Union, Score, and Write

# COMMAND ----------

# DBTITLE 1,Combine all strategies and compute final relevance_score
all_evidence = (
    strat_a.unionByName(strat_b)
    .unionByName(strat_c)
    .unionByName(strat_d)
    .dropDuplicates(["message_id", "person_id", "reports_to_id"])
)

ORG_KEYWORDS = [
    "report to", "reports to", "direct report", "reporting to",
    "team", "department", "manager", "supervisor", "subordinate",
]
org_kw_condition = reduce(
    lambda a, b: a | b,
    [F.lower(F.col("snippet")).contains(kw) for kw in ORG_KEYWORDS]
)

scored = (
    all_evidence
    .withColumn("mass_penalty",
        F.when(F.col("recipient_count") > MASS_MAIL_THRESHOLD, F.lit(-0.3))
         .otherwise(F.lit(0.0)))
    .withColumn("date_boost",
        F.when(
            (F.col("date").between(
                F.date_sub(F.col("effective_from"), DATE_PROXIMITY_WINDOW),
                F.date_add(F.col("effective_to"), DATE_PROXIMITY_WINDOW)
            )),
            F.lit(DATE_PROXIMITY_BOOST)
        ).otherwise(F.lit(0.0)))
    .withColumn("keyword_boost",
        F.when(org_kw_condition, F.lit(ORG_KEYWORD_BOOST)).otherwise(F.lit(0.0)))
    .withColumn("relevance_score",
        F.greatest(
            F.lit(0.0),
            F.least(F.lit(1.0),
                F.col("base_score") + F.col("mass_penalty")
                + F.col("date_boost") + F.col("keyword_boost"))
        ))
    .filter(F.col("relevance_score") >= MIN_RELEVANCE)
)

# COMMAND ----------

# DBTITLE 1,Rank and cap per pair
from pyspark.sql.window import Window

w = Window.partitionBy("person_id", "reports_to_id").orderBy(F.desc("relevance_score"))

final_evidence = (
    scored
    .withColumn("rank", F.row_number().over(w))
    .filter(F.col("rank") <= MAX_EMAILS_PER_PAIR)
    .select(
        "person_id", "reports_to_id", "message_id", "thread_id",
        "evidence_strategy", "relevance_score", "snippet",
        "sender", "date", "subject",
    )
)

# COMMAND ----------

# DBTITLE 1,Write Evidence Table
final_evidence.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(EVIDENCE_TABLE)

total_rows = spark.table(EVIDENCE_TABLE).count()
covered_pairs = (
    spark.table(EVIDENCE_TABLE)
    .select("person_id", "reports_to_id")
    .distinct()
    .count()
)
print(f"Evidence table: {total_rows} rows covering {covered_pairs}/{pair_count} hierarchy pairs → {EVIDENCE_TABLE}")

# COMMAND ----------

# DBTITLE 1,Coverage Summary by Strategy
display(
    spark.table(EVIDENCE_TABLE)
    .groupBy("evidence_strategy")
    .agg(
        F.count("*").alias("evidence_count"),
        F.countDistinct("person_id", "reports_to_id").alias("pairs_covered"),
        F.round(F.avg("relevance_score"), 3).alias("avg_relevance"),
    )
    .orderBy("evidence_strategy")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Evidence linking complete. The `org_hierarchy_evidence` table maps curated
# MAGIC hierarchy claims to source emails with relevance scores. Consumed by the
# MAGIC `get_hierarchy_evidence` agent tool.
