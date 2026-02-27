# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Build Knowledge Graph
# MAGIC
# MAGIC Extract entities and relationships from each chapter using an LLM, then store the results in Delta tables. This is the core of GraphRAG: turning unstructured text into a structured, queryable knowledge graph.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration and Utilities
# MAGIC %run ./util/config

# COMMAND ----------

# MAGIC %run ./util/extraction

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Prepare Chapter Texts
# MAGIC
# MAGIC We aggregate verses into chapter-level text blocks for LLM processing. Processing at the chapter level balances context richness with cost efficiency (~150 LLM calls total).

# COMMAND ----------

# DBTITLE 1,Build Chapter Texts
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

chapter_rows = chapters_df.collect()
print(f"Total chapters to process: {len(chapter_rows)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Entity Extraction
# MAGIC
# MAGIC For each chapter, we ask the LLM to identify all significant entities: people, places, events, groups, and concepts.

# COMMAND ----------

# DBTITLE 1,Extract Entities from All Chapters
client = get_llm_client()
endpoint = config['llm_endpoint']

all_entities = []
all_chapter_entities = {}

for i, row in enumerate(chapter_rows):
    book, chapter, chapter_text = row["book"], row["chapter"], row["chapter_text"]
    print(f"[{i+1}/{len(chapter_rows)}] Extracting entities from {book} {chapter}...")

    prompt = ENTITY_EXTRACTION_PROMPT.format(
        book=book, chapter=chapter, text=chapter_text[:6000]
    )
    response_text = call_llm(client, endpoint, prompt)
    entities = parse_json_response(response_text)

    chapter_key = f"{book}_{chapter}"
    all_chapter_entities[chapter_key] = []

    for ent in entities:
        name = ent.get("name", "").strip()
        if not name:
            continue
        entity_id = slugify(name)
        all_entities.append({
            "entity_id": entity_id,
            "name": name,
            "entity_type": ent.get("entity_type", "Concept"),
            "description": ent.get("description", ""),
            "book": book,
            "chapter": chapter,
        })
        all_chapter_entities[chapter_key].append(name)

    print(f"  Found {len(entities)} entities")

print(f"\nTotal raw entity mentions: {len(all_entities)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deduplicate Entities
# MAGIC
# MAGIC The same entity (e.g., "Moses") appears in many chapters. We keep the first occurrence's description and track where each entity was first mentioned.

# COMMAND ----------

# DBTITLE 1,Deduplicate and Write Entities Table
seen = {}
unique_entities = []
for ent in all_entities:
    eid = ent["entity_id"]
    if eid not in seen:
        seen[eid] = True
        unique_entities.append({
            "entity_id": eid,
            "name": ent["name"],
            "entity_type": ent["entity_type"],
            "description": ent["description"],
            "first_mention_book": ent["book"],
            "first_mention_chapter": ent["chapter"],
        })

print(f"Unique entities: {len(unique_entities)}")

entity_schema = StructType([
    StructField("entity_id", StringType(), False),
    StructField("name", StringType(), False),
    StructField("entity_type", StringType(), False),
    StructField("description", StringType(), True),
    StructField("first_mention_book", StringType(), True),
    StructField("first_mention_chapter", IntegerType(), True),
])

entities_df = spark.createDataFrame(unique_entities, schema=entity_schema)
(
    entities_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['entities_table'])
)
print(f"Wrote {len(unique_entities)} entities to {config['entities_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Relationship Extraction
# MAGIC
# MAGIC For each chapter, we provide the extracted entities and ask the LLM to identify relationships between them.

# COMMAND ----------

# DBTITLE 1,Extract Relationships from All Chapters
all_relationships = []

for i, row in enumerate(chapter_rows):
    book, chapter, chapter_text = row["book"], row["chapter"], row["chapter_text"]
    chapter_key = f"{book}_{chapter}"
    entity_names = all_chapter_entities.get(chapter_key, [])

    if len(entity_names) < 2:
        continue

    print(f"[{i+1}/{len(chapter_rows)}] Extracting relationships from {book} {chapter} ({len(entity_names)} entities)...")

    entities_list = "\n".join(f"- {name}" for name in entity_names)
    prompt = RELATIONSHIP_EXTRACTION_PROMPT.format(
        book=book, chapter=chapter,
        entities_list=entities_list,
        text=chapter_text[:6000],
    )
    response_text = call_llm(client, endpoint, prompt)
    rels = parse_json_response(response_text)

    for rel in rels:
        source = rel.get("source", "").strip()
        target = rel.get("target", "").strip()
        if not source or not target:
            continue
        all_relationships.append({
            "source_entity": slugify(source),
            "target_entity": slugify(target),
            "relationship_type": rel.get("relationship_type", "RELATED_TO"),
            "description": rel.get("description", ""),
            "book": book,
            "chapter": chapter,
        })

    print(f"  Found {len(rels)} relationships")

print(f"\nTotal relationships extracted: {len(all_relationships)}")

# COMMAND ----------

# DBTITLE 1,Write Relationships Table
rel_schema = StructType([
    StructField("source_entity", StringType(), False),
    StructField("target_entity", StringType(), False),
    StructField("relationship_type", StringType(), False),
    StructField("description", StringType(), True),
    StructField("book", StringType(), True),
    StructField("chapter", IntegerType(), True),
])

rels_df = spark.createDataFrame(all_relationships, schema=rel_schema)
(
    rels_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['relationships_table'])
)
print(f"Wrote {len(all_relationships)} relationships to {config['relationships_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions Table
# MAGIC
# MAGIC Link entities back to specific verses for source traceability.

# COMMAND ----------

# DBTITLE 1,Build Entity Mentions via Verse-Level Search
entity_names_bc = spark.sparkContext.broadcast(
    {ent["entity_id"]: ent["name"] for ent in unique_entities}
)

verses_df = spark.table(config['verses_table'])

mention_rows = []
for ent in unique_entities:
    name = ent["name"]
    eid = ent["entity_id"]
    matching = verses_df.filter(F.col("text").contains(name)).select("book", "chapter", "verse_number").collect()
    for m in matching:
        mention_rows.append({
            "entity_id": eid,
            "book": m["book"],
            "chapter": m["chapter"],
            "verse_number": m["verse_number"],
        })

mention_schema = StructType([
    StructField("entity_id", StringType(), False),
    StructField("book", StringType(), False),
    StructField("chapter", IntegerType(), False),
    StructField("verse_number", IntegerType(), False),
])

if mention_rows:
    mentions_df = spark.createDataFrame(mention_rows, schema=mention_schema)
    (
        mentions_df.write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(config['entity_mentions_table'])
    )
    print(f"Wrote {len(mention_rows)} entity mentions to {config['entity_mentions_table']}")
else:
    print("No entity mentions found (will be populated on first run)")

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
