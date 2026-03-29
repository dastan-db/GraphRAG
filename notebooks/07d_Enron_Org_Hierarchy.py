# Databricks notebook source
# MAGIC %md
# MAGIC # 07d — Enron Org Hierarchy & Investigation Timeline
# MAGIC
# MAGIC Create two curated reference tables seeded from public record:
# MAGIC - **`org_hierarchy`** — Enron organizational structure with titles and reporting lines
# MAGIC - **`investigation_timeline`** — Key dates from the Enron investigation
# MAGIC
# MAGIC These tables are consumed by the adaptive agent's fast-path execution
# MAGIC plans for organizational hierarchy and temporal questions.
# MAGIC
# MAGIC **Sources:** SEC filings, congressional testimony, DOJ prosecution records,
# MAGIC Powers Report (2002), Batson Report (2003).

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType, DateType, ArrayType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Organizational Hierarchy
# MAGIC
# MAGIC Curated from public record. Each entry has temporal validity — Enron's
# MAGIC reporting structure shifted significantly between 2000-2002.

# COMMAND ----------

# DBTITLE 1,Curated Org Chart Data
CURATED_HIERARCHY = [
    # (person_id, name, title, department, reports_to_id, effective_from, effective_to, source)
    # Board & C-Suite
    ("kenneth_lay", "Kenneth Lay", "Chairman & CEO", "Enron Corp", None, "1986-07-01", "2001-02-12", "SEC filings"),
    ("kenneth_lay", "Kenneth Lay", "Chairman", "Enron Corp", None, "2001-02-12", "2001-08-14", "SEC filings"),
    ("kenneth_lay", "Kenneth Lay", "Chairman & CEO (reinstated)", "Enron Corp", None, "2001-08-14", "2002-01-23", "SEC filings"),
    ("jeff_skilling", "Jeff Skilling", "President & COO", "Enron Corp", "kenneth_lay", "1997-01-01", "2001-02-12", "SEC filings"),
    ("jeff_skilling", "Jeff Skilling", "CEO", "Enron Corp", "kenneth_lay", "2001-02-12", "2001-08-14", "SEC filings"),
    ("andrew_fastow", "Andrew Fastow", "CFO", "Enron Corp", "jeff_skilling", "1998-01-01", "2001-10-24", "SEC filings"),
    ("richard_causey", "Richard Causey", "Chief Accounting Officer", "Enron Corp", "andrew_fastow", "1999-01-01", "2002-02-14", "SEC filings"),
    ("jeff_mcmahon", "Jeff McMahon", "Treasurer", "Enron Corp", "andrew_fastow", "1998-01-01", "2001-03-01", "congressional testimony"),
    ("jeff_mcmahon", "Jeff McMahon", "CFO", "Enron Corp", "kenneth_lay", "2001-10-25", "2002-01-28", "SEC filings"),
    ("ben_glisan", "Ben Glisan", "Treasurer", "Enron Corp", "andrew_fastow", "2001-03-01", "2001-11-08", "DOJ prosecution"),

    # Senior Executives
    ("greg_whalley", "Greg Whalley", "President & COO", "Enron Corp", "kenneth_lay", "2001-08-14", "2002-01-23", "SEC filings"),
    ("cliff_baxter", "Cliff Baxter", "Vice Chairman", "Enron Corp", "jeff_skilling", "2000-01-01", "2001-05-02", "SEC filings"),
    ("rebecca_mark", "Rebecca Mark", "CEO", "Enron International / Azurix", "kenneth_lay", "1999-01-01", "2000-08-01", "SEC filings"),
    ("lou_pai", "Lou Pai", "CEO", "Enron Energy Services", "jeff_skilling", "1999-01-01", "2001-06-01", "SEC filings"),
    ("mark_frevert", "Mark Frevert", "Vice Chairman", "Enron Wholesale Services", "jeff_skilling", "2001-02-12", "2002-01-23", "SEC filings"),

    # Division Heads
    ("david_delainey", "David Delainey", "CEO", "Enron Energy Services", "jeff_skilling", "2001-06-01", "2001-12-02", "DOJ prosecution"),
    ("john_lavorato", "John Lavorato", "COO", "Enron Energy Services", "david_delainey", "2001-01-01", "2001-12-02", "DOJ prosecution"),
    ("kenneth_rice", "Kenneth Rice", "CEO", "Enron Broadband Services", "jeff_skilling", "2000-01-01", "2001-08-01", "DOJ prosecution"),
    ("tim_belden", "Tim Belden", "Head of Trading", "Enron West Power Trading", "jeff_skilling", "1998-01-01", "2001-12-02", "DOJ prosecution"),
    ("michael_kopper", "Michael Kopper", "Managing Director", "Global Finance", "andrew_fastow", "1997-01-01", "2001-08-01", "DOJ prosecution"),

    # Key Support Executives
    ("james_derrick", "James Derrick", "General Counsel", "Enron Corp", "kenneth_lay", "1999-01-01", "2002-01-23", "SEC filings"),
    ("rick_buy", "Rick Buy", "Chief Risk Officer", "Enron Corp", "jeff_skilling", "2000-01-01", "2002-01-23", "Powers Report"),
    ("sherron_watkins", "Sherron Watkins", "VP Corporate Development", "Enron Corp", "andrew_fastow", "2001-01-01", "2002-01-23", "congressional testimony"),
    ("vince_kaminski", "Vince Kaminski", "MD Research", "Enron Wholesale Services", "rick_buy", "2000-01-01", "2001-12-02", "Powers Report"),
]

