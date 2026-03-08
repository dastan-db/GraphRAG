# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Incremental Book Ingestion
# MAGIC
# MAGIC Add new Bible books to the knowledge graph incrementally. Downloads verses,
# MAGIC extracts entities and relationships via `ai_query()`, and rebuilds graph
# MAGIC analytics — all without overwriting existing data.
# MAGIC
# MAGIC **Parameters:**
# MAGIC - `books_to_add` — JSON array of book names, e.g. `["John", "Luke", "Revelation"]`

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 networkx requests --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration and Extraction Utilities
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %run ../src/extraction/extraction

# COMMAND ----------

# DBTITLE 1,Parse Parameters
import json
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, ArrayType

dbutils.widgets.text("books_to_add", "[]", "Books to Add (JSON)")
raw_param = dbutils.widgets.get("books_to_add")
books_to_add = json.loads(raw_param)

valid_books = {name for name in config['bible_books_all']}
books_to_add = [b for b in books_to_add if b in valid_books]

if not books_to_add:
    dbutils.notebook.exit(json.dumps({"status": "error", "message": "No valid books specified"}))

already_ingested = set()
if spark.catalog.tableExists(config['verses_table']):
    already_ingested = {
        row['book'] for row in
        spark.table(config['verses_table']).select("book").distinct().collect()
    }

new_books = [b for b in books_to_add if b not in already_ingested]
if not new_books:
    dbutils.notebook.exit(json.dumps({
        "status": "skipped",
        "message": f"All requested books already ingested: {books_to_add}",
    }))

print(f"Books to ingest: {new_books}")

# COMMAND ----------

# DBTITLE 1,Update Book Registry — Mark Processing
reg_table = config['book_registry_table']
if spark.catalog.tableExists(reg_table):
    book_list_sql = ",".join(f"'{b}'" for b in new_books)
    spark.sql(f"""
        UPDATE {reg_table}
        SET status = 'processing', updated_at = current_timestamp()
        WHERE book_name IN ({book_list_sql})
    """)
    print(f"Marked {len(new_books)} books as 'processing' in registry")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Download Verses for New Books

# COMMAND ----------

# DBTITLE 1,Download and Append Verses
import requests

BASE_URL = "https://raw.githubusercontent.com/aruljohn/Bible-kjv/master"

rows = []
for book_name in new_books:
    meta = config['bible_books_all'][book_name]
    print(f"  Downloading {book_name} ({meta['chapters']} chapters)...")
    url = f"{BASE_URL}/{book_name}.json"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    for chapter_obj in data.get("chapters", []):
        chapter_num = int(chapter_obj["chapter"])
        for verse_obj in chapter_obj.get("verses", []):
            rows.append({
                "book": book_name,
                "chapter": chapter_num,
                "verse_number": int(verse_obj["verse"]),
                "text": verse_obj["text"],
                "testament": meta["testament"],
            })

print(f"Downloaded {len(rows)} verses across {len(new_books)} books")

verse_schema = StructType([
    StructField("book", StringType(), False),
    StructField("chapter", IntegerType(), False),
    StructField("verse_number", IntegerType(), False),
    StructField("text", StringType(), False),
    StructField("testament", StringType(), False),
])

new_verses_df = spark.createDataFrame(rows, schema=verse_schema)

if not spark.catalog.tableExists(config['verses_table']):
    new_verses_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(config['verses_table'])
else:
    new_verses_df.write.format("delta").mode("append").saveAsTable(config['verses_table'])

