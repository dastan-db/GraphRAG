# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Investigative Analysis
# MAGIC
# MAGIC Forensic analysis of the Enron email corpus (~20,000 emails from key custodians, 2000-2002).
# MAGIC Uses pre-aggregated `communication_dyads`, `person_activity`, and raw `emails` tables
# MAGIC to surface patterns of interest to investigators: self-emailing, external communication,
# MAGIC BCC usage, after-hours activity, communication spikes, and keyword sweeps.
# MAGIC
# MAGIC **Tables used:**
# MAGIC - `emails` — per-message with date, sender, recipients, subject, body
# MAGIC - `communication_dyads` — weekly sender/recipient pair counts (TO/CC/BCC)
# MAGIC - `person_activity` — weekly per-person activity (sent/received, BCC, after-hours, weekend)
# MAGIC - `participants` — email address to display name mapping
# MAGIC - `investigation_timeline` — curated key events

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Setup
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
from datetime import datetime

emails_table = config['enron_emails_table']
dyads_table = config['enron_communication_dyads_table']
activity_table = config['enron_person_activity_table']
participants_table = config['enron_participants_table']
timeline_table = f"{config['catalog']}.{config['enron_schema']}.investigation_timeline"

plt.style.use('seaborn-v0_8-darkgrid')
FIGSIZE = (14, 6)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Self-Email Detection
# MAGIC
# MAGIC Find people who emailed their own personal accounts from their corporate Enron email.
# MAGIC This is a key data exfiltration signal — especially when volume is high or concentrated
# MAGIC around the collapse period (Oct-Dec 2001).

# COMMAND ----------

# DBTITLE 1,Corporate-to-Personal Self-Email Pairs
self_email_df = spark.sql(f"""
    SELECT
        d.person_a,
        d.person_b,
        SUM(d.total_count) AS total_emails,
        MAX(d.total_count) AS peak_week_volume,
        MIN(d.period) AS first_seen,
        MAX(d.period) AS last_seen,
        COUNT(DISTINCT d.period) AS active_weeks
    FROM {dyads_table} d
    WHERE (
        (d.person_a LIKE '%@enron.com' AND d.person_b NOT LIKE '%@enron.com')
        OR (d.person_b LIKE '%@enron.com' AND d.person_a NOT LIKE '%@enron.com')
    )
    GROUP BY d.person_a, d.person_b
    HAVING SUM(d.total_count) >= 5
    ORDER BY total_emails DESC
    LIMIT 100
""")

self_email_pd = self_email_df.toPandas()

def local_part(email):
    return email.split("@")[0].replace(".", "").replace("_", "").replace("-", "").lower() if "@" in email else email

