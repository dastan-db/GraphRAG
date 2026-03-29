# Databricks notebook source
# MAGIC %md
# MAGIC # 07e — Enron Person Identity
# MAGIC
# MAGIC Build **`person_identity`** — one row per Person entity with canonical name,
# MAGIC known email addresses (from `participants`), alias slugs (`entity_aliases`),
# MAGIC and provenance (`custodian` vs `ai` vs `email_header`).
# MAGIC
# MAGIC **Output table:** `{catalog}.{enron_schema}.person_identity`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Extraction Utilities (slugify)
# MAGIC %run ../src/extraction/extraction

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F
from pyspark.sql.types import StringType

slugify_udf = F.udf(slugify, StringType())

# COMMAND ----------

# DBTITLE 1,Custodian hardcoded alias pairs (must match 07_Enron_Build_Knowledge_Graph)
CUSTODIAN_FIXUPS = {
    "kenneth_lay": ["ken_lay", "dr_ken_lay", "dr_kenneth_lay", "kenny_lay",
                    "kenneth_l_lay", "k_lay", "kenneth_lay_enron_com"],
    "jeff_skilling": ["jeffrey_skilling", "jeffrey_k_skilling", "j_skilling",
                      "jeff_skilling_enron_com", "jeffrey_skilling_enron_com"],
    "andrew_fastow": ["andy_fastow", "andrew_s_fastow", "a_fastow",
                      "andrew_fastow_enron_com"],
    "david_delainey": ["dave_delainey", "d_delainey", "david_w_delainey"],
    "jeff_dasovich": ["jeffrey_dasovich", "j_dasovich"],
    "vince_kaminski": ["vincent_kaminski", "wincenty_kaminski", "v_kaminski",
                       "vince_j_kaminski"],
    "louise_kitchen": ["l_kitchen"],
    "sara_shackleton": ["s_shackleton", "sara_shackleton_enron_com"],
    "chris_germany": ["christopher_germany", "c_germany"],
    "eric_bass": ["e_bass"],
    "phillip_allen": ["phil_allen", "p_allen", "phillip_k_allen"],
    "john_arnold": ["j_arnold"],
    "sally_beck": ["s_beck", "sally_beck_enron_com"],
    "lynn_blair": ["l_blair"],
    "larry_campbell": ["l_campbell", "lawrence_campbell"],
    "sherron_watkins": ["s_watkins", "sherron_watkins_enron_com"],
    "richard_causey": ["rick_causey", "r_causey"],
    "rick_buy": ["r_buy", "richard_buy"],
    "tim_belden": ["t_belden", "timothy_belden"],
    "michael_kopper": ["m_kopper", "mike_kopper"],
    "greg_whalley": ["g_whalley", "gregory_whalley"],
    "cliff_baxter": ["c_baxter", "j_clifford_baxter"],
    "kenneth_rice": ["ken_rice", "k_rice"],
    "mark_frevert": ["m_frevert"],
    "rebecca_mark": ["r_mark", "rebecca_mark_jusbasche"],
    "enron_corp": ["enron", "enron_corporation", "enron_inc", "enron_company"],
    "enron_energy_services": ["ees"],
    "enron_broadband_services": ["ebs", "enron_broadband"],
    "federal_energy_regulatory_commission": ["ferc"],
    "california_public_utilities_commission": ["cpuc"],
    "pacific_gas_and_electric": ["pg_e", "pge", "pacific_gas_electric"],
}

custodian_rows = []
for canonical, variants in CUSTODIAN_FIXUPS.items():
    for v in variants:
        if v != canonical:
            custodian_rows.append((v, canonical))

# COMMAND ----------

# DBTITLE 1,Build person_identity
enron_schema = config["enron_schema"]
entities_table = config["enron_entities_table"]
aliases_table = f"{config['catalog']}.{enron_schema}.entity_aliases"
participants_table = config["enron_participants_table"]
out_table = config["enron_person_identity_table"]

