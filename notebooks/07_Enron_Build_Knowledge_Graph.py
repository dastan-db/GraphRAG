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
llm_endpoint = config['small_llm_endpoint']
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
    .withColumn("entity_id", slugify_udf(F.col("name")))
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(entity_mentions_temp_table)
)

entities_exploded_df = spark.table(entity_mentions_temp_table)
total_mentions = entities_exploded_df.count()
print(f"Total raw entity mentions: {total_mentions:,}")

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
# MAGIC ## Step 3.5: Entity Resolution
# MAGIC
# MAGIC Merge variant entity IDs that refer to the same real-world entity.
# MAGIC Three strategies applied in order:
# MAGIC 1. **Email-to-name linking** — join `slugify(email)` IDs against `slugify(x_from)` display names
# MAGIC 2. **Title/prefix stripping** — "Dr. Kenneth Lay" → "Kenneth Lay"
# MAGIC 3. **Substring containment** — `ken_lay` merges into `kenneth_lay` when same entity_type
# MAGIC
# MAGIC Results are written to an `entity_aliases` table, then entities and the
# MAGIC exploded mentions table are rewritten with canonical IDs.

# COMMAND ----------

# DBTITLE 1,Strategy 1: Email-to-Name Linking via x_from
_TITLE_PREFIXES = r"^(dr|mr|mrs|ms|prof|sir|rev)[\.\s]+"

def _strip_title(name_str):
    """Remove honorific prefixes before slugifying."""
    if name_str is None:
        return None
    import re as _re
    return _re.sub(_TITLE_PREFIXES, "", name_str.strip(), flags=_re.IGNORECASE).strip()

strip_title_udf = F.udf(_strip_title, StringType())

emails_for_alias = spark.table(emails_table).select(
    F.col("sender"),
    F.col("x_from"),
).filter(
    F.col("sender").isNotNull() & F.col("x_from").isNotNull()
    & (F.col("sender") != "") & (F.col("x_from") != "")
).distinct()

email_name_links = (
    emails_for_alias
    .withColumn("email_id", slugify_udf(F.col("sender")))
    .withColumn("name_id", slugify_udf(strip_title_udf(F.col("x_from"))))
    .filter(
        (F.col("email_id").isNotNull()) & (F.col("name_id").isNotNull())
        & (F.col("email_id") != F.col("name_id"))
        & (F.length(F.col("name_id")) > 2)
    )
    .select(
        F.col("email_id").alias("alias_id"),
        F.col("name_id").alias("canonical_id"),
    )
    .distinct()
)

email_link_count = email_name_links.count()
print(f"Strategy 1 (email→name): {email_link_count:,} alias mappings")

# COMMAND ----------

# DBTITLE 1,Strategy 2: Title/Prefix Stripping
entities_df = spark.table(config['enron_entities_table'])

title_aliases = (
    entities_df
    .withColumn("stripped_id", slugify_udf(strip_title_udf(F.col("name"))))
    .filter(
        (F.col("stripped_id").isNotNull())
        & (F.col("stripped_id") != F.col("entity_id"))
        & (F.length(F.col("stripped_id")) > 2)
    )
    .join(
        entities_df.select(F.col("entity_id").alias("canonical_id")),
        F.col("stripped_id") == F.col("canonical_id"),
        "inner",
    )
    .select(
        F.col("entity_id").alias("alias_id"),
        F.col("canonical_id"),
    )
    .distinct()
)

title_alias_count = title_aliases.count()
print(f"Strategy 2 (title strip): {title_alias_count:,} alias mappings")

# COMMAND ----------

# DBTITLE 1,Strategy 3: Substring Containment (same entity_type)
from pyspark.sql import Window as W

ent_for_substr = entities_df.select("entity_id", "entity_type", F.length("entity_id").alias("id_len"))

substr_aliases = (
    ent_for_substr.alias("short")
    .join(
        ent_for_substr.alias("long"),
        (F.col("short.entity_type") == F.col("long.entity_type"))
        & (F.col("long.id_len") > F.col("short.id_len"))
        & F.col("long.entity_id").contains(F.col("short.entity_id")),
    )
    .select(
        F.col("short.entity_id").alias("alias_id"),
        F.col("long.entity_id").alias("canonical_id"),
    )
    .distinct()
)

substr_alias_count = substr_aliases.count()
print(f"Strategy 3 (substring): {substr_alias_count:,} alias mappings")

# COMMAND ----------

# DBTITLE 1,Combine Aliases and Resolve Chains
all_aliases = (
    email_name_links
    .unionByName(title_aliases)
    .unionByName(substr_aliases)
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

enron_schema = config['enron_schema']
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
        F.coalesce(F.col("rel.relationship_type"), F.lit("RELATED_TO")).alias("relationship_type"),
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

# Deduplicate: collapse (source, target, type) triples into one edge with count
all_rels = (
    all_rels_resolved
    .groupBy("source_entity", "target_entity", "relationship_type")
    .agg(
        F.first("description").alias("description"),
        F.first("thread_id").alias("thread_id"),
        F.count("*").alias("edge_count"),
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
distinct_edges = rels.select(
    F.col("source_entity").alias("src"),
    F.col("target_entity").alias("dst"),
).distinct()

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
print(f"PageRank computed for {pr_df.count():,} entities")

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
# MAGIC ---
# MAGIC Knowledge graph is built. The Enron corpus is now ready for agent queries.
