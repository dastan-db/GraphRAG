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
# MAGIC ## Step 4b: Semantic Thread Merge
# MAGIC
# MAGIC Subject-line grouping is fast but fragile — minor subject variations
# MAGIC (e.g., "FW: Budget" vs "Fwd: Budget Review") create separate threads.
# MAGIC This pass uses `ai_query()` with the cheap 8B model to merge candidate
# MAGIC thread pairs that have similar subjects and overlapping participants.

# COMMAND ----------

# DBTITLE 1,AI Thread Merge Pass
threads_table = config['enron_threads_table']
small_llm = config['small_llm_endpoint']

_merge_done = False
try:
    _merge_done = spark.catalog.tableExists(f"{config['catalog']}.{config['enron_schema']}.thread_merges") and \
                  spark.table(f"{config['catalog']}.{config['enron_schema']}.thread_merges").count() > 0
except Exception:
    pass

if not _merge_done:
    merge_candidates_table = f"{config['catalog']}.{config['enron_schema']}.thread_merge_candidates_temp"
    merge_results_table = f"{config['catalog']}.{config['enron_schema']}.thread_merges"

    # Candidate selection:
    #   - Exclude blank / very short subjects (< 3 chars after stripping
    #     re:/fw: prefixes) — blank subjects alone create a massive
    #     combinatorial explosion (859 emails share subject "").
    #   - Levenshtein distance ≤ 3 on normalised subjects.
    #   - At least 2 participants in common to avoid false positives from
    #     high-volume senders who appear in many threads.
    spark.sql(f"""
        CREATE OR REPLACE TEMPORARY VIEW thread_pairs AS
        WITH cleaned AS (
            SELECT
                thread_id,
                subject,
                LOWER(TRIM(REGEXP_REPLACE(subject, '^(?i)(re|fw|fwd)\\s*:\\s*', ''))) AS subject_norm,
                participants
            FROM {threads_table}
        )
        SELECT
            t1.thread_id AS thread_a,
            t2.thread_id AS thread_b,
            t1.subject   AS subject_a,
            t2.subject   AS subject_b,
            CONCAT_WS(', ', t1.participants) AS participants_a,
            CONCAT_WS(', ', t2.participants) AS participants_b
        FROM cleaned t1
        JOIN cleaned t2
            ON  t1.thread_id < t2.thread_id
            AND LENGTH(t1.subject_norm) >= 3
            AND LENGTH(t2.subject_norm) >= 3
            AND LEVENSHTEIN(t1.subject_norm, t2.subject_norm) <= 3
            AND SIZE(ARRAY_INTERSECT(t1.participants, t2.participants)) >= 2
    """)

    candidate_count = spark.sql("SELECT COUNT(*) FROM thread_pairs").collect()[0][0]
    print(f"Thread merge candidates: {candidate_count:,} pairs")

    if candidate_count > 0 and candidate_count <= 10000:
        print("Running ai_query() thread merge assessment...")
        # ai_query with failOnError => false returns
        # STRUCT<result:STRING, errorMessage:STRING>
        spark.sql(f"""
            SELECT
                thread_a,
                thread_b,
                ai_query(
                    '{small_llm}',
                    CONCAT(
                        'Are these two email threads about the same conversation topic?\n',
                        'Thread A subject: ', subject_a, '\n',
                        'Thread A participants: ', participants_a, '\n',
                        'Thread B subject: ', subject_b, '\n',
                        'Thread B participants: ', participants_b, '\n',
                        'Answer YES or NO only.'
                    ),
                    modelParameters => named_struct('temperature', 0.0, 'max_tokens', 8),
                    failOnError => false
                ) AS merge_decision
            FROM thread_pairs
        """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(merge_candidates_table)

        spark.sql(f"""
            CREATE OR REPLACE TABLE {merge_results_table} AS
            SELECT thread_a AS alias_thread_id, thread_b AS canonical_thread_id
            FROM {merge_candidates_table}
            WHERE merge_decision.errorMessage IS NULL
              AND UPPER(TRIM(merge_decision.result)) LIKE 'YES%'
        """)

        merge_count = spark.table(merge_results_table).count()
        print(f"Threads to merge: {merge_count:,}")

        if merge_count > 0:
            spark.sql(f"""
                MERGE INTO {threads_table} AS t
                USING {merge_results_table} AS m
                ON t.thread_id = m.alias_thread_id
                WHEN MATCHED THEN UPDATE SET
                    t.thread_id = m.canonical_thread_id
            """)
            print(f"Applied {merge_count:,} thread merges")

        spark.sql(f"DROP TABLE IF EXISTS {merge_candidates_table}")
    else:
        spark.sql(f"""
            CREATE OR REPLACE TABLE {merge_results_table} (
                alias_thread_id STRING, canonical_thread_id STRING
            ) USING DELTA
        """)
        if candidate_count > 10000:
            print(f"Too many candidates ({candidate_count:,}) — skipping AI merge. Consider tightening filters.")
        else:
            print("No merge candidates found — threads are well-separated.")
else:
    merge_count = spark.table(f"{config['catalog']}.{config['enron_schema']}.thread_merges").count()
    print(f"Thread merge already done ({merge_count:,} merges)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5a: AI-Enriched Email Parsing
# MAGIC
# MAGIC Use `ai_query()` to extract clean body text (stripping quoted reply chains),
# MAGIC sender display name, sentiment, and key topics from each email. This
# MAGIC replaces fragile regex-based body cleaning with LLM understanding and
# MAGIC adds enrichment fields consumed by downstream extraction and the agent.

# COMMAND ----------

# DBTITLE 1,AI Email Enrichment via ai_query()
emails_table = config['enron_emails_table']
llm_endpoint = config['small_llm_endpoint']

# ---------------------------------------------------------------------------
# Ensure the sensitivity column exists. A downstream row-filter references it;
# if it is missing, ALL queries fail with
# ROW_LEVEL_SECURITY_COLUMN_MASK_UNRESOLVED_REFERENCE_COLUMN.
# ---------------------------------------------------------------------------
try:
    spark.sql(f"ALTER TABLE {emails_table} ADD COLUMNS (sensitivity STRING)")
except Exception as _e:
    if "FIELD_ALREADY_EXISTS" not in str(_e) and "already exists" not in str(_e).lower():
        raise

# ---------------------------------------------------------------------------
# Temporarily drop row-filter and column-mask policies.
# When sensitivity is NULL the filter returns FALSE for every row, making the
# table unreadable. We strip the policies, enrich + classify, then restore
# them in cell 24 once sensitivity values are populated.
# ---------------------------------------------------------------------------
try:
    spark.sql(f"ALTER TABLE {emails_table} DROP ROW FILTER")
    print("Dropped row filter")
except Exception as _e:
    print(f"Row filter: {_e}")

try:
    spark.sql(f"ALTER TABLE {emails_table} ALTER COLUMN bcc_recipients DROP MASK")
    print("Dropped column mask on bcc_recipients")
except Exception as _e:
    print(f"Column mask: {_e}")

# ---------------------------------------------------------------------------
# AI Enrichment: clean_body, sender_display_name, sentiment, topics, is_forward
# ---------------------------------------------------------------------------
_enriched_col_exists = False
try:
    _cols = [f.name for f in spark.table(emails_table).schema.fields]
    _enriched_col_exists = "clean_body" in _cols
    # Also check if enrichment data is actually populated
    if _enriched_col_exists:
        _has_data = spark.table(emails_table).filter("clean_body IS NOT NULL").limit(1).count() > 0
        if not _has_data:
            _enriched_col_exists = False  # Columns exist but are empty — re-run enrichment
            print("Enrichment columns exist but are empty — will re-run enrichment")
except Exception:
    pass

if not _enriched_col_exists:
    for col_name, col_type in [
        ("clean_body", "STRING"),
        ("sender_display_name", "STRING"),
        ("sentiment", "STRING"),
        ("key_topics", "ARRAY<STRING>"),
        ("is_forward", "BOOLEAN"),
    ]:
        try:
            spark.sql(f"ALTER TABLE {emails_table} ADD COLUMNS ({col_name} {col_type})")
        except Exception as _e:
            if "FIELD_ALREADY_EXISTS" not in str(_e) and "already exists" not in str(_e).lower():
                raise

    enrichment_table = f"{config['catalog']}.{config['enron_schema']}.email_enrichment_temp"
    spark.sql(f"DROP TABLE IF EXISTS {enrichment_table}")

    # ai_query responseFormat requires exactly one top-level field.
    # With failOnError => false the return type is always
    # STRUCT<result:STRING, errorMessage:STRING> where result is JSON.
    inner_schema = 'clean_body STRING, sender_display_name STRING, sentiment STRING, key_topics ARRAY<STRING>, is_forward BOOLEAN'

    print("Running ai_query() email enrichment (clean body, sentiment, topics)...")
    spark.sql(f"""
        SELECT
            message_id,
            ai_query(
                '{llm_endpoint}',
                CONCAT(
                    'Extract structured fields from this email.\\n',
                    'Rules:\\n',
                    '- clean_body: the email body WITHOUT quoted replies, forwarded headers, or signature blocks. Keep only the original message text.\\n',
                    '- sender_display_name: the human-readable name of the sender (from the X-From or From header)\\n',
                    '- sentiment: one of positive, neutral, negative\\n',
                    '- key_topics: 1-3 short topic tags describing what this email is about\\n',
                    '- is_forward: true if the email is forwarding another message\\n\\n',
                    'From: ', COALESCE(sender, ''), '\\n',
                    'X-From: ', COALESCE(x_from, ''), '\\n',
                    'Subject: ', COALESCE(subject, ''), '\\n',
                    'Body:\\n', SUBSTRING(COALESCE(body, ''), 1, 4000)
                ),
                responseFormat => 'STRUCT<result: STRUCT<{inner_schema}>>',
                modelParameters => named_struct('temperature', 0.0, 'max_tokens', 2048),
                failOnError => false
            ) AS enriched
        FROM {emails_table}
    """).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(enrichment_table)

    # Parse the JSON string in enriched.result.
    # The LLM may return flat JSON or wrapped in a "result" key — handle both.
    spark.sql(f"""
        MERGE INTO {emails_table} AS target
        USING (
            SELECT
                message_id,
                COALESCE(
                    from_json(enriched.result, 'STRUCT<result: STRUCT<{inner_schema}>>').result,
                    from_json(enriched.result, 'STRUCT<{inner_schema}>')
                ) AS parsed,
                enriched.errorMessage
            FROM {enrichment_table}
        ) AS src
        ON target.message_id = src.message_id
        WHEN MATCHED AND src.errorMessage IS NULL AND src.parsed IS NOT NULL THEN UPDATE SET
            target.clean_body = src.parsed.clean_body,
            target.sender_display_name = src.parsed.sender_display_name,
            target.sentiment = src.parsed.sentiment,
            target.key_topics = src.parsed.key_topics,
            target.is_forward = src.parsed.is_forward
    """)

    enriched_count = spark.table(emails_table).filter("clean_body IS NOT NULL").count()
    total_count = spark.table(emails_table).count()
    print(f"AI enrichment complete: {enriched_count:,}/{total_count:,} emails enriched")

    spark.sql(f"DROP TABLE IF EXISTS {enrichment_table}")
else:
    enriched_count = spark.table(emails_table).filter("clean_body IS NOT NULL").count()
    print(f"AI enrichment already done ({enriched_count:,} emails have clean_body)")

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

try:
    spark.sql(f"ALTER TABLE {emails_table} ADD COLUMNS (sensitivity STRING)")
except Exception as _e:
    if "FIELD_ALREADY_EXISTS" in str(_e) or "already exists" in str(_e).lower():
        print("sensitivity column already exists — skipping ALTER TABLE")
    else:
        raise

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

# ---------------------------------------------------------------------------
# Restore ABAC row-filter and column-mask policies.
# These were dropped in cell 22 to allow enrichment and classification
# to proceed while sensitivity was NULL.
# ---------------------------------------------------------------------------
catalog = config['catalog']
schema = config['enron_schema']

spark.sql(f"""
    CREATE OR REPLACE FUNCTION {catalog}.{schema}.email_access_filter(sensitivity STRING)
    RETURNS BOOLEAN
    RETURN
        CASE
            WHEN is_account_group_member('legal_team') THEN TRUE
            WHEN is_account_group_member('executive_team')
                THEN sensitivity IN ('general', 'executive_confidential')
            WHEN is_account_group_member('analyst_team')
                THEN sensitivity = 'general'
            ELSE TRUE
        END
""")
print("Restored row filter function")

spark.sql(f"""
    ALTER TABLE {emails_table}
    SET ROW FILTER {catalog}.{schema}.email_access_filter ON (sensitivity)
""")
print("Applied row filter to emails table")

spark.sql(f"""
    CREATE OR REPLACE FUNCTION {catalog}.{schema}.mask_bcc(bcc ARRAY<STRING>)
    RETURNS ARRAY<STRING>
    RETURN
        CASE
            WHEN is_account_group_member('legal_team') THEN bcc
            ELSE NULL
        END
""")

spark.sql(f"""
    ALTER TABLE {emails_table}
    ALTER COLUMN bcc_recipients SET MASK {catalog}.{schema}.mask_bcc
""")
print("Applied column mask on bcc_recipients")

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