entities_person = spark.table(entities_table).filter(F.col("entity_type") == "Person")

alias_person = (
    spark.table(aliases_table)
    .join(entities_person.select(F.col("entity_id").alias("canon_person")), F.col("canonical_id") == F.col("canon_person"), "inner")
    .select("alias_id", "canonical_id")
    .distinct()
)

custodian_bc = spark.createDataFrame(custodian_rows, ["alias_id", "canonical_id"]).distinct()

alias_tagged = (
    alias_person.join(custodian_bc.withColumn("_cust", F.lit(1)), on=["alias_id", "canonical_id"], how="left")
    .withColumn("alias_source", F.when(F.col("_cust").isNotNull(), F.lit("custodian")).otherwise(F.lit("ai")))
    .drop("_cust")
)

alias_priority = alias_tagged.groupBy("canonical_id").agg(
    F.max(F.when(F.col("alias_source") == "custodian", F.lit(1)).otherwise(F.lit(0))).alias("has_custodian"),
    F.max(F.when(F.col("alias_source") == "ai", F.lit(1)).otherwise(F.lit(0))).alias("has_ai"),
)

aliases_agg = alias_tagged.groupBy("canonical_id").agg(
    F.array_sort(F.collect_set("alias_id")).alias("aliases"),
)

slug_map = (
    entities_person.select(F.col("entity_id").alias("slug"), F.col("entity_id"))
    .unionByName(alias_person.select(F.col("alias_id").alias("slug"), F.col("canonical_id").alias("entity_id")))
    .distinct()
)

parts = spark.table(participants_table)
parts_slugs = (
    parts.select("email_address", slugify_udf(F.trim(F.col("name_normalized"))).alias("slug"))
    .unionByName(
        parts.select("email_address", slugify_udf(F.regexp_extract(F.col("email_address"), r"^([^@]+)", 1)).alias("slug"))
    )
    .filter(F.col("slug").isNotNull() & (F.col("slug") != ""))
)

emails_by_person = (
    parts_slugs.join(slug_map, on="slug", how="inner")
    .groupBy("entity_id")
    .agg(F.array_sort(F.collect_set("email_address")).alias("email_addresses"))
)

person_keys = entities_person.select(
    F.col("entity_id"),
    F.col("name").alias("canonical_name"),
)

joined = (
    person_keys.alias("p")
    .join(aliases_agg.alias("a"), F.col("p.entity_id") == F.col("a.canonical_id"), "left")
    .join(emails_by_person.alias("e"), F.col("p.entity_id") == F.col("e.entity_id"), "left")
    .join(alias_priority.alias("pr"), F.col("p.entity_id") == F.col("pr.canonical_id"), "left")
)

result_df = (
    joined.select(
        F.col("p.entity_id").alias("entity_id"),
        F.col("p.canonical_name").alias("canonical_name"),
        F.coalesce(F.col("e.email_addresses"), F.expr("array()")).alias("email_addresses"),
        F.coalesce(F.col("a.aliases"), F.expr("array()")).alias("aliases"),
        F.when(F.col("pr.has_custodian") == 1, F.lit("custodian"))
        .when(F.col("pr.has_ai") == 1, F.lit("ai"))
        .when(F.size(F.coalesce(F.col("e.email_addresses"), F.expr("array()"))) > 0, F.lit("email_header"))
        .otherwise(F.lit("ai"))
        .alias("source"),
    )
    .withColumn(
        "confidence",
        F.when(F.col("source") == "custodian", F.lit(1.0))
        .when(F.col("source") == "email_header", F.lit(0.7))
        .otherwise(F.lit(0.85)),
    )
)

(
    result_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(out_table)
)

n = spark.table(out_table).count()
print(f"person_identity: {n:,} rows → {out_table}")

# COMMAND ----------

# DBTITLE 1,Sample
spark.table(out_table).orderBy(F.desc(F.size("email_addresses"))).show(5, truncate=80)
