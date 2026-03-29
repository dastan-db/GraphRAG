# Databricks notebook source
# MAGIC %md
# MAGIC # 07i — Enron Email Classification (M3)
# MAGIC
# MAGIC Heuristic email typing and metadata flags for downstream retrieval and ABAC.
# MAGIC
# MAGIC **Table:** `email_classification`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import re

import pyspark.sql.functions as F
from pyspark.sql.types import BooleanType, IntegerType, StringType


def _reply_depth(subject: str) -> int:
    if not subject:
        return 0
    s = subject.strip()
    n = 0
    while True:
        m = re.match(r"(?i)re:\s*", s)
        if not m:
            break
        n += 1
        s = s[m.end() :].lstrip()
    return n


def _all_enron_internal(sender, to_r, cc_r, bcc_r) -> bool:
    parts = []
    if sender:
        parts.append(str(sender).strip())
    for arr in (to_r, cc_r, bcc_r):
        if not arr:
            continue
        for x in arr:
            if x:
                parts.append(str(x).strip())
    if not parts:
        return False
    for p in parts:
        if "@enron.com" not in p.lower():
            return False
    return True


def _has_attachments(x_from, body) -> bool:
    xf = (x_from or "").lower()
    bd = (body or "").lower()
    return "attachment" in xf or "attachment" in bd


def _email_type(sender: str, subject: str, body: str) -> str:
    subj = subject or ""
    subjl = subj.lower()
    snd = (sender or "").lower()
    bod = body or ""

    if "undeliverable" in subjl or "failure notice" in subjl:
        return "bounce"
    if "BEGIN:VCALENDAR" in bod:
        return "calendar"
    if (
        "postmaster" in snd
        or "mailer-daemon" in snd
        or "delivery status" in subjl
        or "out of office" in subjl
    ):
        return "automated"

    t = subj.lstrip()
    tl = t.lower()
    if tl.startswith("fwd:") or tl.startswith("fw:"):
        return "forward"
    if tl.startswith("re:"):
        return "reply"
    return "original"


reply_depth_udf = F.udf(_reply_depth, IntegerType())
all_enron_udf = F.udf(_all_enron_internal, BooleanType())
has_att_udf = F.udf(_has_attachments, BooleanType())
email_type_udf = F.udf(_email_type, StringType())

# COMMAND ----------

# DBTITLE 1,Build email_classification
emails_t = config["enron_emails_table"]
out_t = config["enron_email_classification_table"]

df = (
    spark.table(emails_t)
    .select(
        F.col("message_id"),
        F.col("sender"),
        F.col("subject"),
        F.col("body"),
        F.col("to_recipients"),
        F.col("cc_recipients"),
        F.col("bcc_recipients"),
        F.col("x_from"),
    )
    .withColumn("email_type", email_type_udf(F.col("sender"), F.col("subject"), F.col("body")))
    .withColumn("reply_depth", reply_depth_udf(F.col("subject")))
    .withColumn("has_attachments", has_att_udf(F.col("x_from"), F.col("body")))
    .withColumn(
        "is_internal",
        all_enron_udf(
            F.col("sender"),
            F.col("to_recipients"),
            F.col("cc_recipients"),
            F.col("bcc_recipients"),
        ),
    )
    .withColumn(
        "is_automated",
        F.col("email_type").isin("calendar", "automated", "bounce"),
    )
    .select(
        "message_id",
        "email_type",
        "reply_depth",
        "has_attachments",
        "is_internal",
        "is_automated",
    )
)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(out_t)

n = spark.table(out_t).count()
print(f"email_classification: {n:,} rows → {out_t}")
