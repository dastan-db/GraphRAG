# Databricks notebook source
# MAGIC %md
# MAGIC # 06 — Enron Email Data Preparation
# MAGIC
# MAGIC Load the Enron email corpus into structured Delta tables. We download
# MAGIC a pre-processed CSV from the CMU/Kaggle public dataset, parse email
# MAGIC headers and body text, reconstruct threads, and write three tables:
# MAGIC `emails`, `participants`, and `threads`.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install requests --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import email
import re
from datetime import datetime

import pyspark.sql.functions as F
from pyspark.sql.types import (
    ArrayType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Download Enron Email Data
# MAGIC
# MAGIC The dataset originates from the CMU Enron Email Dataset. We use the
# MAGIC Kaggle-cleaned CSV version (`emails.csv`) which provides one row per
# MAGIC email with columns `file` (mailbox path) and `message` (raw RFC 2822).
# MAGIC
# MAGIC **Option A** — Upload `emails.csv` to a Unity Catalog Volume first:
# MAGIC ```
# MAGIC /Volumes/{catalog}/{enron_schema}/raw/emails.csv
# MAGIC ```
# MAGIC **Option B** — Download programmatically (requires Kaggle API token).
# MAGIC
# MAGIC This notebook assumes Option A (volume upload). Adjust the path below
# MAGIC if using a different ingestion method.

# COMMAND ----------

# DBTITLE 1,Read Raw Email CSV
VOLUME_PATH = f"/Volumes/{config['catalog']}/{config['enron_schema']}/raw"

raw_df = (
    spark.read
    .option("header", "true")
    .option("multiLine", "true")
    .option("escape", '"')
    .csv(f"{VOLUME_PATH}/emails.csv")
)

print(f"Total raw emails: {raw_df.count():,}")
display(raw_df.limit(3))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Parse Email Headers and Body

# COMMAND ----------

# DBTITLE 1,Email Parser UDF
def parse_email_message(raw_message: str) -> dict:
    """Parse an RFC 2822 email into structured fields."""
    if not raw_message:
        return None

    msg = email.message_from_string(raw_message)

    def clean_addr(addr_str):
        if not addr_str:
            return []
        addrs = [a.strip() for a in addr_str.split(",")]
        return [a for a in addrs if a]

    date_str = msg.get("Date", "")
    parsed_date = None
    if date_str:
        date_str = re.sub(r"\s*\(.*?\)\s*$", "", date_str).strip()
        for fmt in (
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S",
            "%d %b %Y %H:%M:%S %z",
            "%d %b %Y %H:%M:%S",
        ):
            try:
                parsed_date = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue

    body = msg.get_payload(decode=False) or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    body = re.sub(
        r"-{3,}\s*Original Message\s*-{3,}.*",
        "",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = re.sub(
        r"-{3,}\s*Forwarded by\s.*?-{3,}",
        "",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = body.strip()

    return {
        "message_id": msg.get("Message-ID", ""),
        "date": parsed_date,
        "sender": msg.get("From", ""),
        "to_recipients": clean_addr(msg.get("To", "")),
        "cc_recipients": clean_addr(msg.get("Cc", "")),
        "bcc_recipients": clean_addr(msg.get("Bcc", "")),
        "subject": msg.get("Subject", ""),
        "body": body[:8000],
        "x_from": msg.get("X-From", ""),
        "x_to": msg.get("X-To", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
    }

# COMMAND ----------

# DBTITLE 1,Define Output Schema
email_schema = StructType([
    StructField("message_id", StringType(), True),
    StructField("date", TimestampType(), True),
    StructField("sender", StringType(), True),
    StructField("to_recipients", ArrayType(StringType()), True),
    StructField("cc_recipients", ArrayType(StringType()), True),
    StructField("bcc_recipients", ArrayType(StringType()), True),
    StructField("subject", StringType(), True),
    StructField("body", StringType(), True),
    StructField("x_from", StringType(), True),
    StructField("x_to", StringType(), True),
    StructField("in_reply_to", StringType(), True),
])

parse_email_udf = F.udf(parse_email_message, email_schema)

# COMMAND ----------

# DBTITLE 1,Parse All Emails
parsed_df = (
    raw_df
    .withColumn("parsed", parse_email_udf(F.col("message")))
    .select(
        F.col("file").alias("mailbox_path"),
        "parsed.*",
    )
    .filter(F.col("message_id").isNotNull())
    .filter(F.col("body").isNotNull() & (F.length(F.col("body")) > 10))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Filter to Key Custodians
# MAGIC
# MAGIC Focus on emails from the most relevant executives and employees
# MAGIC to keep the demo dataset manageable while preserving rich connectivity.

# COMMAND ----------

# DBTITLE 1,Filter by Custodian
custodian_pattern = "|".join(config['enron_key_custodians'])
filtered_df = (
    parsed_df
    .filter(F.col("mailbox_path").rlike(f"({custodian_pattern})"))
    .dropDuplicates(["message_id"])
    .limit(config['enron_max_emails'])
)

email_count = filtered_df.count()
print(f"Filtered emails (key custodians, deduplicated): {email_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Reconstruct Threads

# COMMAND ----------

# DBTITLE 1,Thread Assignment
threaded_df = (
    filtered_df
    .withColumn(
        "subject_clean",
        F.lower(F.regexp_replace(F.col("subject"), r"^(?:re|fw|fwd)\s*:\s*", "")),
    )
    .withColumn(
        "thread_id",
        F.coalesce(
            F.col("in_reply_to"),
            F.concat(F.lit("subj:"), F.col("subject_clean")),
        ),
    )
)

thread_count = threaded_df.select("thread_id").distinct().count()
print(f"Reconstructed threads: {thread_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Write Delta Tables

# COMMAND ----------

# DBTITLE 1,Write Emails Table
emails_final = threaded_df.select(
    "message_id", "date", "sender", "to_recipients", "cc_recipients",
    "bcc_recipients", "subject", "body", "thread_id", "mailbox_path",
    "x_from", "x_to",
)

(
    emails_final.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_emails_table'])
)

print(f"Wrote {emails_final.count():,} emails to {config['enron_emails_table']}")

# COMMAND ----------

# DBTITLE 1,Build Participants Table
senders = (
    emails_final
    .select(
        F.col("sender").alias("email_address"),
        F.col("x_from").alias("display_name"),
    )
    .distinct()
)

recipients = (
    emails_final
    .select(F.explode(F.col("to_recipients")).alias("email_address"))
    .withColumn("display_name", F.lit(None).cast(StringType()))
    .distinct()
)

cc_addrs = (
    emails_final
    .select(F.explode(F.col("cc_recipients")).alias("email_address"))
    .withColumn("display_name", F.lit(None).cast(StringType()))
    .distinct()
)

all_participants = (
    senders.unionByName(recipients).unionByName(cc_addrs)
    .groupBy("email_address")
    .agg(F.first("display_name", ignorenulls=True).alias("display_name"))
    .withColumn(
        "name_normalized",
        F.regexp_replace(
            F.coalesce(F.col("display_name"), F.col("email_address")),
            r"\s+",
            " ",
        ),
    )
    .withColumn(
        "department",
        F.regexp_extract(F.col("email_address"), r"@(\w+)", 1),
    )
)

(
    all_participants.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_participants_table'])
)

print(f"Wrote {all_participants.count():,} participants to {config['enron_participants_table']}")

# COMMAND ----------

# DBTITLE 1,Build Threads Table
threads_df = (
    emails_final
    .groupBy("thread_id")
    .agg(
        F.count("*").alias("email_count"),
        F.min("date").alias("first_email_date"),
        F.max("date").alias("last_email_date"),
        F.first("subject").alias("subject"),
        F.collect_set("sender").alias("participants"),
        F.concat_ws(
            "\n\n---\n\n",
            F.transform(
                F.array_sort(F.collect_list(F.struct("date", "sender", "body"))),
                lambda x: F.concat(
                    F.lit("From: "), x["sender"],
                    F.lit("\n"), x["body"],
                ),
            ),
        ).alias("thread_text"),
    )
    .withColumn(
        "thread_text_preview",
        F.substring("thread_text", 1, 2000),
    )
)

(
    threads_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config['enron_threads_table'])
)

print(f"Wrote {threads_df.count():,} threads to {config['enron_threads_table']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Verify and Explore

# COMMAND ----------

# DBTITLE 1,Email Volume by Month
display(
    spark.table(config['enron_emails_table'])
    .withColumn("month", F.date_trunc("month", "date"))
    .groupBy("month")
    .agg(F.count("*").alias("email_count"))
    .orderBy("month")
)

# COMMAND ----------

# DBTITLE 1,Top Senders
display(
    spark.table(config['enron_emails_table'])
    .groupBy("sender")
    .agg(F.count("*").alias("emails_sent"))
    .orderBy(F.desc("emails_sent"))
    .limit(20)
)

# COMMAND ----------

# DBTITLE 1,Thread Size Distribution
display(
    spark.table(config['enron_threads_table'])
    .groupBy("email_count")
    .agg(F.count("*").alias("thread_count"))
    .orderBy("email_count")
    .limit(20)
)

# COMMAND ----------

# DBTITLE 1,Sample Thread Preview
display(
    spark.table(config['enron_threads_table'])
    .filter(F.col("email_count") > 3)
    .select("subject", "email_count", "participants", "thread_text_preview")
    .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Data is ready. Proceed to **07_Enron_Build_Knowledge_Graph** for entity and relationship extraction.
