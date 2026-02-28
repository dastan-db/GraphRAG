# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Build Knowledge Graph (Interactive Debug)
# MAGIC
# MAGIC Self-contained notebook for interactive debugging. Run cell by cell.
# MAGIC Once everything works, update `notebooks/02_Build_Knowledge_Graph.py` with the fixes.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration (inlined)
import re

config = {}
config['catalog'] = 'serverless_8e8gyh_catalog'
config['schema'] = 'graphrag_bible'
config['llm_endpoint'] = 'databricks-meta-llama-3-3-70b-instruct'
config['small_llm_endpoint'] = 'databricks-meta-llama-3-1-8b-instruct'
config['embedding_endpoint'] = 'databricks-gte-large-en'

config['verses_table'] = f"{config['catalog']}.{config['schema']}.verses"
config['chapters_table'] = f"{config['catalog']}.{config['schema']}.chapters"
config['entities_table'] = f"{config['catalog']}.{config['schema']}.entities"
config['relationships_table'] = f"{config['catalog']}.{config['schema']}.relationships"
config['entity_mentions_table'] = f"{config['catalog']}.{config['schema']}.entity_mentions"

_ = spark.sql(f"USE CATALOG {config['catalog']}")
_ = spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config['catalog']}.{config['schema']}")
_ = spark.sql(f"USE SCHEMA {config['schema']}")

print(config)

# COMMAND ----------

# DBTITLE 1,Extraction Prompts and Helpers (inlined)
ENTITY_PROMPT_PREFIX = """You are an expert biblical scholar. Extract all significant entities from the following chapter text.

For each entity, provide:
- name: The canonical name (e.g., Abraham not Abram unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept (treat God/Lord as Person)
- description: A brief description of this entity in context

Rules:
- Be comprehensive but only include entities actually mentioned in the text
- Use canonical names consistently
- Include divine figures (God, Lord, Holy Spirit) as Person type

"""

RELATIONSHIP_PROMPT_PREFIX = """You are an expert biblical scholar. Given the following chapter text and a list of entities found in it, extract the relationships between these entities.

For each relationship, provide:
- source: Name of the source entity (must match an entity from the list)
- target: Name of the target entity (must match an entity from the list)
- relationship_type: One of: FAMILY_OF, PARENT_OF, CHILD_OF, SPOUSE_OF, ANCESTOR_OF, SPOKE_TO, COMMANDED, TRAVELED_TO, LOCATED_IN, PARTICIPATED_IN, LEADS, CREATED, PROMISED, PROPHESIED, BLESSED, SERVED, OPPOSED, WITNESSED
- description: A brief description of this specific relationship in context

Rules:
- Only use entity names from the provided list
- Each relationship should be grounded in what actually happens in this chapter
- Prefer specific relationship types over generic ones
- A single pair of entities can have multiple relationships

"""