def is_likely_same(row):
    a, b = row["person_a"], row["person_b"]
    la, lb = local_part(a), local_part(b)
    if la == lb:
        return True
    shorter, longer = sorted([la, lb], key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    if len(shorter) >= 5 and longer.startswith(shorter[:5]):
        return True
    return False

self_email_pd["is_self"] = self_email_pd.apply(is_likely_same, axis=1)
confirmed_self = self_email_pd[self_email_pd["is_self"]].copy()
confirmed_self = confirmed_self.sort_values("total_emails", ascending=False)

print(f"Found {len(confirmed_self)} confirmed self-email pairs out of {len(self_email_pd)} cross-domain pairs")
display(confirmed_self[["person_a", "person_b", "total_emails", "peak_week_volume", "first_seen", "last_seen", "active_weeks"]])

# COMMAND ----------

# DBTITLE 1,Self-Email Volume Chart
if len(confirmed_self) > 0:
    top_self = confirmed_self.head(15)
    labels = [f"{r['person_a'].split('@')[0]}" for _, r in top_self.iterrows()]

    fig, ax = plt.subplots(figsize=FIGSIZE)
    bars = ax.barh(range(len(top_self)), top_self["total_emails"], color="#e74c3c", alpha=0.85)
    ax.set_yticks(range(len(top_self)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Total Self-Emails")
    ax.set_title("Corporate → Personal Self-Email Volume (Top 15)")
    ax.invert_yaxis()
    plt.tight_layout()
    plt.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. External Communication Ranking
# MAGIC
# MAGIC Which Enron employees communicated most with non-Enron email addresses?
# MAGIC High external communication volume may indicate external advisors, regulators,
# MAGIC media contacts, or information leakage channels.

# COMMAND ----------

# DBTITLE 1,Top Enron Employees by External Email Volume
external_ranking = spark.sql(f"""
    SELECT
        enron_person,
        p.name_normalized AS display_name,
        SUM(total) AS total_external_emails,
        COUNT(DISTINCT external_email) AS unique_external_contacts
    FROM (
        SELECT d.person_a AS enron_person, d.person_b AS external_email,
               SUM(d.total_count) AS total
        FROM {dyads_table} d
        WHERE d.person_a LIKE '%@enron.com' AND d.person_b NOT LIKE '%@enron.com'
        GROUP BY d.person_a, d.person_b
        UNION ALL
        SELECT d.person_b AS enron_person, d.person_a AS external_email,
               SUM(d.total_count) AS total
        FROM {dyads_table} d
        WHERE d.person_b LIKE '%@enron.com' AND d.person_a NOT LIKE '%@enron.com'
        GROUP BY d.person_b, d.person_a
    ) combined
    LEFT JOIN {participants_table} p ON combined.enron_person = p.email_address
    GROUP BY enron_person, p.name_normalized
    ORDER BY total_external_emails DESC
    LIMIT 25
""")

display(external_ranking)

# COMMAND ----------

# DBTITLE 1,Top External Domains
external_domains = spark.sql(f"""
    SELECT
        SPLIT(external_email, '@')[1] AS domain,
        COUNT(DISTINCT external_email) AS unique_addresses,
        SUM(total) AS total_emails
    FROM (
        SELECT d.person_b AS external_email, SUM(d.total_count) AS total
        FROM {dyads_table} d
        WHERE d.person_a LIKE '%@enron.com' AND d.person_b NOT LIKE '%@enron.com'
        GROUP BY d.person_b
        UNION ALL
        SELECT d.person_a AS external_email, SUM(d.total_count) AS total
        FROM {dyads_table} d
        WHERE d.person_b LIKE '%@enron.com' AND d.person_a NOT LIKE '%@enron.com'
        GROUP BY d.person_a
    ) combined
    GROUP BY SPLIT(external_email, '@')[1]
    ORDER BY total_emails DESC
    LIMIT 30
""")

display(external_domains)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Temporal Anomalies — Corpus-Wide Email Volume
# MAGIC
# MAGIC Weekly email volume across the entire corpus with key investigation dates overlaid.
# MAGIC Look for volume spikes or drops around critical events.

# COMMAND ----------

# DBTITLE 1,Weekly Email Volume Time Series
volume_df = spark.sql(f"""
    SELECT DATE_TRUNC('week', date) AS week, COUNT(*) AS email_count
    FROM {emails_table}
    WHERE date IS NOT NULL
      AND date >= '1999-01-01' AND date <= '2002-12-31'
    GROUP BY 1
    ORDER BY 1
""").toPandas()

volume_df["week"] = pd.to_datetime(volume_df["week"])

key_dates = {
    "2001-08-15": "Watkins memo",
    "2001-10-16": "Q3 restatement",
    "2001-10-22": "SEC inquiry",
    "2001-11-08": "Merger collapse",
    "2001-12-02": "Bankruptcy",
}

fig, ax = plt.subplots(figsize=(16, 6))
ax.plot(volume_df["week"], volume_df["email_count"], color="#3498db", linewidth=1.5)
ax.fill_between(volume_df["week"], volume_df["email_count"], alpha=0.15, color="#3498db")

for date_str, label in key_dates.items():
    dt = pd.Timestamp(date_str)
    if volume_df["week"].min() <= dt <= volume_df["week"].max():
        ax.axvline(dt, color="#e74c3c", linestyle="--", alpha=0.7, linewidth=1)
        ax.annotate(label, xy=(dt, ax.get_ylim()[1] * 0.95),
                    fontsize=7, rotation=45, ha="right", color="#e74c3c")

ax.set_xlabel("Week")
ax.set_ylabel("Email Count")
ax.set_title("Enron Corpus — Weekly Email Volume with Key Investigation Events")
ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. BCC Usage Patterns
# MAGIC
# MAGIC BCC is the "I want someone to see this without others knowing" channel.
# MAGIC Heavy BCC usage — especially with a high BCC-to-total ratio — is an investigative red flag.

# COMMAND ----------

# DBTITLE 1,Top BCC Users (by Ratio and Volume)
bcc_ranking = spark.sql(f"""
    SELECT
        pa.person_id,
        p.name_normalized AS display_name,
        SUM(pa.bcc_emails_sent) AS bcc_total,
        SUM(pa.emails_sent) AS sent_total,
        ROUND(SUM(pa.bcc_emails_sent) * 1.0 / NULLIF(SUM(pa.emails_sent), 0), 3) AS bcc_ratio
    FROM {activity_table} pa
    LEFT JOIN {participants_table} p ON pa.person_id = p.email_address
    GROUP BY pa.person_id, p.name_normalized
    HAVING SUM(pa.emails_sent) >= 10 AND SUM(pa.bcc_emails_sent) > 0
    ORDER BY bcc_total DESC
    LIMIT 25
""")

display(bcc_ranking)

# COMMAND ----------

# DBTITLE 1,BCC Volume Over Time (Top 5 BCC Users)
bcc_timeseries = spark.sql(f"""
    SELECT pa.person_id, pa.period, pa.bcc_emails_sent
    FROM {activity_table} pa
    WHERE pa.person_id IN (
        SELECT person_id FROM (
            SELECT person_id, SUM(bcc_emails_sent) AS bcc_total
            FROM {activity_table}
            GROUP BY person_id
            HAVING SUM(emails_sent) >= 10
            ORDER BY bcc_total DESC
            LIMIT 5
        )
    )
    AND pa.bcc_emails_sent > 0
    ORDER BY pa.period
""").toPandas()

if len(bcc_timeseries) > 0:
    bcc_timeseries["period"] = pd.to_datetime(bcc_timeseries["period"])

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for person_id in bcc_timeseries["person_id"].unique():
        subset = bcc_timeseries[bcc_timeseries["person_id"] == person_id]
        label = person_id.split("@")[0].replace(".", " ").title()
        ax.plot(subset["period"], subset["bcc_emails_sent"], label=label, linewidth=1.5, marker="o", markersize=3)

    ax.set_xlabel("Week")
    ax.set_ylabel("BCC Emails Sent")
    ax.set_title("BCC Email Volume Over Time (Top 5 Users)")
    ax.legend(fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. After-Hours / Weekend Activity
# MAGIC
# MAGIC People who email heavily outside business hours or on weekends may be under pressure,
# MAGIC dealing with crises, or trying to avoid oversight during normal working hours.

# COMMAND ----------

# DBTITLE 1,Top After-Hours Emailers
after_hours = spark.sql(f"""
    SELECT
        pa.person_id,
        p.name_normalized AS display_name,
        SUM(pa.after_hours_count) AS after_hours_total,
        SUM(pa.weekend_count) AS weekend_total,
        SUM(pa.emails_sent) AS sent_total,
        ROUND(SUM(pa.after_hours_count) * 1.0 / NULLIF(SUM(pa.emails_sent), 0), 3) AS after_hours_ratio,
        ROUND(SUM(pa.weekend_count) * 1.0 / NULLIF(SUM(pa.emails_sent), 0), 3) AS weekend_ratio
    FROM {activity_table} pa
    LEFT JOIN {participants_table} p ON pa.person_id = p.email_address
    GROUP BY pa.person_id, p.name_normalized
    HAVING SUM(pa.emails_sent) >= 20
    ORDER BY after_hours_total DESC
    LIMIT 25
""")

display(after_hours)

# COMMAND ----------

# DBTITLE 1,After-Hours vs Weekend Scatter
after_hours_pd = after_hours.toPandas()

if len(after_hours_pd) > 0:
    fig, ax = plt.subplots(figsize=(10, 8))
    scatter = ax.scatter(
        after_hours_pd["after_hours_ratio"],
        after_hours_pd["weekend_ratio"],
        s=after_hours_pd["sent_total"] / after_hours_pd["sent_total"].max() * 300 + 20,
        c=after_hours_pd["sent_total"],
        cmap="YlOrRd",
        alpha=0.7,
        edgecolors="gray",
        linewidth=0.5,
    )
    for _, row in after_hours_pd.head(10).iterrows():
        label = (row["display_name"] or row["person_id"].split("@")[0]).split()[0]
        ax.annotate(label, (row["after_hours_ratio"], row["weekend_ratio"]),
                    fontsize=7, alpha=0.8)
    ax.set_xlabel("After-Hours Ratio")
    ax.set_ylabel("Weekend Ratio")
    ax.set_title("After-Hours vs Weekend Email Activity (size = total volume)")
    plt.colorbar(scatter, label="Total Emails Sent")
    plt.tight_layout()
    plt.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Communication Spikes
# MAGIC
# MAGIC Detect sudden increases in email volume between specific pairs.
# MAGIC A relationship that jumps from 2 emails/week to 30+ signals something changed.

# COMMAND ----------

# DBTITLE 1,Biggest Week-over-Week Volume Spikes (Per Person)
spikes = spark.sql(f"""
    WITH weekly AS (
        SELECT
            person_id,
            period,
            emails_sent + emails_received AS total_volume,
            LAG(emails_sent + emails_received) OVER (PARTITION BY person_id ORDER BY period) AS prev_volume
        FROM {activity_table}
    )
    SELECT
        w.person_id,
        p.name_normalized AS display_name,
        w.period,
        w.total_volume,
        w.prev_volume,
        w.total_volume - COALESCE(w.prev_volume, 0) AS volume_change,
        CASE WHEN COALESCE(w.prev_volume, 0) > 0
            THEN ROUND(w.total_volume * 1.0 / w.prev_volume, 1)
            ELSE NULL END AS multiplier
    FROM weekly w
    LEFT JOIN {participants_table} p ON w.person_id = p.email_address
    WHERE w.prev_volume IS NOT NULL AND w.prev_volume > 0
      AND w.total_volume >= 20
      AND w.total_volume >= w.prev_volume * 3
    ORDER BY volume_change DESC
    LIMIT 30
""")

display(spikes)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Keyword Sweep
# MAGIC
# MAGIC Search for investigative keywords in email subject and body.
# MAGIC These keywords are signals of potential document destruction, secrecy,
# MAGIC or awareness of wrongdoing.

# COMMAND ----------

# DBTITLE 1,Keyword Frequency by Month
investigative_keywords = [
    "shred", "delete", "destroy", "off the record",
    "confidential", "personal", "attorney", "privilege",
    "sec", "investigation", "subpoena",
]

keyword_conditions = " OR ".join(
    f"(LOWER(subject) LIKE '%{kw}%' OR LOWER(body) LIKE '%{kw}%')" for kw in investigative_keywords
)

keyword_hits = spark.sql(f"""
    SELECT
        DATE_TRUNC('month', date) AS month,
        COUNT(*) AS hit_count
    FROM {emails_table}
    WHERE date IS NOT NULL
      AND ({keyword_conditions})
    GROUP BY 1
    ORDER BY 1
""").toPandas()

if len(keyword_hits) > 0:
    keyword_hits["month"] = pd.to_datetime(keyword_hits["month"])

    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.bar(keyword_hits["month"], keyword_hits["hit_count"], width=25, color="#e67e22", alpha=0.85)

    for date_str, label in key_dates.items():
        dt = pd.Timestamp(date_str)
        if keyword_hits["month"].min() <= dt <= keyword_hits["month"].max():
            ax.axvline(dt, color="#e74c3c", linestyle="--", alpha=0.7, linewidth=1)

    ax.set_xlabel("Month")
    ax.set_ylabel("Emails Matching Keywords")
    ax.set_title(f"Investigative Keyword Hits by Month ({', '.join(investigative_keywords[:5])}...)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.show()

# COMMAND ----------

# DBTITLE 1,Per-Keyword Breakdown
keyword_breakdown_rows = []
for kw in investigative_keywords:
    count_df = spark.sql(f"""
        SELECT COUNT(*) AS cnt FROM {emails_table}
        WHERE LOWER(subject) LIKE '%{kw}%' OR LOWER(body) LIKE '%{kw}%'
    """).collect()
    keyword_breakdown_rows.append({"keyword": kw, "email_count": count_df[0]["cnt"]})

kw_df = pd.DataFrame(keyword_breakdown_rows).sort_values("email_count", ascending=False)

fig, ax = plt.subplots(figsize=(10, 6))
bars = ax.barh(kw_df["keyword"], kw_df["email_count"], color="#9b59b6", alpha=0.85)
ax.set_xlabel("Number of Emails")
ax.set_title("Investigative Keywords — Email Frequency")
ax.invert_yaxis()
plt.tight_layout()
plt.show()

display(spark.createDataFrame(kw_df))

# COMMAND ----------

# DBTITLE 1,Sample Emails with "shred" or "destroy"
sample_destruction = spark.sql(f"""
    SELECT date, sender, subject, SUBSTR(body, 1, 300) AS body_preview
    FROM {emails_table}
    WHERE (LOWER(subject) LIKE '%shred%' OR LOWER(body) LIKE '%shred%'
           OR LOWER(subject) LIKE '%destroy%' OR LOWER(body) LIKE '%destroy%')
      AND date IS NOT NULL
    ORDER BY date
    LIMIT 20
""")

display(sample_destruction)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary
# MAGIC
# MAGIC | Analysis | What It Reveals |
# MAGIC |---|---|
# MAGIC | Self-Email Detection | Data exfiltration — forwarding corporate email to personal accounts |
# MAGIC | External Communication | Information flow outside Enron — external advisors, media, regulators |
# MAGIC | Temporal Volume | Crisis-period communication surges; correlation with key events |
# MAGIC | BCC Patterns | Covert information sharing; selective disclosure |
# MAGIC | After-Hours / Weekend | Pressure indicators; off-hours activity around sensitive periods |
# MAGIC | Communication Spikes | Sudden relationship intensity changes — what triggered the change? |
# MAGIC | Keyword Sweep | Document destruction, legal awareness, evidence of knowledge |
