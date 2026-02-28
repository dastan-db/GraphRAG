# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Data Preparation
# MAGIC
# MAGIC Load the King James Version (KJV) Bible into a structured Delta table. We download a public-domain JSON source, filter to our five selected books, and write a `verses` table with one row per verse.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install requests --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import json
import requests
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Download Bible Text
# MAGIC
# MAGIC We use the [aruljohn/Bible-kjv](https://github.com/aruljohn/Bible-kjv) public-domain repository which provides each book as a separate JSON file with chapter/verse structure.

# COMMAND ----------

# DBTITLE 1,Download and Parse Selected Books
BASE_URL = "https://raw.githubusercontent.com/aruljohn/Bible-kjv/master"

rows = []
for book_name, meta in config['bible_books'].items():
    print(f"Downloading {book_name}...")
    url = f"{BASE_URL}/{book_name}.json"
    resp = requests.get(url, timeout=30)
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

print(f"\nTotal verses downloaded: {len(rows)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Write to Delta Table

# COMMAND ----------

# DBTITLE 1,Create DataFrame and Write to Delta
schema = StructType([
    StructField("book", StringType(), False),
    StructField("chapter", IntegerType(), False),
    StructField("verse_number", IntegerType(), False),
    StructField("text", StringType(), False),
    StructField("testament", StringType(), False),
])

df = spark.createDataFrame(rows, schema=schema)

(
    df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['verses_table'])
)

print(f"Wrote {df.count()} verses to {config['verses_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Verify and Explore
# MAGIC
# MAGIC Chapter text previews below use ordered verse aggregation (`ARRAY_SORT + COLLECT_LIST`) to preserve narrative order.

# COMMAND ----------

# DBTITLE 1,Verse Counts per Book
display(
    spark.table(config['verses_table'])
    .groupBy("book", "testament")
    .agg(
        F.count("*").alias("total_verses"),
        F.countDistinct("chapter").alias("chapters"),
    )
    .orderBy("testament", "book")
)

# COMMAND ----------

# DBTITLE 1,Sample Verses
display(
    spark.table(config['verses_table'])
    .filter("book = 'Genesis' AND chapter = 1")
    .orderBy("verse_number")
    .limit(10)
)

# COMMAND ----------

# DBTITLE 1,Chapter-Level Text Preview
display(
    spark.table(config['verses_table'])
    .groupBy("book", "chapter")
    .agg(
        F.count("*").alias("verse_count"),
        F.concat_ws(" ",
            F.transform(
                F.array_sort(F.collect_list(F.struct("verse_number", "text"))),
                lambda x: x["text"]
            )
        ).alias("chapter_text_preview"),
    )
    .withColumn("chapter_text_preview", F.substring("chapter_text_preview", 1, 300))
    .orderBy("book", "chapter")
    .limit(10)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Data is ready. Proceed to **02_Build_Knowledge_Graph** for entity and relationship extraction.