# COMMAND ----------

# DBTITLE 1,Write Org Hierarchy Table
enron_schema = config['enron_schema']
org_table = f"{config['catalog']}.{enron_schema}.org_hierarchy"

schema = StructType([
    StructField("person_id", StringType()),
    StructField("name", StringType()),
    StructField("title", StringType()),
    StructField("department", StringType()),
    StructField("reports_to_id", StringType()),
    StructField("effective_from", StringType()),
    StructField("effective_to", StringType()),
    StructField("source", StringType()),
])

rows = [(pid, name, title, dept, rpt, efrom, eto, src)
        for pid, name, title, dept, rpt, efrom, eto, src in CURATED_HIERARCHY]

df = (
    spark.createDataFrame(rows, schema)
    .withColumn("effective_from", F.col("effective_from").cast(DateType()))
    .withColumn("effective_to", F.col("effective_to").cast(DateType()))
)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(org_table)

row_count = spark.table(org_table).count()
unique_people = spark.table(org_table).select("person_id").distinct().count()
print(f"Org hierarchy: {row_count} entries for {unique_people} people → {org_table}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Investigation Timeline
# MAGIC
# MAGIC Key dates from the Enron investigation, sourced from public record.
# MAGIC These anchor temporal queries ("what happened before/after X").

# COMMAND ----------

# DBTITLE 1,Curated Timeline Events
TIMELINE_EVENTS = [
    # (date, description, category, key_persons)
    ("2000-08-23", "Enron stock hits all-time high of $90.56", "financial_event", ["Kenneth Lay", "Jeff Skilling"]),
    ("2001-02-12", "Skilling promoted to CEO; Lay remains Chairman", "leadership_change", ["Jeff Skilling", "Kenneth Lay"]),
    ("2001-03-01", "Jeff McMahon moved from Treasurer (after complaining about Fastow conflicts)", "leadership_change", ["Jeff McMahon", "Andrew Fastow"]),
    ("2001-04-17", "Skilling on earnings call: Enron stock is 'an incredible bargain'", "public_statement", ["Jeff Skilling"]),
    ("2001-05-02", "Cliff Baxter resigns as Vice Chairman", "resignation", ["Cliff Baxter"]),
    ("2001-06-01", "Lou Pai leaves Enron Energy Services; David Delainey replaces", "leadership_change", ["Lou Pai", "David Delainey"]),
    ("2001-08-14", "Skilling resigns as CEO citing 'personal reasons'; Lay reinstated", "resignation", ["Jeff Skilling", "Kenneth Lay"]),
    ("2001-08-15", "Sherron Watkins sends anonymous warning letter to Kenneth Lay", "whistleblower", ["Sherron Watkins", "Kenneth Lay"]),
    ("2001-08-22", "Watkins meets with Lay in person about accounting concerns", "whistleblower", ["Sherron Watkins", "Kenneth Lay"]),
    ("2001-09-26", "Lay tells employees on call: stock is 'an incredible bargain'", "public_statement", ["Kenneth Lay"]),
    ("2001-10-12", "Arthur Andersen begins shredding Enron documents", "document_destruction", []),
    ("2001-10-16", "Enron reports $618M Q3 loss and $1.2B equity writedown", "financial_event", ["Kenneth Lay"]),
    ("2001-10-22", "SEC opens informal inquiry into Enron", "regulatory", []),
    ("2001-10-24", "Fastow removed as CFO; replaced by Jeff McMahon", "leadership_change", ["Andrew Fastow", "Jeff McMahon"]),
    ("2001-10-31", "SEC upgrades to formal investigation", "regulatory", []),
    ("2001-11-08", "Enron restates earnings for 1997-2001 (reduces by $586M)", "financial_event", []),
    ("2001-11-09", "Dynegy announces $8.4B merger with Enron", "financial_event", []),
    ("2001-11-28", "Dynegy pulls out of merger; credit agencies downgrade to junk", "financial_event", []),
    ("2001-12-02", "Enron files Chapter 11 bankruptcy", "bankruptcy", ["Kenneth Lay"]),
    ("2002-01-09", "DOJ opens criminal investigation", "criminal_investigation", []),
    ("2002-01-23", "Kenneth Lay resigns as Chairman", "resignation", ["Kenneth Lay"]),
    ("2002-01-25", "Cliff Baxter found dead (ruled suicide)", "death", ["Cliff Baxter"]),
    ("2002-02-07", "Watkins testifies before Senate Commerce Committee", "congressional_testimony", ["Sherron Watkins"]),
    ("2002-03-14", "Arthur Andersen indicted for obstruction of justice", "criminal_investigation", []),
    ("2002-10-31", "Fastow indicted on 78 counts of fraud", "criminal_investigation", ["Andrew Fastow"]),
    ("2004-02-19", "Skilling indicted on 35 counts", "criminal_investigation", ["Jeff Skilling"]),
    ("2004-07-08", "Lay indicted on 11 counts", "criminal_investigation", ["Kenneth Lay"]),
    ("2006-05-25", "Lay and Skilling convicted on multiple counts", "conviction", ["Kenneth Lay", "Jeff Skilling"]),
]

# COMMAND ----------

# DBTITLE 1,Write Investigation Timeline Table
timeline_table = f"{config['catalog']}.{enron_schema}.investigation_timeline"

timeline_schema = StructType([
    StructField("event_date", StringType()),
    StructField("description", StringType()),
    StructField("category", StringType()),
    StructField("key_persons", ArrayType(StringType())),
])

timeline_rows = [(d, desc, cat, persons) for d, desc, cat, persons in TIMELINE_EVENTS]

timeline_df = (
    spark.createDataFrame(timeline_rows, timeline_schema)
    .withColumn("event_date", F.col("event_date").cast(DateType()))
    .withColumn("event_id", F.concat(
        F.date_format("event_date", "yyyyMMdd"),
        F.lit("_"),
        F.monotonically_increasing_id(),
    ))
)

timeline_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(timeline_table)

event_count = spark.table(timeline_table).count()
print(f"Investigation timeline: {event_count} events → {timeline_table}")

# COMMAND ----------

# DBTITLE 1,Display Timeline
display(
    spark.table(timeline_table)
    .select("event_date", "category", "description", "key_persons")
    .orderBy("event_date")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Org hierarchy and investigation timeline tables built. These are consumed
# MAGIC by the adaptive agent's fast-path execution plans for organizational
# MAGIC and temporal questions.