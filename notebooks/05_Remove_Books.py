# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Remove Books from Knowledge Graph
# MAGIC
# MAGIC Remove specified books from the knowledge graph. Deletes verses, chapters,
# MAGIC relationships, and entity mentions for the given books, removes orphaned
# MAGIC entities, and rebuilds graph analytics.
# MAGIC
# MAGIC **Parameters:**
# MAGIC - `books_to_remove` — JSON array of book names, e.g. `["John", "Luke"]`

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 networkx --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Parse Parameters
import json
import pyspark.sql.functions as F

dbutils.widgets.text("books_to_remove", "[]", "Books to Remove (JSON)")
raw_param = dbutils.widgets.get("books_to_remove")
books_to_remove = json.loads(raw_param)

if not books_to_remove:
    dbutils.notebook.exit(json.dumps({"status": "error", "message": "No books specified"}))

ingested_books = set()
if spark.catalog.tableExists(config['verses_table']):
    ingested_books = {
        row['book'] for row in
        spark.table(config['verses_table']).select("book").distinct().collect()
    }

books_to_remove = [b for b in books_to_remove if b in ingested_books]
if not books_to_remove:
    dbutils.notebook.exit(json.dumps({
        "status": "skipped",
        "message": "None of the specified books are currently ingested",
    }))

print(f"Books to remove: {books_to_remove}")
book_list_sql = ",".join(f"'{b}'" for b in books_to_remove)

# COMMAND ----------

# DBTITLE 1,Update Book Registry — Mark Processing
reg_table = config['book_registry_table']
if spark.catalog.tableExists(reg_table):
    spark.sql(f"""
        UPDATE {reg_table}
        SET status = 'processing', updated_at = current_timestamp()
        WHERE book_name IN ({book_list_sql})
    """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Delete Book Data from All Tables

# COMMAND ----------

# DBTITLE 1,Delete Verses and Chapters
spark.sql(f"DELETE FROM {config['verses_table']} WHERE book IN ({book_list_sql})")
print(f"Deleted verses for: {books_to_remove}")

if spark.catalog.tableExists(config['chapters_table']):
    spark.sql(f"DELETE FROM {config['chapters_table']} WHERE book IN ({book_list_sql})")
    print(f"Deleted chapters for: {books_to_remove}")

# COMMAND ----------

# DBTITLE 1,Delete Relationships and Entity Mentions
if spark.catalog.tableExists(config['relationships_table']):
    spark.sql(f"DELETE FROM {config['relationships_table']} WHERE book IN ({book_list_sql})")
    print(f"Deleted relationships for: {books_to_remove}")

if spark.catalog.tableExists(config['entity_mentions_table']):
    spark.sql(f"DELETE FROM {config['entity_mentions_table']} WHERE book IN ({book_list_sql})")
    print(f"Deleted entity mentions for: {books_to_remove}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Remove Orphaned Entities
# MAGIC
# MAGIC Entities that no longer have mentions in any remaining book or relationships
# MAGIC referencing them should be removed.

# COMMAND ----------

# DBTITLE 1,Delete Orphaned Entities
entities_table = config['entities_table']
mentions_table = config['entity_mentions_table']
rels_table = config['relationships_table']

if spark.catalog.tableExists(entities_table):
    has_mentions = spark.catalog.tableExists(mentions_table)
    has_rels = spark.catalog.tableExists(rels_table)

    if has_mentions and has_rels:
        orphaned = spark.sql(f"""
            SELECT e.entity_id
            FROM {entities_table} e
            LEFT JOIN {mentions_table} em ON e.entity_id = em.entity_id
            LEFT JOIN {rels_table} r_src ON e.entity_id = r_src.source_entity
            LEFT JOIN {rels_table} r_tgt ON e.entity_id = r_tgt.target_entity
            WHERE em.entity_id IS NULL
              AND r_src.source_entity IS NULL
              AND r_tgt.target_entity IS NULL
        """)
        orphan_count = orphaned.count()

        if orphan_count > 0:
            orphaned.createOrReplaceTempView("orphaned_entities")
            spark.sql(f"""
                DELETE FROM {entities_table}
                WHERE entity_id IN (SELECT entity_id FROM orphaned_entities)
            """)
            print(f"Removed {orphan_count} orphaned entities")
        else:
            print("No orphaned entities to remove")

    remaining = spark.table(entities_table).count()
    print(f"Remaining entities: {remaining}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Rebuild Graph Analytics

# COMMAND ----------

# DBTITLE 1,Recompute Degree Centrality + PageRank + Cross-Testament
import networkx as nx
from pyspark.sql.types import DoubleType

rels = spark.table(config['relationships_table'])
entities = spark.table(config['entities_table'])

if entities.count() == 0 or rels.count() == 0:
    spark.sql(f"DELETE FROM {config['entity_analytics_table']} WHERE 1=1")
    spark.sql(f"DELETE FROM {config['entity_paths_table']} WHERE 1=1")
    print("No entities/relationships remain — cleared analytics tables")
else:
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

    # BFS paths
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
# MAGIC ## Step 4: Update Book Registry

# COMMAND ----------

# DBTITLE 1,Reset Removed Books to Available
reg_table = config['book_registry_table']
if spark.catalog.tableExists(reg_table):
    spark.sql(f"""
        UPDATE {reg_table}
        SET status = 'available',
            entity_count = 0,
            relationship_count = 0,
            verse_count = 0,
            added_at = NULL,
            updated_at = current_timestamp()
        WHERE book_name IN ({book_list_sql})
    """)
    print(f"Reset {len(books_to_remove)} books to 'available' in registry")

# COMMAND ----------

# DBTITLE 1,Summary
result = {
    "status": "success",
    "books_removed": books_to_remove,
    "remaining_entities": spark.table(config['entities_table']).count(),
    "remaining_relationships": spark.table(config['relationships_table']).count(),
    "remaining_verses": spark.table(config['verses_table']).count(),
}
print(json.dumps(result, indent=2))
dbutils.notebook.exit(json.dumps(result))
