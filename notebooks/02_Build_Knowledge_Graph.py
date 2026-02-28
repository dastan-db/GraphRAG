# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Build Knowledge Graph
# MAGIC
# MAGIC Extract entities and relationships from each chapter using parallelized Spark SQL,
# MAGIC then store the results in Delta tables. Both entity and relationship extraction use
# MAGIC `ai_query()` with `responseFormat` for structured output — no external agents or
# MAGIC manual setup required.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration and Utilities
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %run ../src/extraction/extraction

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType

slugify_udf = F.udf(slugify, StringType())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Prepare Chapter Texts
# MAGIC
# MAGIC Aggregate verses into chapter-level text blocks and persist as a Delta table.
# MAGIC This table serves as the input dataset for both entity and relationship extraction.

# COMMAND ----------

# DBTITLE 1,Build and Persist Chapter Texts
if not spark.catalog.tableExists(config['chapters_table']):
    chapters_df = (
        spark.table(config['verses_table'])
        .groupBy("book", "chapter", "testament")
        .agg(
            F.concat_ws(" ", F.collect_list(
                F.concat(F.lit("["), F.col("verse_number"), F.lit("] "), F.col("text"))
            )).alias("chapter_text"),
            F.count("*").alias("verse_count"),
        )
        .orderBy("testament", "book", "chapter")
    )

    (
        chapters_df.repartition(50).write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(config['chapters_table'])
    )

