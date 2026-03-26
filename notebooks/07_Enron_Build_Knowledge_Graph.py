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

if not spark.catalog.tableExists(raw_entities_table):
    print("Running corporate entity extraction for all threads...")
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
    print(f"Raw entities table already exists — SKIPPING extraction")

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

if not spark.catalog.tableExists(raw_rels_table):
    print("Running corporate relationship extraction...")
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
    print(f"Raw relationships table already exists — SKIPPING extraction")

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

all_rels = sent_to_rels.unionByName(semantic_rels)

(
    all_rels.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_relationships_table'])
)

rel_count = spark.table(config['enron_relationships_table']).count()
print(f"Wrote {rel_count:,} relationships (structural + semantic) to {config['enron_relationships_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions Table
# MAGIC
# MAGIC Link entities back to specific emails for source traceability.

# COMMAND ----------

# DBTITLE 1,Build Entity Mentions via Email-Level Search
entities_df = spark.table(config['enron_entities_table']).select("entity_id", "name")

(
    entities_df
    .crossJoin(emails_df.select("message_id", "body", "thread_id"))
    .filter(F.col("body").contains(F.col("name")))
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

pagerank = nx.pagerank(G, alpha=0.85, max_iter=20)

pr_df = spark.createDataFrame(
    [(k, float(v)) for k, v in pagerank.items()],
    ["entity_id", "pagerank"],
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

# DBTITLE 1,BFS Shortest Paths (NetworkX)
MAX_BFS_DEPTH = 6
UG = G.to_undirected()

entity_names_lookup = {
    row["entity_id"]: row["name"]
    for row in entities.select("entity_id", "name").collect()
}

path_rows = []
for source in UG.nodes():
    lengths = nx.single_source_shortest_path_length(UG, source, cutoff=MAX_BFS_DEPTH)
    src_name = entity_names_lookup.get(source, source)
    for target, dist in lengths.items():
        if source == target:
            continue
        tgt_name = entity_names_lookup.get(target, target)
        if dist == 1:
            path_name = f"{src_name} -> {tgt_name}"
        else:
            path_name = f"{src_name} -> ... ({dist} hops) -> {tgt_name}"
        path_rows.append((source, target, dist, path_name))

entity_paths_df = spark.createDataFrame(
    path_rows, ["source_id", "target_id", "distance", "path_names"],
)

(
    entity_paths_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_entity_paths_table'])
)

paths_count = spark.table(config['enron_entity_paths_table']).count()
print(f"Wrote {paths_count:,} shortest paths to {config['enron_entity_paths_table']}")

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