print(f"Appended {new_verses_df.count()} verses to {config['verses_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Build Chapter Texts for New Books

# COMMAND ----------

# DBTITLE 1,Build and Append Chapter Texts
new_chapters_df = (
    new_verses_df
    .groupBy("book", "chapter", "testament")
    .agg(
        F.concat_ws(" ",
            F.transform(
                F.array_sort(
                    F.collect_list(
                        F.struct(
                            F.col("verse_number"),
                            F.concat(F.lit("["), F.col("verse_number"), F.lit("] "), F.col("text")).alias("formatted")
                        )
                    )
                ),
                lambda x: x["formatted"]
            )
        ).alias("chapter_text"),
        F.count("*").alias("verse_count"),
    )
    .orderBy("testament", "book", "chapter")
)

if not spark.catalog.tableExists(config['chapters_table']):
    new_chapters_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(config['chapters_table'])
else:
    new_chapters_df.write.format("delta").mode("append").saveAsTable(config['chapters_table'])

chapter_count = new_chapters_df.count()
print(f"Appended {chapter_count} chapters to {config['chapters_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Entity Extraction via ai_query()

# COMMAND ----------

# DBTITLE 1,Extract Entities from New Chapters
new_chapters_df.createOrReplaceTempView("new_chapters")

llm_endpoint = config['llm_endpoint']
entity_prompt_prefix = ENTITY_PROMPT_PREFIX.replace("'", "''")

slugify_udf = F.udf(slugify, StringType())

print(f"Running entity extraction for {chapter_count} new chapters...")
raw_entities_df = spark.sql(f"""
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
    FROM new_chapters
""")

raw_entities_df.cache()
print(f"Entity extraction complete for {raw_entities_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,Parse and Flatten New Entities
from pyspark.sql.functions import from_json

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

new_entity_mentions_df = (
    parsed_entities_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "book", "chapter",
        F.explode("result_struct.entities").alias("entity"),
    )
    .select(
        "book", "chapter",
        F.col("entity.name").alias("name"),
        F.col("entity.entity_type").alias("entity_type"),
        F.col("entity.description").alias("description"),
    )
    .filter(F.trim(F.col("name")) != "")
    .withColumn("name", F.trim(F.col("name")))
    .withColumn("entity_id", slugify_udf(F.col("name")))
)

new_entity_mentions_df.cache()
print(f"New entity mentions: {new_entity_mentions_df.count()}")

# COMMAND ----------

# DBTITLE 1,Merge New Entities into Main Table
from pyspark.sql import Window

new_entity_mentions_df.createOrReplaceTempView("new_entity_mentions")

first_mention_window = Window.partitionBy("entity_id").orderBy("book", "chapter")
new_unique_entities_df = (
    new_entity_mentions_df
    .withColumn("rn", F.row_number().over(first_mention_window))
    .filter(F.col("rn") == 1)
    .select(
        "entity_id", "name", "entity_type", "description",
        F.col("book").alias("first_mention_book"),
        F.col("chapter").alias("first_mention_chapter"),
    )
)

entities_table = config['entities_table']
if not spark.catalog.tableExists(entities_table):
    new_unique_entities_df.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(entities_table)
    print(f"Created entities table with {new_unique_entities_df.count()} entities")
else:
    new_unique_entities_df.createOrReplaceTempView("new_entities_to_merge")
    spark.sql(f"""
        MERGE INTO {entities_table} AS target
        USING new_entities_to_merge AS source
        ON target.entity_id = source.entity_id
        WHEN NOT MATCHED THEN INSERT *
    """)
    total = spark.table(entities_table).count()
    print(f"Merged new entities → total: {total}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Relationship Extraction

# COMMAND ----------

# DBTITLE 1,Build Chapter Entity Lists for New Books
chapter_entity_names_df = (
    new_entity_mentions_df
    .groupBy("book", "chapter")
    .agg(
        F.concat_ws("\n- ", F.collect_set("name")).alias("entity_names"),
        F.count("*").alias("entity_count"),
    )
    .filter(F.col("entity_count") >= 2)
)

chapter_entity_names_df.createOrReplaceTempView("new_chapter_entities")
chapters_with_entities = chapter_entity_names_df.count()
print(f"Chapters with 2+ entities for relationship extraction: {chapters_with_entities}")

# COMMAND ----------

# DBTITLE 1,Extract Relationships from New Chapters
rel_prompt_prefix = RELATIONSHIP_PROMPT_PREFIX.replace("'", "''")

print(f"Running relationship extraction for {chapters_with_entities} chapters...")
raw_rels_df = spark.sql(f"""
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
    FROM new_chapters c
    JOIN new_chapter_entities e ON c.book = e.book AND c.chapter = e.chapter
""")

raw_rels_df.cache()
print(f"Relationship extraction complete for {raw_rels_df.count()} chapters")

# COMMAND ----------

# DBTITLE 1,Parse and Append Relationships
relationships_schema = ArrayType(
    StructType([
        StructField("source", StringType()),
        StructField("target", StringType()),
        StructField("relationship_type", StringType()),
        StructField("description", StringType())
    ])
)
rel_result_schema = StructType([StructField("relationships", relationships_schema)])

parsed_rels_df = raw_rels_df.withColumn(
    "result_struct",
    from_json(F.col("extracted.result"), rel_result_schema)
)

new_rels_df = (
    parsed_rels_df
    .filter(F.col("extracted.errorMessage").isNull())
    .select(
        "book", "chapter",
        F.explode("result_struct.relationships").alias("rel"),
    )
    .select(
        slugify_udf(F.trim(F.col("rel.source"))).alias("source_entity"),
        slugify_udf(F.trim(F.col("rel.target"))).alias("target_entity"),
        F.coalesce(F.col("rel.relationship_type"), F.lit("RELATED_TO")).alias("relationship_type"),
        F.col("rel.description").alias("description"),
        "book", "chapter",
    )
    .filter(
        (F.col("source_entity").isNotNull()) &
        (F.col("source_entity") != "") &
        (F.col("target_entity").isNotNull()) &
        (F.col("target_entity") != "")
    )
)

rels_table = config['relationships_table']
if not spark.catalog.tableExists(rels_table):
    new_rels_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(rels_table)
else:
    new_rels_df.write.format("delta").mode("append").saveAsTable(rels_table)

new_rel_count = new_rels_df.count()
total_rels = spark.table(rels_table).count()
print(f"Appended {new_rel_count} relationships → total: {total_rels}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Build Entity Mentions for New Books

# COMMAND ----------

# DBTITLE 1,Append Entity Mentions via Verse-Level Search
all_entities_df = spark.table(config['entities_table']).select("entity_id", "name")

new_mentions_df = (
    all_entities_df
    .crossJoin(new_verses_df)
    .filter(F.col("text").contains(F.col("name")))
    .select("entity_id", new_verses_df["book"], new_verses_df["chapter"], "verse_number")
)

mentions_table = config['entity_mentions_table']
if not spark.catalog.tableExists(mentions_table):
    new_mentions_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(mentions_table)
else:
    new_mentions_df.write.format("delta").mode("append").saveAsTable(mentions_table)

new_mention_count = new_mentions_df.count()
print(f"Appended {new_mention_count} entity mentions")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Rebuild Graph Analytics (Full Graph)
# MAGIC
# MAGIC PageRank and BFS paths are global metrics — they must be recomputed over the
# MAGIC entire graph whenever new books are added.

# COMMAND ----------

# DBTITLE 1,Recompute Degree Centrality + PageRank + Cross-Testament
import networkx as nx
from pyspark.sql.types import DoubleType

rels = spark.table(config['relationships_table'])
entities = spark.table(config['entities_table'])

in_deg = rels.groupBy(F.col("target_entity").alias("entity_id")).agg(F.count("*").alias("in_degree"))
out_deg = rels.groupBy(F.col("source_entity").alias("entity_id")).agg(F.count("*").alias("out_degree"))
degrees_df = (
    entities.select("entity_id")
    .join(in_deg, "entity_id", "left")
    .join(out_deg, "entity_id", "left")
    .fillna(0, subset=["in_degree", "out_degree"])
    .withColumn("total_degree", F.col("in_degree") + F.col("out_degree"))
)

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
    ["entity_id", "pagerank"]
)
print(f"PageRank computed for {pr_df.count()} entities")

book_testament_map = {b: info['testament'] for b, info in config['bible_books_all'].items()}
testament_mapping = F.create_map([F.lit(x) for pair in book_testament_map.items() for x in pair])

entities_with_testament = (
    entities.select("entity_id", "name", "entity_type", "first_mention_book")
    .withColumn("testament", testament_mapping[F.col("first_mention_book")])
)

edges_with_testament = (
    rels.select("source_entity", "target_entity")
    .join(entities_with_testament.select(
        F.col("entity_id").alias("source_entity"),
        F.col("testament").alias("src_testament"),
    ), "source_entity")
    .join(entities_with_testament.select(
        F.col("entity_id").alias("target_entity"),
        F.col("testament").alias("tgt_testament"),
    ), "target_entity")
)

cross_testament_df = (
    edges_with_testament
    .filter(F.col("src_testament") != F.col("tgt_testament"))
    .groupBy(F.col("source_entity").alias("entity_id"))
    .agg(F.countDistinct("target_entity").alias("cross_testament_connections"))
)

entity_analytics_df = (
    entities_with_testament
    .join(pr_df, "entity_id", "left")
    .join(degrees_df, "entity_id", "left")
    .join(cross_testament_df, "entity_id", "left")
    .fillna(0, subset=["pagerank", "in_degree", "out_degree", "total_degree", "cross_testament_connections"])
    .select(
        "entity_id", "name", "entity_type", "testament",
        F.col("pagerank").cast(DoubleType()),
        "in_degree", "out_degree", "total_degree",
        "cross_testament_connections",
    )
)

entity_analytics_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
    .saveAsTable(config['entity_analytics_table'])
print(f"Rebuilt entity_analytics: {entity_analytics_df.count()} entities")

# COMMAND ----------

# DBTITLE 1,Recompute BFS Shortest Paths
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
    path_rows, ["source_id", "target_id", "distance", "path_names"]
)

entity_paths_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
    .saveAsTable(config['entity_paths_table'])
print(f"Rebuilt entity_paths: {entity_paths_df.count()} paths")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: Update Book Registry

# COMMAND ----------

# DBTITLE 1,Record Final Stats and Mark Active
reg_table = config['book_registry_table']
if spark.catalog.tableExists(reg_table):
    for book_name in new_books:
        v_count = spark.table(config['verses_table']).filter(F.col("book") == book_name).count()
        e_count = (
            spark.table(config['entity_mentions_table'])
            .filter(F.col("book") == book_name)
            .select("entity_id").distinct().count()
        )
        r_count = spark.table(config['relationships_table']).filter(F.col("book") == book_name).count()

        spark.sql(f"""
            UPDATE {reg_table}
            SET status = 'active',
                verse_count = {v_count},
                entity_count = {e_count},
                relationship_count = {r_count},
                added_at = current_timestamp(),
                updated_at = current_timestamp()
            WHERE book_name = '{book_name}'
        """)
        print(f"  {book_name}: {v_count} verses, {e_count} entities, {r_count} relationships")

# COMMAND ----------

# DBTITLE 1,Summary
result = {
    "status": "success",
    "books_added": new_books,
    "total_entities": spark.table(config['entities_table']).count(),
    "total_relationships": spark.table(config['relationships_table']).count(),
    "total_verses": spark.table(config['verses_table']).count(),
}
print(json.dumps(result, indent=2))
dbutils.notebook.exit(json.dumps(result))