def slugify(name):
    """Convert an entity name to a stable ID."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType

slugify_udf = F.udf(slugify, StringType())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Prepare Chapter Texts
# MAGIC
# MAGIC Skip this if chapters table already exists from a previous run.

# COMMAND ----------

# DBTITLE 1,Check if chapters table already exists
if spark.catalog.tableExists(config['chapters_table']):
    chapter_count = spark.table(config['chapters_table']).count()
    print(f"Chapters table already exists with {chapter_count} rows — SKIPPING build")
else:
    print("Chapters table does not exist — will build in the next cell")

# COMMAND ----------

# DBTITLE 1,Build and Persist Chapter Texts (skip if exists)
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

# COMMAND ----------

# DBTITLE 1,Extract Entities (skip if temp table exists)
llm_endpoint = config['llm_endpoint']
chapters_table = config['chapters_table']
entity_prompt_prefix = ENTITY_PROMPT_PREFIX.replace("'", "''")
raw_entities_table = f"{config['catalog']}.{config['schema']}.raw_entities_temp"

if spark.catalog.tableExists(raw_entities_table):
    print(f"Raw entities table already exists — SKIPPING extraction")
else:
    print(f"Running entity extraction for all chapters...")
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

raw_entities_df = spark.table(raw_entities_table)
print(f"Entity extraction complete for {raw_entities_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,DEBUG: Inspect extracted column schema and sample data
raw_entities_df = spark.table(raw_entities_table)
print("=== Schema of raw_entities_df ===")
raw_entities_df.printSchema()

print("\n=== Schema of 'extracted' column ===")
from pyspark.sql.types import StructType
extracted_type = raw_entities_df.schema["extracted"].dataType
print(extracted_type)

print("\n=== Sample extracted values (first 2 rows) ===")
display(raw_entities_df.select("book", "chapter", "extracted").limit(2))

# COMMAND ----------

# DBTITLE 1,DEBUG: Test field access paths on one row
sample = raw_entities_df.limit(1)

print("=== Try extracted.errorMessage ===")
try:
    display(sample.select("extracted.errorMessage"))
except Exception as e:
    print(f"  FAILED: {e}")

print("\n=== Try extracted.result ===")
try:
    display(sample.select("extracted.result"))
except Exception as e:
    print(f"  FAILED: {e}")

print("\n=== Try extracted.result.entities ===")
try:
    display(sample.select("extracted.result.entities"))
except Exception as e:
    print(f"  FAILED: {e}")

print("\n=== Try extracted.result.result.entities ===")
try:
    display(sample.select("extracted.result.result.entities"))
except Exception as e:
    print(f"  FAILED: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2b: Parse and Flatten Entities
# MAGIC
# MAGIC Based on the debug output above, use the correct field access path.
# MAGIC The cell below tries `extracted.result.entities` first (the expected path).

# COMMAND ----------

# DBTITLE 1,Parse and Flatten Extracted Entities
entity_mentions_temp_table = f"{config['catalog']}.{config['schema']}.entity_mentions_all_temp"

from pyspark.sql.functions import from_json, col, explode
from pyspark.sql.types import StructType, ArrayType, StructField, StringType

entities_schema = ArrayType(
    StructType([
        StructField("name", StringType()),
        StructField("entity_type", StringType()),
        StructField("description", StringType())
    ])
)
result_schema = StructType([
    StructField("entities", entities_schema)
])
extracted_schema = StructType([
    StructField("result", StringType()),  # result is a JSON string
    StructField("errorMessage", StringType())
])

parsed_df = raw_entities_df.withColumn(
    "result_struct",
    from_json(col("extracted.result"), result_schema)
)

(
    parsed_df
    .filter(col("extracted.errorMessage").isNull())
    .select(
        "book",
        "chapter",
        explode(col("result_struct.entities")).alias("entity"),
    )
    .select(
        "book",
        "chapter",
        col("entity.name").alias("name"),
        col("entity.entity_type").alias("entity_type"),
        col("entity.description").alias("description"),
    )
    .filter(F.trim(col("name")) != "")
    .withColumn("name", F.trim(col("name")))
    .withColumn("entity_id", slugify_udf(col("name")))
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(entity_mentions_temp_table)
)

entities_exploded_df = spark.table(entity_mentions_temp_table)
total_mentions = entities_exploded_df.count()
display(entities_exploded_df.limit(20))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deduplicate Entities

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
display(spark.table(config['entities_table']).limit(20))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Relationship Extraction

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

# DBTITLE 1,Extract Relationships (skip if temp table exists)
llm_endpoint = config['llm_endpoint']
rel_prompt_prefix = RELATIONSHIP_PROMPT_PREFIX.replace("'", "''")
raw_rels_table = f"{config['catalog']}.{config['schema']}.raw_relationships_temp"

if spark.catalog.tableExists(raw_rels_table):
    print(f"Raw relationships table already exists — SKIPPING extraction")
else:
    print(f"Running relationship extraction...")
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

raw_rels_df = spark.table(raw_rels_table)
print(f"Relationship extraction complete for {raw_rels_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,Parse, Flatten, and Write Relationships Table
from pyspark.sql.functions import from_json, col, explode, trim, coalesce, lit

# Define schema for relationships
from pyspark.sql.types import StructType, ArrayType, StructField, StringType

relationships_schema = ArrayType(
    StructType([
        StructField("source", StringType()),
        StructField("target", StringType()),
        StructField("relationship_type", StringType()),
        StructField("description", StringType())
    ])
)
result_schema = StructType([
    StructField("relationships", relationships_schema)
])

parsed_df = raw_rels_df.withColumn(
    "result_struct",
    from_json(col("extracted.result"), result_schema)
)

rels_exploded_df = (
    parsed_df
    .filter(col("extracted.errorMessage").isNull())
    .select(
        "book",
        "chapter",
        explode(col("result_struct.relationships")).alias("rel"),
    )
    .select(
        slugify_udf(trim(col("rel.source"))).alias("source_entity"),
        slugify_udf(trim(col("rel.target"))).alias("target_entity"),
        coalesce(col("rel.relationship_type"), lit("RELATED_TO")).alias("relationship_type"),
        col("rel.description").alias("description"),
        "book",
        "chapter",
    )
    .filter(
        (col("source_entity").isNotNull()) &
        (col("source_entity") != "") &
        (col("target_entity").isNotNull()) &
        (col("target_entity") != "")
    )
)

rels_exploded_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(config['relationships_table'])

rel_count = spark.table(config['relationships_table']).count()
print(f"Wrote {rel_count} relationships to {config['relationships_table']}")
display(spark.table(config['relationships_table']).limit(20))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions Table

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
# MAGIC **Done!** Once everything above works, update `notebooks/02_Build_Knowledge_Graph.py` with the fixes.