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
# MAGIC **Option B** — Auto-download from the CMU Enron Email Dataset archive
# MAGIC (runs automatically if the CSV is not found in the volume).

# COMMAND ----------

# DBTITLE 1,Download and Extract (idempotent — skips if emails table populated)
CMU_URL = "https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz"

_existing = 0
try:
    _existing = spark.table(config['enron_emails_table']).count()
except Exception:
    pass

if _existing > 0:
    print(f"✓ Emails table already populated ({_existing:,} rows) — skipping download.")
    raw_df = None
else:
    import io
    import tarfile
    import requests

    print(f"Downloading {CMU_URL} (~423 MB)...")
    resp = requests.get(CMU_URL, stream=True, timeout=600)
    resp.raise_for_status()
    chunks = []
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        chunks.append(chunk)
    raw_bytes = b"".join(chunks)
    del chunks
    print(f"Downloaded {len(raw_bytes) / 1024 / 1024:.0f} MB")

    # NOTE: In the CMU archive, every email file ends with '.' (e.g. maildir/lay-k/inbox/1.)
    # Do NOT filter on endswith('.') — that discards ALL emails.
    print("Extracting emails from archive...")
    rows = []
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tar:
        for member in tar:
            if member.isfile():
                try:
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    raw = f.read().decode("utf-8", errors="replace")
                    rows.append((member.name, raw))
                except Exception:
                    continue
    del raw_bytes
    print(f"Extracted {len(rows):,} raw email files")

    raw_schema = StructType([StructField("file", StringType()), StructField("message", StringType())])
    raw_df = spark.createDataFrame(rows, raw_schema)
    del rows
    print(f"Total raw emails: {raw_df.count():,}")

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
if raw_df is not None:
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
else:
    parsed_df = None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Filter to Key Custodians
# MAGIC
# MAGIC Focus on emails from the most relevant executives and employees
# MAGIC to keep the demo dataset manageable while preserving rich connectivity.

# COMMAND ----------

# DBTITLE 1,Filter by Custodian
if parsed_df is not None:
    custodian_pattern = "|".join(config['enron_key_custodians'])
    filtered_df = (
        parsed_df
        .filter(F.col("mailbox_path").rlike(f"({custodian_pattern})"))
        .dropDuplicates(["message_id"])
        .limit(config['enron_max_emails'])
    )
    email_count = filtered_df.count()
    print(f"Filtered emails (key custodians, deduplicated): {email_count:,}")
else:
    filtered_df = None
    print("Skipped — using existing data.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Reconstruct Threads

# COMMAND ----------

# DBTITLE 1,Thread Assignment
if filtered_df is not None:
    threaded_df = (
        filtered_df
        .withColumn(
            "subject_clean",
            F.lower(F.regexp_replace(F.col("subject"), r"^(?:re|fw|fwd)\s*:\s*", "")),
        )
        .withColumn(
            "thread_id",
            F.concat(F.lit("subj:"), F.col("subject_clean")),
        )
    )
    thread_count = threaded_df.select("thread_id").distinct().count()
    print(f"Reconstructed threads: {thread_count:,}")
else:
    threaded_df = None
    print("Skipped — using existing data.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Write Delta Tables

# COMMAND ----------

# DBTITLE 1,Write Emails Table
if threaded_df is not None:
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
else:
    emails_final = spark.table(config['enron_emails_table'])
    print(f"Using existing emails table: {emails_final.count():,} rows")

# COMMAND ----------

# DBTITLE 1,Build Participants Table
if threaded_df is not None:
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
else:
    print(f"Using existing participants table")

# COMMAND ----------

# DBTITLE 1,Build Threads Table
if threaded_df is not None:
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
else:
    print(f"Using existing threads table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5b: Classify Email Sensitivity (ABAC)
# MAGIC
# MAGIC Assign a `sensitivity` label to each email based on signals already present
# MAGIC in the data. These labels drive Unity Catalog row-filter policies that
# MAGIC restrict which emails (and therefore which knowledge-graph entities) each
# MAGIC user tier can see.
# MAGIC
# MAGIC | Label | Rule | Visible to |
# MAGIC |---|---|---|
# MAGIC | `attorney_client_privileged` | Sender domain contains "legal", or subject/body contains privilege keywords | legal_team only |
# MAGIC | `executive_confidential` | Sender is a C-suite custodian or BCC includes an executive | legal_team, executive_team |
# MAGIC | `general` | Everything else | all tiers |

# COMMAND ----------

# DBTITLE 1,Classify Email Sensitivity
EXECUTIVE_CUSTODIANS = ['lay-k', 'skilling-j', 'fastow-a', 'delainey-d']
LEGAL_KEYWORDS = [
    'privileged', 'attorney-client', 'attorney client',
    'confidential legal', 'legal counsel', 'work product',
    'litigation hold', 'legal department',
]
legal_pattern = '|'.join(LEGAL_KEYWORDS)

emails_table = config['enron_emails_table']
exec_pattern = '|'.join(EXECUTIVE_CUSTODIANS)

spark.sql(f"ALTER TABLE {emails_table} ADD COLUMNS (sensitivity STRING)")

spark.sql(f"""
    UPDATE {emails_table}
    SET sensitivity = CASE
        WHEN LOWER(sender) RLIKE '.*(legal|lawyer|counsel).*'
          OR LOWER(subject) RLIKE '{legal_pattern}'
          OR LOWER(body) RLIKE '{legal_pattern}'
        THEN 'attorney_client_privileged'

        WHEN mailbox_path RLIKE '({exec_pattern})'
          OR LOWER(sender) RLIKE '({exec_pattern})'
          OR (SIZE(bcc_recipients) > 0
              AND EXISTS(bcc_recipients, r -> LOWER(r) RLIKE '({exec_pattern})'))
        THEN 'executive_confidential'

        ELSE 'general'
    END
    WHERE sensitivity IS NULL
""")

tier_counts = (
    spark.table(emails_table)
    .groupBy("sensitivity")
    .agg(F.count("*").alias("email_count"))
    .orderBy("sensitivity")
)
display(tier_counts)
print("Sensitivity classification complete.")

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
