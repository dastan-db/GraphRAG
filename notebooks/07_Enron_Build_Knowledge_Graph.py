# Databricks notebook source
# MAGIC %md
# MAGIC # 07 — Enron Build Knowledge Graph
# MAGIC
# MAGIC Extract entities and relationships from Enron email threads using parallelized
# MAGIC Spark SQL `ai_query()`, then store results in Delta tables. Follows the same
# MAGIC pattern as `02_Build_Knowledge_Graph.py` but with corporate extraction prompts
# MAGIC and email-level traceability.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 networkx --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration and Utilities
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %run ../src/extraction/extraction

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import (
    ArrayType,
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

slugify_udf = F.udf(slugify, StringType())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Prepare Thread Texts
# MAGIC
# MAGIC The `threads` table was built by `06_Enron_Data_Prep`. Each thread
# MAGIC aggregates emails in chronological order — analogous to chapter text
# MAGIC in the Bible pipeline.

# COMMAND ----------

# DBTITLE 1,Verify Thread Data
threads_table = config['enron_threads_table']
emails_table = config['enron_emails_table']

thread_count = spark.table(threads_table).count()
email_count = spark.table(emails_table).count()
print(f"Threads: {thread_count:,}  |  Emails: {email_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Entity Extraction via ai_query()
# MAGIC
# MAGIC Uses `ai_query()` with corporate extraction prompts on thread-level text.

# COMMAND ----------

# DBTITLE 1,Extract Entities from All Threads (Parallel)
llm_endpoint = config['llm_endpoint']
entity_prompt = CORPORATE_ENTITY_PROMPT_PREFIX.replace("'", "''")

enron_schema = config['enron_schema']
raw_entities_table = f"{config['catalog']}.{enron_schema}.raw_entities_temp"

_entities_exist = False
try:
    _entities_exist = spark.catalog.tableExists(raw_entities_table) and spark.table(raw_entities_table).count() > 0
except Exception:
    pass

if not _entities_exist:
    print("Running corporate entity extraction for all threads...")
    spark.sql(f"DROP TABLE IF EXISTS {raw_entities_table}")
    spark.sql(f"""
        SELECT
            thread_id,
            subject,
            ai_query(
                '{llm_endpoint}',
                CONCAT(
                    '{entity_prompt}',
                    'Email Thread Subject: ', COALESCE(subject, '(no subject)'),
                    '\\nParticipants: ', CONCAT_WS(', ', participants),
                    '\\n\\nThread Text:\\n', SUBSTRING(thread_text, 1, 6000)
                ),
                responseFormat => 'STRUCT<result:STRUCT<entities:ARRAY<STRUCT<name:STRING,entity_type:STRING,description:STRING>>>>',
                modelParameters => named_struct('temperature', 0.1, 'max_tokens', 4096),
                failOnError => false
            ) AS extracted
        FROM {threads_table}
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(raw_entities_table)
else:
    print(f"Raw entities table already has data — SKIPPING extraction")

raw_entities_df = spark.table(raw_entities_table)
print(f"Entity extraction complete for {raw_entities_df.count()} threads")

# COMMAND ----------

# DBTITLE 1,Parse and Flatten Extracted Entities
from pyspark.sql.functions import from_json

entity_mentions_temp_table = f"{config['catalog']}.{enron_schema}.entity_mentions_all_temp"

entities_schema = ArrayType(
    StructType([
        StructField("name", StringType()),
        StructField("entity_type", StringType()),
        StructField("description", StringType()),
    ])
)
entity_result_schema = StructType([
    StructField("entities", entities_schema)
])

parsed_entities_df = raw_entities_df.withColumn(
    "result_struct",
    from_json(F.col("extracted.result"), entity_result_schema),
)

# ── Entity type normalization + email-as-Location fix ──
# Maps hallucinated/rare types to a canonical taxonomy and reclassifies
# email addresses from Location to Person.
def _normalize_entity_type(entity_type, name):
    import re
    CANONICAL = {"Person", "Organization", "Location", "Project", "Document",
                 "Event", "Meeting", "Division", "Financial_Event", "Product"}
    if name and re.match(r'^[\w.+-]+@[\w.-]+\.\w+$', name.strip()):
        return "Person"
    if not entity_type or entity_type.strip() in ("", "None", "null"):
        return "Other"
    et = entity_type.strip()
    if et in CANONICAL:
        return et
    et_lower = et.lower().replace(" ", "_")
    MAPPING = {
        "company": "Organization", "corp": "Organization", "corporation": "Organization",
        "firm": "Organization", "agency": "Organization", "institution": "Organization",
        "university": "Organization", "school": "Organization",
        "city": "Location", "state": "Location", "country": "Location",
        "region": "Location", "place": "Location",
        "date": "Event", "time_period": "Event", "semester": "Event",
        "class": "Event", "course": "Event",
        "program": "Project", "initiative": "Project", "system": "Product",
        "conference": "Meeting", "workshop": "Meeting",
        "report": "Document", "publication": "Document", "website": "Document",
        "folder": "Document", "file": "Document",
        "group": "Organization", "team": "Organization", "family": "Organization",
    }
    for key, canonical in MAPPING.items():
        if key in et_lower:
            return canonical
    return "Other"

normalize_entity_type_udf = F.udf(_normalize_entity_type, StringType())

(
    parsed_entities_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "thread_id",
        "subject",
        F.explode("result_struct.entities").alias("entity"),
    )
    .select(
        "thread_id",
        "subject",
        F.col("entity.name").alias("name"),
        F.col("entity.entity_type").alias("entity_type"),
        F.col("entity.description").alias("description"),
    )
    .filter(F.trim(F.col("name")) != "")
    .withColumn("name", F.trim(F.col("name")))
    .withColumn("entity_type", normalize_entity_type_udf(F.col("entity_type"), F.col("name")))
    .withColumn("entity_id", slugify_udf(F.col("name")))
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(entity_mentions_temp_table)
)

entities_exploded_df = spark.table(entity_mentions_temp_table)
total_mentions = entities_exploded_df.count()
print(f"Total raw entity mentions: {total_mentions:,}")

# Show type distribution after normalization
print("\nEntity type distribution after normalization:")
entities_exploded_df.groupBy("entity_type").count().orderBy(F.desc("count")).show(15, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deduplicate Entities

# COMMAND ----------

# DBTITLE 1,Deduplicate and Write Entities Table
from pyspark.sql import Window

first_mention_window = Window.partitionBy("entity_id").orderBy("thread_id")

unique_entities_df = (
    entities_exploded_df
    .withColumn("rn", F.row_number().over(first_mention_window))
    .filter(F.col("rn") == 1)
    .select(
        "entity_id",
        "name",
        "entity_type",
        "description",
        F.col("thread_id").alias("first_mention_thread"),
        F.col("subject").alias("first_mention_subject"),
    )
)

(
    unique_entities_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entities_table'])
)

entity_count = spark.table(config['enron_entities_table']).count()
print(f"Wrote {entity_count:,} unique entities to {config['enron_entities_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3.5: Entity Resolution (AI-Powered)
# MAGIC
# MAGIC Two-stage entity resolution:
# MAGIC 1. **Custodian known-variant fix-ups** — hardcoded aliases for 25 key Enron figures (zero-cost, high-precision safety net)
# MAGIC 2. **AI-powered deduplication** — `ai_query()` with the 8B model evaluates candidate pairs (substring, Levenshtein, email-pattern matches) to decide merges
# MAGIC
# MAGIC Results are written to an `entity_aliases` table, then entities and the
# MAGIC exploded mentions table are rewritten with canonical IDs.

# COMMAND ----------

# DBTITLE 1,Stage 1: Custodian Known-Variant Fix-Ups
CUSTODIAN_FIXUPS = {
    "kenneth_lay": ["ken_lay", "dr_ken_lay", "dr_kenneth_lay", "kenny_lay",
                     "kenneth_l_lay", "k_lay", "kenneth_lay_enron_com"],
    "jeff_skilling": ["jeffrey_skilling", "jeffrey_k_skilling", "j_skilling",
                      "jeff_skilling_enron_com", "jeffrey_skilling_enron_com"],
    "andrew_fastow": ["andy_fastow", "andrew_s_fastow", "a_fastow",
                       "andrew_fastow_enron_com"],
    "david_delainey": ["dave_delainey", "d_delainey", "david_w_delainey"],
    "jeff_dasovich": ["jeffrey_dasovich", "j_dasovich"],
    "vince_kaminski": ["vincent_kaminski", "wincenty_kaminski", "v_kaminski",
                        "vince_j_kaminski"],
    "louise_kitchen": ["l_kitchen"],
    "sara_shackleton": ["s_shackleton", "sara_shackleton_enron_com"],
    "chris_germany": ["christopher_germany", "c_germany"],
    "eric_bass": ["e_bass"],
    "phillip_allen": ["phil_allen", "p_allen", "phillip_k_allen"],
    "john_arnold": ["j_arnold"],
    "sally_beck": ["s_beck", "sally_beck_enron_com"],
    "lynn_blair": ["l_blair"],
    "larry_campbell": ["l_campbell", "lawrence_campbell"],
    "sherron_watkins": ["s_watkins", "sherron_watkins_enron_com"],
    "richard_causey": ["rick_causey", "r_causey"],
    "rick_buy": ["r_buy", "richard_buy"],
    "tim_belden": ["t_belden", "timothy_belden"],
    "michael_kopper": ["m_kopper", "mike_kopper"],
    "greg_whalley": ["g_whalley", "gregory_whalley"],
    "cliff_baxter": ["c_baxter", "j_clifford_baxter"],
    "kenneth_rice": ["ken_rice", "k_rice"],
    "mark_frevert": ["m_frevert"],
    "rebecca_mark": ["r_mark", "rebecca_mark_jusbasche"],
    # ── Corporate entity aliases (high-value non-person merges) ──
    "enron_corp": ["enron", "enron_corporation", "enron_inc", "enron_company"],
    "enron_energy_services": ["ees"],
    "enron_broadband_services": ["ebs", "enron_broadband"],
    "federal_energy_regulatory_commission": ["ferc"],
    "california_public_utilities_commission": ["cpuc"],
    "pacific_gas_and_electric": ["pg_e", "pge", "pacific_gas_electric"],
}

custodian_rows = []
for canonical, variants in CUSTODIAN_FIXUPS.items():
    for v in variants:
        if v != canonical:
            custodian_rows.append((v, canonical))

custodian_aliases_df = spark.createDataFrame(custodian_rows, ["alias_id", "canonical_id"])
custodian_alias_count = custodian_aliases_df.count()
print(f"Stage 1 (custodian fix-ups): {custodian_alias_count:,} alias mappings")

# COMMAND ----------

# DBTITLE 1,Stage 2: AI-Powered Entity Deduplication
from pyspark.sql import Window as W

entities_df = spark.table(config['enron_entities_table'])
small_llm = config['small_llm_endpoint']
enron_schema = config['enron_schema']
ai_merge_table = f"{config['catalog']}.{enron_schema}.ai_entity_merge_temp"

_ai_merge_done = False
try:
    _ai_merge_done = spark.catalog.tableExists(ai_merge_table) and spark.table(ai_merge_table).count() > 0
except Exception:
    pass

if not _ai_merge_done:
    # Candidate generation with prefix blocking + proportional Levenshtein.
    # Previous version used LEVENSHTEIN <= 3 without blocking, producing 156K+ pairs.
    # Now: require same entity_type AND (shared 3-char prefix OR substring containment)
    # AND Levenshtein distance <= 20% of shorter name length (min 2).
    spark.sql(f"""
        CREATE OR REPLACE TEMPORARY VIEW entity_candidate_pairs AS
        SELECT
            e1.entity_id AS id_a, e1.name AS name_a, e1.entity_type AS type_a,
            e2.entity_id AS id_b, e2.name AS name_b, e2.entity_type AS type_b
        FROM {config['enron_entities_table']} e1
        JOIN {config['enron_entities_table']} e2
            ON e1.entity_id < e2.entity_id
            AND e1.entity_type = e2.entity_type
            AND (
                -- Prefix blocking: share first 3 characters of slug
                SUBSTRING(e1.entity_id, 1, 3) = SUBSTRING(e2.entity_id, 1, 3)
                -- OR substring containment (shorter name >= 4 chars)
                OR (
                    LENGTH(e1.entity_id) >= 4 AND LENGTH(e2.entity_id) >= 4
                    AND (
                        e1.entity_id LIKE CONCAT('%%', e2.entity_id, '%%')
                        OR e2.entity_id LIKE CONCAT('%%', e1.entity_id, '%%')
                    )
                )
            )
            AND LEVENSHTEIN(e1.entity_id, e2.entity_id) <=
                GREATEST(2, CAST(LEAST(LENGTH(e1.entity_id), LENGTH(e2.entity_id)) * 0.2 AS INT))
    """)

    candidate_count = spark.sql("SELECT COUNT(*) FROM entity_candidate_pairs").collect()[0][0]
    print(f"AI entity resolution candidates: {candidate_count:,} pairs")

    if candidate_count > 0 and candidate_count <= 50000:
        print("Running ai_query() entity merge assessment...")
        spark.sql(f"DROP TABLE IF EXISTS {ai_merge_table}")
        spark.sql(f"""
            SELECT
                id_a, name_a, type_a,
                id_b, name_b, type_b,
                ai_query(
                    '{small_llm}',
                    CONCAT(
                        'Do these two entity names refer to the same real-world entity?\\n',
                        'Entity A: "', name_a, '" (type: ', type_a, ')\\n',
                        'Entity B: "', name_b, '" (type: ', type_b, ')\\n',
                        'Answer: YES <canonical_name> or NO\\n',
                        'If YES, state which name is the canonical (most complete, formal) form.\\n',
                        'Examples: YES Kenneth Lay | YES Enron Corp | NO'
                    ),
                    modelParameters => named_struct('temperature', 0.0, 'max_tokens', 32),
                    failOnError => false
                ) AS merge_decision
            FROM entity_candidate_pairs
        """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(ai_merge_table)
    else:
        # Write empty table with matching STRUCT schema for consistency
        empty_merge_schema = StructType([
            StructField("id_a", StringType()), StructField("name_a", StringType()),
            StructField("type_a", StringType()), StructField("id_b", StringType()),
            StructField("name_b", StringType()), StructField("type_b", StringType()),
            StructField("merge_decision", StructType([
                StructField("result", StringType()),
                StructField("errorMessage", StringType()),
            ])),
        ])
        spark.createDataFrame([], empty_merge_schema) \
            .write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(ai_merge_table)
        if candidate_count > 50000:
            print(f"Too many candidates ({candidate_count:,}) — further tighten blocking strategy")
        else:
            print("No candidates found")
else:
    print(f"AI entity merge already computed")

ai_merge_df = spark.table(ai_merge_table)
ai_yes_count = ai_merge_df.filter(F.upper(F.trim(F.col("merge_decision.result"))).like("YES%")).count()
print(f"AI approved merges: {ai_yes_count:,}")

# COMMAND ----------

# DBTITLE 1,Build AI-Derived Alias Pairs
ai_aliases = (
    spark.table(ai_merge_table)
    .filter(F.upper(F.trim(F.col("merge_decision.result"))).like("YES%"))
    .withColumn(
        "canonical_id",
        F.when(F.length(F.col("id_a")) >= F.length(F.col("id_b")), F.col("id_a"))
         .otherwise(F.col("id_b"))
    )
    .withColumn(
        "alias_id",
        F.when(F.length(F.col("id_a")) >= F.length(F.col("id_b")), F.col("id_b"))
         .otherwise(F.col("id_a"))
    )
    .select("alias_id", "canonical_id")
    .filter(F.col("alias_id") != F.col("canonical_id"))
    .distinct()
)

ai_alias_count = ai_aliases.count()
print(f"AI-derived aliases: {ai_alias_count:,}")

# COMMAND ----------

# DBTITLE 1,Combine Aliases and Resolve Chains
all_aliases = (
    custodian_aliases_df
    .unionByName(ai_aliases)
    .distinct()
)

canonical_ids = entities_df.select("entity_id").distinct()
all_aliases = all_aliases.join(
    canonical_ids,
    all_aliases.canonical_id == canonical_ids.entity_id,
    "left_semi",
)

all_aliases = all_aliases.filter(F.col("alias_id") != F.col("canonical_id"))

alias_window = W.partitionBy("alias_id").orderBy(F.length("canonical_id").desc())
resolved_aliases = (
    all_aliases
    .withColumn("rn", F.row_number().over(alias_window))
    .filter(F.col("rn") == 1)
    .select("alias_id", "canonical_id")
)

alias_table = f"{config['catalog']}.{enron_schema}.entity_aliases"

(
    resolved_aliases.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(alias_table)
)

alias_count = spark.table(alias_table).count()
print(f"Wrote {alias_count:,} entity aliases to {alias_table}")

# COMMAND ----------

# DBTITLE 1,Build Entity Resolution Audit Table

audit_table = f"{config['catalog']}.{enron_schema}.entity_resolution_audit"

# Custodian-derived audit rows
custodian_audit = (
    custodian_aliases_df
    .withColumn("method", F.lit("custodian_hardcode"))
    .withColumn("blocking_reason", F.lit(None).cast("string"))
    .withColumn("ai_raw_response", F.lit(None).cast("string"))
    .withColumn("confidence", F.lit(1.0))
    .withColumn("created_at", F.current_timestamp())
)

# AI-derived audit rows
ai_audit = (
    spark.table(ai_merge_table)
    .filter(F.upper(F.trim(F.col("merge_decision.result"))).like("YES%"))
    .withColumn(
        "canonical_id",
        F.when(F.length(F.col("id_a")) >= F.length(F.col("id_b")), F.col("id_a"))
         .otherwise(F.col("id_b"))
    )
    .withColumn(
        "alias_id",
        F.when(F.length(F.col("id_a")) >= F.length(F.col("id_b")), F.col("id_b"))
         .otherwise(F.col("id_a"))
    )
    .filter(F.col("alias_id") != F.col("canonical_id"))
    .select(
        "alias_id", "canonical_id",
        F.lit("ai_powered").alias("method"),
        F.lit("levenshtein").alias("blocking_reason"),
        F.col("merge_decision.result").alias("ai_raw_response"),
        F.lit(0.85).alias("confidence"),
        F.current_timestamp().alias("created_at"),
    )
)

(
    custodian_audit
    .unionByName(ai_audit)
    .write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(audit_table)
)

audit_count = spark.table(audit_table).count()
print(f"Wrote {audit_count:,} entity resolution audit rows to {audit_table}")

# COMMAND ----------

# DBTITLE 1,Rewrite Entity Mentions with Canonical IDs
aliases_df = spark.table(alias_table)

entities_exploded_df = (
    spark.table(entity_mentions_temp_table)
    .join(aliases_df, F.col("entity_id") == aliases_df.alias_id, "left")
    .withColumn("entity_id", F.coalesce(aliases_df.canonical_id, F.col("entity_id")))
    .drop("alias_id", "canonical_id")
)

(
    entities_exploded_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(entity_mentions_temp_table)
)

print(f"Rewrote entity mentions with canonical IDs")

# COMMAND ----------

# DBTITLE 1,Rebuild Deduplicated Entities with Merged Aliases
first_mention_window_2 = Window.partitionBy("entity_id").orderBy("thread_id")

unique_entities_df = (
    entities_exploded_df
    .withColumn("rn", F.row_number().over(first_mention_window_2))
    .filter(F.col("rn") == 1)
    .select(
        "entity_id",
        "name",
        "entity_type",
        "description",
        F.col("thread_id").alias("first_mention_thread"),
        F.col("subject").alias("first_mention_subject"),
    )
)

(
    unique_entities_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entities_table'])
)

resolved_entity_count = spark.table(config['enron_entities_table']).count()
print(f"After entity resolution: {resolved_entity_count:,} entities (was {entity_count:,}, merged {entity_count - resolved_entity_count:,})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Relationship Extraction
# MAGIC
# MAGIC Two relationship sources:
# MAGIC 1. **Structural** — SENT_TO/CC_TO from email metadata (free, no LLM cost)
# MAGIC 2. **Semantic** — LLM-extracted relationships from email body text

# COMMAND ----------

# DBTITLE 1,Structural Relationships from Email Metadata
emails_df = spark.table(emails_table)
participants_df = spark.table(config['enron_participants_table'])

sent_to_rels = (
    emails_df
    .select(
        F.col("sender"),
        F.explode(F.col("to_recipients")).alias("recipient"),
        F.col("thread_id"),
    )
    .withColumn("source_entity", slugify_udf(F.col("sender")))
    .withColumn("target_entity", slugify_udf(F.col("recipient")))
    .groupBy("source_entity", "target_entity")
    .agg(
        F.count("*").alias("weight"),
        F.lit("SENT_TO").alias("relationship_type"),
        F.concat(
            F.lit("Sent "), F.count("*").cast(StringType()), F.lit(" emails")
        ).alias("description"),
        F.first("thread_id").alias("thread_id"),
    )
    .filter(
        (F.col("source_entity").isNotNull()) &
        (F.col("source_entity") != "") &
        (F.col("target_entity").isNotNull()) &
        (F.col("target_entity") != "") &
        (F.col("source_entity") != F.col("target_entity"))
    )
    .select("source_entity", "target_entity", "relationship_type", "description", "thread_id")
)

structural_count = sent_to_rels.count()
print(f"Structural SENT_TO relationships: {structural_count:,}")

# COMMAND ----------

# DBTITLE 1,Build Thread Entity Lists for Semantic Extraction
chapter_entity_names_df = (
    entities_exploded_df
    .groupBy("thread_id")
    .agg(
        F.concat_ws("\n- ", F.collect_set("name")).alias("entity_names"),
        F.count("*").alias("entity_count"),
    )
    .filter(F.col("entity_count") >= 2)
)

chapter_entity_names_df.createOrReplaceTempView("thread_entities")
spark.table(threads_table).createOrReplaceTempView("threads")

threads_with_entities = chapter_entity_names_df.count()
print(f"Threads with 2+ entities for relationship extraction: {threads_with_entities:,}")

# COMMAND ----------

# DBTITLE 1,Semantic Relationship Extraction (Parallel)
rel_prompt = CORPORATE_RELATIONSHIP_PROMPT_PREFIX.replace("'", "''")

raw_rels_table = f"{config['catalog']}.{enron_schema}.raw_relationships_temp"

_rels_exist = False
try:
    _rels_exist = spark.catalog.tableExists(raw_rels_table) and spark.table(raw_rels_table).count() > 0
except Exception:
    pass

if not _rels_exist:
    print("Running corporate relationship extraction...")
    spark.sql(f"DROP TABLE IF EXISTS {raw_rels_table}")
    spark.sql(f"""
        SELECT
            t.thread_id,
            t.subject,
            ai_query(
                '{llm_endpoint}',
                CONCAT(
                    '{rel_prompt}',
                    'Email Thread Subject: ', COALESCE(t.subject, '(no subject)'),
                    '\\n\\nEntities found in this thread:\\n- ', e.entity_names,
                    '\\n\\nThread Text:\\n', SUBSTRING(t.thread_text, 1, 6000)
                ),
                responseFormat => 'STRUCT<result:STRUCT<relationships:ARRAY<STRUCT<source:STRING,target:STRING,relationship_type:STRING,description:STRING>>>>',
                modelParameters => named_struct('temperature', 0.1, 'max_tokens', 4096),
                failOnError => false
            ) AS extracted
        FROM threads t
        JOIN thread_entities e ON t.thread_id = e.thread_id
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(raw_rels_table)
else:
    print(f"Raw relationships table already has data — SKIPPING extraction")

raw_rels_df = spark.table(raw_rels_table)
print(f"Relationship extraction complete for {raw_rels_df.count()} threads")

# COMMAND ----------

# DBTITLE 1,Parse, Flatten, and Combine All Relationships
relationships_schema = ArrayType(
    StructType([
        StructField("source", StringType()),
        StructField("target", StringType()),
        StructField("relationship_type", StringType()),
        StructField("description", StringType()),
    ])
)
rel_result_schema = StructType([
    StructField("relationships", relationships_schema)
])

parsed_rels_df = raw_rels_df.withColumn(
    "result_struct",
    from_json(F.col("extracted.result"), rel_result_schema),
)

semantic_rels = (
    parsed_rels_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "thread_id",
        F.explode("result_struct.relationships").alias("rel"),
    )
    .select(
        slugify_udf(F.trim(F.col("rel.source"))).alias("source_entity"),
        slugify_udf(F.trim(F.col("rel.target"))).alias("target_entity"),
        F.coalesce(F.col("rel.relationship_type"), F.lit("RELATED_TO")).alias("relationship_type_raw"),
        F.col("rel.description").alias("description"),
        "thread_id",
    )
    .filter(
        (F.col("source_entity").isNotNull()) &
        (F.col("source_entity") != "") &
        (F.col("target_entity").isNotNull()) &
        (F.col("target_entity") != "")
    )
)

normalize_rel_udf = F.udf(normalize_corporate_rel_type, StringType())
semantic_rels = semantic_rels.withColumn(
    "relationship_type", normalize_rel_udf(F.col("relationship_type_raw"))
).drop("relationship_type_raw")

raw_type_count = semantic_rels.select("relationship_type").distinct().count()
print(f"Normalized relationship types to {raw_type_count} canonical types")

all_rels_raw = sent_to_rels.unionByName(semantic_rels)

# Apply entity resolution aliases to relationship source/target
aliases_df = spark.table(f"{config['catalog']}.{enron_schema}.entity_aliases")
all_rels_resolved = (
    all_rels_raw
    .join(
        aliases_df.select(F.col("alias_id").alias("src_alias"), F.col("canonical_id").alias("src_canonical")),
        F.col("source_entity") == F.col("src_alias"),
        "left",
    )
    .withColumn("source_entity", F.coalesce(F.col("src_canonical"), F.col("source_entity")))
    .drop("src_alias", "src_canonical")
    .join(
        aliases_df.select(F.col("alias_id").alias("tgt_alias"), F.col("canonical_id").alias("tgt_canonical")),
        F.col("target_entity") == F.col("tgt_alias"),
        "left",
    )
    .withColumn("target_entity", F.coalesce(F.col("tgt_canonical"), F.col("target_entity")))
    .drop("tgt_alias", "tgt_canonical")
    .filter(F.col("source_entity") != F.col("target_entity"))
)

# Deduplicate: collapse (source, target, type) triples into one edge with count.
# Preserve ALL source thread_ids for evidentiary provenance (legal audit).
all_rels = (
    all_rels_resolved
    .groupBy("source_entity", "target_entity", "relationship_type")
    .agg(
        F.first("description").alias("description"),
        F.collect_set("thread_id").alias("source_threads"),
        F.count("*").alias("edge_count"),
        F.min("thread_id").alias("first_observed"),
        F.max("thread_id").alias("last_observed"),
        F.when(
            F.first("relationship_type").isin(
                "SENT_TO", "REPORTS_TO", "EMPLOYED_BY", "MANAGES"
            ),
            F.lit("structural"),
        )
        .otherwise(F.lit("semantic"))
        .alias("evidence_type"),
        F.when(
            F.first("relationship_type").isin(
                "SENT_TO", "REPORTS_TO", "EMPLOYED_BY", "MANAGES"
            ),
            F.lit(1.0),
        )
        .otherwise(
            F.least(
                F.lit(0.95),
                F.lit(0.5) + F.count("*").cast("float") * F.lit(0.1),
            )
        )
        .alias("confidence"),
    )
)

(
    all_rels.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_relationships_table'])
)

rel_count = spark.table(config['enron_relationships_table']).count()
raw_count = all_rels_raw.count()
print(f"Wrote {rel_count:,} deduplicated relationships (from {raw_count:,} raw) to {config['enron_relationships_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions Table
# MAGIC
# MAGIC Link entities back to specific emails for source traceability.

# COMMAND ----------

# DBTITLE 1,Build Entity Mentions via Thread Join
# The entity_mentions_all_temp table already maps (entity_id, thread_id) from the
# extraction output. Join through thread_id to emails to get per-email mentions —
# avoids the O(entities * emails) cross-join string search.
(
    spark.table(entity_mentions_temp_table)
    .select("entity_id", "thread_id")
    .join(
        emails_df.select("message_id", "thread_id"),
        on="thread_id",
        how="inner",
    )
    .select("entity_id", "message_id", "thread_id")
    .distinct()
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(config['enron_entity_mentions_table'])
)

mention_count = spark.table(config['enron_entity_mentions_table']).count()
print(f"Wrote {mention_count:,} entity mentions to {config['enron_entity_mentions_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Knowledge Graph Statistics

# COMMAND ----------

# DBTITLE 1,Entity Counts by Type
display(
    spark.table(config['enron_entities_table'])
    .groupBy("entity_type")
    .count()
    .orderBy(F.desc("count"))
)

# COMMAND ----------

# DBTITLE 1,Top Entities by Mention Count
display(
    spark.table(config['enron_entity_mentions_table'])
    .groupBy("entity_id")
    .agg(F.count("*").alias("mention_count"))
    .join(spark.table(config['enron_entities_table']), "entity_id")
    .select("name", "entity_type", "mention_count")
    .orderBy(F.desc("mention_count"))
    .limit(20)
)

# COMMAND ----------

# DBTITLE 1,Relationship Type Distribution
display(
    spark.table(config['enron_relationships_table'])
    .groupBy("relationship_type")
    .count()
    .orderBy(F.desc("count"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: Graph Analytics (NetworkX)

# COMMAND ----------

# DBTITLE 1,Degree Centrality
import networkx as nx

rels = spark.table(config['enron_relationships_table'])
entities = spark.table(config['enron_entities_table'])

in_deg = (
    rels.groupBy(F.col("target_entity").alias("entity_id"))
    .agg(F.count("*").alias("in_degree"))
)
out_deg = (
    rels.groupBy(F.col("source_entity").alias("entity_id"))
    .agg(F.count("*").alias("out_degree"))
)

degrees_df = (
    entities.select("entity_id")
    .join(in_deg, "entity_id", "left")
    .join(out_deg, "entity_id", "left")
    .fillna(0, subset=["in_degree", "out_degree"])
    .withColumn("total_degree", F.col("in_degree") + F.col("out_degree"))
)

# COMMAND ----------

# DBTITLE 1,PageRank (NetworkX)
# Prune dangling references: only keep edges where both endpoints exist
# in the entities table. Previous run included 22K+ phantom nodes (email
# addresses from SENT_TO that were never extracted as entities).
entity_ids_df = entities.select(F.col("entity_id"))

distinct_edges = (
    rels.select(
        F.col("source_entity").alias("src"),
        F.col("target_entity").alias("dst"),
    )
    .distinct()
    .join(entity_ids_df, F.col("src") == F.col("entity_id"), "inner").drop("entity_id")
    .join(entity_ids_df, F.col("dst") == F.col("entity_id"), "inner").drop("entity_id")
)

edges_pdf = distinct_edges.toPandas()
G = nx.DiGraph()
G.add_edges_from(zip(edges_pdf["src"], edges_pdf["dst"]))

for eid in entities.select("entity_id").toPandas()["entity_id"]:
    if eid not in G:
        G.add_node(eid)

if len(G.nodes()) == 0:
    raise ValueError(
        f"Graph has 0 nodes — entity extraction produced no results. "
        f"Check that {config['enron_entities_table']} is populated and ai_query() calls succeeded."
    )

pagerank = nx.pagerank(G, alpha=0.85, max_iter=20)

pr_schema = StructType([StructField("entity_id", StringType()), StructField("pagerank", DoubleType())])
pr_df = spark.createDataFrame(
    [(k, float(v)) for k, v in pagerank.items()],
    pr_schema,
)
print(f"PageRank computed for {pr_df.count():,} entities (graph: {len(G.nodes()):,} nodes, {len(G.edges()):,} edges)")

# COMMAND ----------

# DBTITLE 1,Join and Write entity_analytics Table
entity_analytics_df = (
    entities.select("entity_id", "name", "entity_type")
    .join(pr_df, "entity_id", "left")
    .join(degrees_df, "entity_id", "left")
    .fillna(0, subset=["pagerank", "in_degree", "out_degree", "total_degree"])
    .select(
        "entity_id", "name", "entity_type",
        F.col("pagerank").cast(DoubleType()),
        "in_degree", "out_degree", "total_degree",
    )
)

(
    entity_analytics_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entity_analytics_table'])
)

analytics_count = spark.table(config['enron_entity_analytics_table']).count()
print(f"Wrote {analytics_count:,} entity analytics to {config['enron_entity_analytics_table']}")

display(
    spark.table(config['enron_entity_analytics_table'])
    .orderBy(F.desc("pagerank"))
    .limit(15)
)

# COMMAND ----------

# DBTITLE 1,Entity Paths — On-Demand (skip precomputation)
# With 24K+ entities, precomputing all-pairs BFS is prohibitive (O(V*E), hundreds
# of millions of rows). Instead, the MCP server and agent compute shortest paths
# on-demand using NetworkX at query time (<1ms per BFS call).
# We write an empty table here so the schema exists for downstream consumers.

paths_schema = StructType([
    StructField("source_id", StringType()),
    StructField("target_id", StringType()),
    StructField("distance", IntegerType()),
    StructField("path_names", StringType()),
])
empty_paths = spark.createDataFrame([], paths_schema)

(
    empty_paths.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entity_paths_table'])
)

print(f"Entity paths table created (empty — paths computed on-demand at query time)")

# COMMAND ----------

# DBTITLE 1,Most Connected Entities
display(
    spark.table(config['enron_entity_analytics_table'])
    .orderBy(F.desc("total_degree"))
    .limit(15)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8: Entity Quality Filtering (AI-Powered)
# MAGIC
# MAGIC Remove low-value noise entities (e.g., "Outlook Migration Team", "Evening MBA",
# MAGIC "Top Career Forums") using `ai_query()`. Person entities are kept unconditionally
# MAGIC since they form the core of the communication graph.

# COMMAND ----------

# DBTITLE 1,AI Entity Relevance Filtering
small_llm = config['small_llm_endpoint']
enron_schema = config['enron_schema']
filter_table = f"{config['catalog']}.{enron_schema}.entity_relevance_temp"

_filter_done = False
try:
    _filter_done = spark.catalog.tableExists(filter_table) and spark.table(filter_table).count() > 0
except Exception:
    pass

if not _filter_done:
    non_person_count = spark.table(config['enron_entities_table']).filter("entity_type != 'Person'").count()
    print(f"Evaluating {non_person_count:,} non-Person entities for relevance...")

    spark.sql(f"DROP TABLE IF EXISTS {filter_table}")
    spark.sql(f"""
        SELECT
            entity_id,
            name,
            entity_type,
            description,
            ai_query(
                '{small_llm}',
                CONCAT(
                    'Is this entity relevant to understanding Enron corporate communications, ',
                    'organizational structure, or business operations?\\n',
                    'Entity: "', name, '" (type: ', entity_type, ')\\n',
                    'Description: ', COALESCE(description, 'none'), '\\n',
                    'Answer KEEP or REMOVE (one word only).'
                ),
                modelParameters => named_struct('temperature', 0.0, 'max_tokens', 8),
                failOnError => false
            ) AS relevance
        FROM {config['enron_entities_table']}
        WHERE entity_type != 'Person'
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(filter_table)

    remove_count = spark.table(filter_table).filter(
        F.upper(F.trim(F.col("relevance.result"))).like("REMOVE%")
    ).count()
    print(f"Entities marked for removal: {remove_count:,}/{non_person_count:,}")
else:
    remove_count = spark.table(filter_table).filter(
        F.upper(F.trim(F.col("relevance.result"))).like("REMOVE%")
    ).count()
    print(f"Entity filtering already done ({remove_count:,} marked for removal)")

# COMMAND ----------

# DBTITLE 1,Apply Entity Filter
remove_ids = (
    spark.table(filter_table)
    .filter(F.upper(F.trim(F.col("relevance.result"))).like("REMOVE%"))
    .select("entity_id")
)

# ── Educational/coursework noise filter ──
# Entities from MBA coursework ("Team Assignment", "Problem Set", etc.) dominate
# top mentions from a single custodian's mailbox. Remove by pattern matching.
edu_patterns = ["team_assignment", "problem_set", "midterm", "final_exam",
                "homework", "quiz_", "lecture_notes", "syllabus",
                "class_schedule", "course_outline", "study_group"]
edu_filter = F.lit(False)
for pattern in edu_patterns:
    edu_filter = edu_filter | F.col("entity_id").contains(pattern)

edu_remove_ids = (
    spark.table(config['enron_entities_table'])
    .filter(edu_filter)
    .select("entity_id")
)

remove_ids = remove_ids.unionByName(edu_remove_ids).distinct()

before_count = spark.table(config['enron_entities_table']).count()

filtered_entities = (
    spark.table(config['enron_entities_table'])
    .join(remove_ids, "entity_id", "left_anti")
)

(
    filtered_entities.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entities_table'])
)

after_count = spark.table(config['enron_entities_table']).count()
print(f"Entity filtering: {before_count:,} → {after_count:,} (removed {before_count - after_count:,} noise entities)")

filtered_rels = (
    spark.table(config['enron_relationships_table'])
    .join(remove_ids.withColumnRenamed("entity_id", "src_id"), F.col("source_entity") == F.col("src_id"), "left_anti")
    .join(remove_ids.withColumnRenamed("entity_id", "tgt_id"), F.col("target_entity") == F.col("tgt_id"), "left_anti")
)

rels_before = spark.table(config['enron_relationships_table']).count()

(
    filtered_rels.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_relationships_table'])
)

rels_after = spark.table(config['enron_relationships_table']).count()
print(f"Relationships: {rels_before:,} → {rels_after:,} (removed {rels_before - rels_after:,} edges to noise entities)")

# COMMAND ----------

# DBTITLE 1,Step 8b: Refresh Graph Analytics
# MAGIC %md
# MAGIC ## Step 8b: Refresh Graph Analytics (Post-Filter)
# MAGIC
# MAGIC Re-compute PageRank and degree centrality on the filtered entity/relationship
# MAGIC tables so that `entity_analytics` reflects the pruned graph.

# COMMAND ----------

# DBTITLE 1,Refresh entity_analytics After Quality Filter
# Re-run graph analytics on filtered data to keep entity_analytics in sync.
# Steps 7 ran before the quality filter — this refresh ensures consistency.
import networkx as nx

rels_filtered = spark.table(config['enron_relationships_table'])
entities_filtered = spark.table(config['enron_entities_table'])
entity_ids_df = entities_filtered.select(F.col("entity_id"))

# Degree centrality
in_deg = rels_filtered.groupBy(F.col("target_entity").alias("entity_id")).agg(F.count("*").alias("in_degree"))
out_deg = rels_filtered.groupBy(F.col("source_entity").alias("entity_id")).agg(F.count("*").alias("out_degree"))

degrees_df = (
    entities_filtered.select("entity_id")
    .join(in_deg, "entity_id", "left")
    .join(out_deg, "entity_id", "left")
    .fillna(0, subset=["in_degree", "out_degree"])
    .withColumn("total_degree", F.col("in_degree") + F.col("out_degree"))
)

# PageRank (pruned graph — only edges between existing entities)
distinct_edges = (
    rels_filtered.select(F.col("source_entity").alias("src"), F.col("target_entity").alias("dst")).distinct()
    .join(entity_ids_df, F.col("src") == F.col("entity_id"), "inner").drop("entity_id")
    .join(entity_ids_df, F.col("dst") == F.col("entity_id"), "inner").drop("entity_id")
)

edges_pdf = distinct_edges.toPandas()
G = nx.DiGraph()
G.add_edges_from(zip(edges_pdf["src"], edges_pdf["dst"]))
for eid in entities_filtered.select("entity_id").toPandas()["entity_id"]:
    if eid not in G:
        G.add_node(eid)

pagerank = nx.pagerank(G, alpha=0.85, max_iter=20)
pr_schema = StructType([StructField("entity_id", StringType()), StructField("pagerank", DoubleType())])
pr_df = spark.createDataFrame([(k, float(v)) for k, v in pagerank.items()], pr_schema)

# Write refreshed entity_analytics
entity_analytics_df = (
    entities_filtered.select("entity_id", "name", "entity_type")
    .join(pr_df, "entity_id", "left")
    .join(degrees_df, "entity_id", "left")
    .fillna(0, subset=["pagerank", "in_degree", "out_degree", "total_degree"])
    .select(
        "entity_id", "name", "entity_type",
        F.col("pagerank").cast(DoubleType()),
        "in_degree", "out_degree", "total_degree",
    )
)

(
    entity_analytics_df.write
    .format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(config['enron_entity_analytics_table'])
)

refreshed_count = spark.table(config['enron_entity_analytics_table']).count()
print(f"Refreshed entity_analytics: {refreshed_count:,} entities (graph: {len(G.nodes()):,} nodes, {len(G.edges()):,} edges)")

display(
    spark.table(config['enron_entity_analytics_table'])
    .orderBy(F.desc("pagerank"))
    .limit(15)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9: Thread Summarization (AI-Powered)
# MAGIC
# MAGIC Add per-thread summaries and topic tags using `ai_query()`. These enrich
# MAGIC the agent's context when searching emails and enable topic-based filtering.

# COMMAND ----------

# DBTITLE 1,AI Thread Summarization
threads_table = config['enron_threads_table']
summary_temp_table = f"{config['catalog']}.{enron_schema}.thread_summaries_temp"

_summary_col_exists = False
try:
    _cols = [f.name for f in spark.table(threads_table).schema.fields]
    _summary_col_exists = "summary" in _cols
except Exception:
    pass

if not _summary_col_exists:
    for col_name, col_type in [("summary", "STRING"), ("key_topics", "ARRAY<STRING>")]:
        try:
            spark.sql(f"ALTER TABLE {threads_table} ADD COLUMNS ({col_name} {col_type})")
        except Exception as _e:
            if "FIELD_ALREADY_EXISTS" not in str(_e) and "already exists" not in str(_e).lower():
                raise

_summaries_done = False
try:
    _summaries_done = spark.table(threads_table).filter("summary IS NOT NULL").count() > 0
except Exception:
    pass

if not _summaries_done:
    thread_count = spark.table(threads_table).count()
    print(f"Summarizing {thread_count:,} threads with ai_query()...")

    spark.sql(f"DROP TABLE IF EXISTS {summary_temp_table}")
    spark.sql(f"""
        SELECT
            thread_id,
            ai_query(
                '{small_llm}',
                CONCAT(
                    'Summarize this email thread in 2-3 sentences. ',
                    'Also list 1-3 key topics as short tags.\\n\\n',
                    'Subject: ', COALESCE(subject, '(no subject)'), '\\n',
                    'Participants: ', CONCAT_WS(', ', participants), '\\n\\n',
                    SUBSTRING(COALESCE(thread_text, ''), 1, 4000)
                ),
                responseFormat => 'STRUCT<result:STRUCT<summary:STRING, key_topics:ARRAY<STRING>>>',
                modelParameters => named_struct('temperature', 0.0, 'max_tokens', 256),
                failOnError => false
            ) AS summarized
        FROM {threads_table}
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(summary_temp_table)

    spark.sql(f"""
        MERGE INTO {threads_table} AS target
        USING (
            SELECT thread_id, parsed
            FROM (
                SELECT
                    thread_id,
                    from_json(summarized.result, 'STRUCT<summary:STRING, key_topics:ARRAY<STRING>>') AS parsed,
                    ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY thread_id) AS rn
                FROM {summary_temp_table}
                WHERE summarized.errorMessage IS NULL
            )
            WHERE rn = 1
        ) AS src
        ON target.thread_id = src.thread_id
        WHEN MATCHED THEN UPDATE SET
            target.summary = src.parsed.summary,
            target.key_topics = src.parsed.key_topics
    """)

    summarized_count = spark.table(threads_table).filter("summary IS NOT NULL").count()
    print(f"Thread summarization complete: {summarized_count:,}/{thread_count:,} threads summarized")
    spark.sql(f"DROP TABLE IF EXISTS {summary_temp_table}")
else:
    summarized_count = spark.table(threads_table).filter("summary IS NOT NULL").count()
    print(f"Thread summaries already present ({summarized_count:,} threads)")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Knowledge graph is built. The Enron corpus is now ready for agent queries.