chapter_count = spark.table(config['chapters_table']).count()
print(f"Chapters: {chapter_count}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Entity Extraction via ai_query()
# MAGIC
# MAGIC Uses `ai_query()` with `responseFormat` for structured output to extract entities
# MAGIC from all chapters in parallel. Spark distributes the LLM calls automatically.

# COMMAND ----------

# DBTITLE 1,Extract Entities from All Chapters (Parallel)
llm_endpoint = config['llm_endpoint']
chapters_table = config['chapters_table']
entity_prompt_prefix = ENTITY_PROMPT_PREFIX.replace("'", "''")

raw_entities_table = f"{config['catalog']}.{config['schema']}.raw_entities_temp"

if not spark.catalog.tableExists(raw_entities_table):
    print("Running entity extraction for all chapters...")
    spark.sql(f"""
        SELECT
            book,
            chapter,
            ai_query(
                '{llm_endpoint}',
                CONCAT(
                    '{entity_prompt_prefix}',
                    'Book: ', book, ', Chapter: ', CAST(chapter AS STRING),
                    '\\n\\nText:\\n', SUBSTRING(chapter_text, 1, 6000)
                ),
                responseFormat => 'STRUCT<result:STRUCT<entities:ARRAY<STRUCT<name:STRING,entity_type:STRING,description:STRING>>>>',
                modelParameters => named_struct('temperature', 0.1, 'max_tokens', 4096),
                failOnError => false
            ) AS extracted
        FROM {chapters_table}
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(raw_entities_table)
else:
    print(f"Raw entities table already exists — SKIPPING extraction")

raw_entities_df = spark.table(raw_entities_table)
print(f"Entity extraction complete for {raw_entities_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,Parse and Flatten Extracted Entities
from pyspark.sql.functions import from_json
from pyspark.sql.types import ArrayType

entity_mentions_temp_table = f"{config['catalog']}.{config['schema']}.entity_mentions_all_temp"

entities_schema = ArrayType(
    StructType([
        StructField("name", StringType()),
        StructField("entity_type", StringType()),
        StructField("description", StringType())
    ])
)
entity_result_schema = StructType([
    StructField("entities", entities_schema)
])

parsed_entities_df = raw_entities_df.withColumn(
    "result_struct",
    from_json(F.col("extracted.result"), entity_result_schema)
)

(
    parsed_entities_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "book",
        "chapter",
        F.explode("result_struct.entities").alias("entity"),
    )
    .select(
        "book",
        "chapter",
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
print(f"Total raw entity mentions: {total_mentions}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deduplicate Entities
# MAGIC
# MAGIC The same entity (e.g., "Moses") appears in many chapters. We keep the first
# MAGIC occurrence's description and track where each entity was first mentioned.

# COMMAND ----------

# DBTITLE 1,Deduplicate and Write Entities Table
from pyspark.sql import Window

first_mention_window = (
    Window.partitionBy("entity_id")
    .orderBy("book", "chapter")
)

unique_entities_df = (
    entities_exploded_df
    .withColumn("rn", F.row_number().over(first_mention_window))
    .filter(F.col("rn") == 1)
    .select(
        "entity_id",
        "name",
        "entity_type",
        "description",
        F.col("book").alias("first_mention_book"),
        F.col("chapter").alias("first_mention_chapter"),
    )
)

(
    unique_entities_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['entities_table'])
)

entity_count = spark.table(config['entities_table']).count()
print(f"Wrote {entity_count} unique entities to {config['entities_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Relationship Extraction
# MAGIC
# MAGIC For each chapter, we join the chapter text with its extracted entity names and
# MAGIC call `ai_query()` with `responseFormat` for structured output. Spark SQL
# MAGIC parallelizes all ~150 chapters automatically.

# COMMAND ----------

# DBTITLE 1,Build Chapter Entity Lists
chapter_entity_names_df = (
    entities_exploded_df
    .groupBy("book", "chapter")
    .agg(
        F.concat_ws("\n- ", F.collect_set("name")).alias("entity_names"),
        F.count("*").alias("entity_count"),
    )
    .filter(F.col("entity_count") >= 2)
)

chapter_entity_names_df.repartition(50).createOrReplaceTempView("chapter_entities")
spark.table(config['chapters_table']).createOrReplaceTempView("chapters")

chapters_with_entities = chapter_entity_names_df.count()
print(f"Chapters with 2+ entities for relationship extraction: {chapters_with_entities}")

# COMMAND ----------

# DBTITLE 1,Extract Relationships from All Chapters (Parallel)
llm_endpoint = config['llm_endpoint']

rel_prompt_prefix = RELATIONSHIP_PROMPT_PREFIX.replace("'", "''")

raw_rels_table = f"{config['catalog']}.{config['schema']}.raw_relationships_temp"

if not spark.catalog.tableExists(raw_rels_table):
    print("Running relationship extraction...")
    spark.sql(f"""
        SELECT
            c.book,
            c.chapter,
            ai_query(
                '{llm_endpoint}',
                CONCAT(
                    '{rel_prompt_prefix}',
                    'Book: ', c.book, ', Chapter: ', CAST(c.chapter AS STRING),
                    '\\n\\nEntities found in this chapter:\\n- ', e.entity_names,
                    '\\n\\nText:\\n', SUBSTRING(c.chapter_text, 1, 6000)
                ),
                responseFormat => 'STRUCT<result:STRUCT<relationships:ARRAY<STRUCT<source:STRING,target:STRING,relationship_type:STRING,description:STRING>>>>',
                modelParameters => named_struct('temperature', 0.1, 'max_tokens', 4096),
                failOnError => false
            ) AS extracted
        FROM chapters c
        JOIN chapter_entities e ON c.book = e.book AND c.chapter = e.chapter
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(raw_rels_table)
else:
    print(f"Raw relationships table already exists — SKIPPING extraction")

raw_rels_df = spark.table(raw_rels_table)
print(f"Relationship extraction complete for {raw_rels_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,Parse, Flatten, and Write Relationships Table
relationships_schema = ArrayType(
    StructType([
        StructField("source", StringType()),
        StructField("target", StringType()),
        StructField("relationship_type", StringType()),
        StructField("description", StringType())
    ])
)
rel_result_schema = StructType([
    StructField("relationships", relationships_schema)
])

parsed_rels_df = raw_rels_df.withColumn(
    "result_struct",
    from_json(F.col("extracted.result"), rel_result_schema)
)

rels_exploded_df = (
    parsed_rels_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "book",
        "chapter",
        F.explode("result_struct.relationships").alias("rel"),
    )
    .select(
        slugify_udf(F.trim(F.col("rel.source"))).alias("source_entity"),
        slugify_udf(F.trim(F.col("rel.target"))).alias("target_entity"),
        F.coalesce(F.col("rel.relationship_type"), F.lit("RELATED_TO")).alias("relationship_type"),
        F.col("rel.description").alias("description"),
        "book",
        "chapter",
    )
    .filter(
        (F.col("source_entity").isNotNull()) &
        (F.col("source_entity") != "") &
        (F.col("target_entity").isNotNull()) &
        (F.col("target_entity") != "")
    )
)

(
    rels_exploded_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['relationships_table'])
)

rel_count = spark.table(config['relationships_table']).count()
print(f"Wrote {rel_count} relationships to {config['relationships_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions Table
# MAGIC
# MAGIC Link entities back to specific verses for source traceability.

# COMMAND ----------

# DBTITLE 1,Build Entity Mentions via Verse-Level Search
entities_df = spark.table(config['entities_table']).select("entity_id", "name")
verses_df = spark.table(config['verses_table'])

(
    entities_df
    .crossJoin(verses_df)
    .filter(F.col("text").contains(F.col("name")))
    .select(
        F.col("entity_id"),
        verses_df["book"],
        verses_df["chapter"],
        F.col("verse_number"),
    )
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(config['entity_mentions_table'])
)

mention_count = spark.table(config['entity_mentions_table']).count()
print(f"Wrote {mention_count} entity mentions to {config['entity_mentions_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Knowledge Graph Statistics

# COMMAND ----------

# DBTITLE 1,Entity Counts by Type
display(
    spark.table(config['entities_table'])
    .groupBy("entity_type")
    .count()
    .orderBy(F.desc("count"))
)

# COMMAND ----------

# DBTITLE 1,Top Entities by Mention Count
if spark.catalog.tableExists(config['entity_mentions_table']):
    display(
        spark.table(config['entity_mentions_table'])
        .groupBy("entity_id")
        .agg(F.count("*").alias("mention_count"))
        .join(spark.table(config['entities_table']), "entity_id")
        .select("name", "entity_type", "mention_count")
        .orderBy(F.desc("mention_count"))
        .limit(20)
    )

# COMMAND ----------

# DBTITLE 1,Relationship Type Distribution
display(
    spark.table(config['relationships_table'])
    .groupBy("relationship_type")
    .count()
    .orderBy(F.desc("count"))
)

# COMMAND ----------

# DBTITLE 1,Cross-Book Entity Overlap
display(
    spark.table(config['relationships_table'])
    .select("source_entity", "book")
    .union(spark.table(config['relationships_table']).select("target_entity", "book"))
    .withColumnRenamed("source_entity", "entity_id")
    .groupBy("entity_id")
    .agg(F.countDistinct("book").alias("books_mentioned_in"))
    .filter("books_mentioned_in > 1")
    .join(spark.table(config['entities_table']), "entity_id")
    .select("name", "entity_type", "books_mentioned_in")
    .orderBy(F.desc("books_mentioned_in"))
    .limit(20)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Knowledge graph is built. Proceed to **03_Build_Agent** to create the query agent.
