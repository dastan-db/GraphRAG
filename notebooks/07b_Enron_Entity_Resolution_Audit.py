# Databricks notebook source
# MAGIC %md
# MAGIC # 07b — Enron Entity Resolution Audit
# MAGIC
# MAGIC Quality gate: audit entity resolution for the 15 key custodians.
# MAGIC For each custodian, find all entity_id variants, check which are
# MAGIC in the `entity_aliases` table, and generate fix-up SQL for orphans.
# MAGIC
# MAGIC **Run this before building aggregation tables** — entity resolution
# MAGIC errors compound into every downstream table.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import pyspark.sql.functions as F

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Map Custodians to Expected Entity IDs

# COMMAND ----------

# DBTITLE 1,Define Expected Canonical Entities
CUSTODIAN_EXPECTED = {
    "lay-k": {"canonical": "kenneth_lay", "display": "Kenneth Lay", "variants": [
        "ken_lay", "dr_ken_lay", "dr_kenneth_lay", "kenny_lay",
        "kenneth_l_lay", "k_lay", "kenneth_lay_enron_com",
    ]},
    "skilling-j": {"canonical": "jeff_skilling", "display": "Jeff Skilling", "variants": [
        "jeffrey_skilling", "jeffrey_k_skilling", "j_skilling",
        "jeff_skilling_enron_com", "jeffrey_skilling_enron_com",
    ]},
    "fastow-a": {"canonical": "andrew_fastow", "display": "Andrew Fastow", "variants": [
        "andy_fastow", "andrew_s_fastow", "a_fastow",
        "andrew_fastow_enron_com",
    ]},
    "delainey-d": {"canonical": "david_delainey", "display": "David Delainey", "variants": [
        "dave_delainey", "d_delainey", "david_w_delainey",
    ]},
    "dasovich-j": {"canonical": "jeff_dasovich", "display": "Jeff Dasovich", "variants": [
        "jeffrey_dasovich", "j_dasovich",
    ]},
    "kaminski-v": {"canonical": "vince_kaminski", "display": "Vince Kaminski", "variants": [
        "vincent_kaminski", "wincenty_kaminski", "v_kaminski",
        "vince_j_kaminski",
    ]},
    "kitchen-l": {"canonical": "louise_kitchen", "display": "Louise Kitchen", "variants": [
        "l_kitchen",
    ]},
    "shackleton-s": {"canonical": "sara_shackleton", "display": "Sara Shackleton", "variants": [
        "s_shackleton", "sara_shackleton_enron_com",
    ]},
    "germany-c": {"canonical": "chris_germany", "display": "Chris Germany", "variants": [
        "christopher_germany", "c_germany",
    ]},
    "bass-e": {"canonical": "eric_bass", "display": "Eric Bass", "variants": [
        "e_bass",
    ]},
    "allen-p": {"canonical": "phillip_allen", "display": "Phillip Allen", "variants": [
        "phil_allen", "p_allen", "phillip_k_allen",
    ]},
    "arnold-j": {"canonical": "john_arnold", "display": "John Arnold", "variants": [
        "j_arnold",
    ]},
    "beck-s": {"canonical": "sally_beck", "display": "Sally Beck", "variants": [
        "s_beck", "sally_beck_enron_com",
    ]},
    "blair-l": {"canonical": "lynn_blair", "display": "Lynn Blair", "variants": [
        "l_blair",
    ]},
    "campbell-l": {"canonical": "larry_campbell", "display": "Larry Campbell", "variants": [
        "l_campbell", "lawrence_campbell",
    ]},
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Scan Entities Table for All Variants

# COMMAND ----------

# DBTITLE 1,Find All Entity ID Variants per Custodian
entities_df = spark.table(config['enron_entities_table'])
aliases_table = f"{config['catalog']}.{config['enron_schema']}.entity_aliases"

try:
    aliases_df = spark.table(aliases_table)
    has_aliases = True
except Exception:
    aliases_df = None
    has_aliases = False
    print(f"WARNING: {aliases_table} does not exist — all variants are orphaned")

audit_results = []

for mailbox, spec in CUSTODIAN_EXPECTED.items():
    canonical = spec["canonical"]
    display = spec["display"]
    all_patterns = [canonical] + spec["variants"]

    conditions = [F.col("entity_id").contains(p) for p in all_patterns]
    combined = conditions[0]
    for c in conditions[1:]:
        combined = combined | c

    found_ids = (
        entities_df
        .filter(combined)
        .select("entity_id", "name", "entity_type")
        .distinct()
        .collect()
    )

    found_set = {r["entity_id"] for r in found_ids}
    aliased_set = set()
    if has_aliases:
        aliased_rows = (
            aliases_df
            .filter(F.col("canonical_id") == canonical)
            .select("alias_id")
            .collect()
        )
        aliased_set = {r["alias_id"] for r in aliased_rows}

    orphans = found_set - aliased_set - {canonical}

    audit_results.append({
        "custodian": display,
        "canonical_id": canonical,
        "total_variants": len(found_set),
        "aliased": len(aliased_set),
        "orphans": len(orphans),
        "orphan_ids": sorted(orphans),
        "all_found": sorted(found_set),
    })

    status = "OK" if len(orphans) == 0 else f"NEEDS FIX ({len(orphans)} orphans)"
    print(f"  {display:25s} canonical={canonical:25s} found={len(found_set):3d}  aliased={len(aliased_set):3d}  orphans={len(orphans):3d}  [{status}]")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Generate Fix-Up SQL

# COMMAND ----------

# DBTITLE 1,Generate INSERT Statements for Missing Aliases
fix_statements = []
for entry in audit_results:
    for orphan_id in entry["orphan_ids"]:
        fix_statements.append(
            f"  ('{orphan_id}', '{entry['canonical_id']}')"
        )

if fix_statements:
    sql = (
        f"INSERT INTO {aliases_table} (alias_id, canonical_id) VALUES\n"
        + ",\n".join(fix_statements)
        + ";"
    )
    print(f"=== FIX-UP SQL ({len(fix_statements)} aliases to add) ===\n")
    print(sql)
else:
    print("All custodian entities are properly aliased — no fixes needed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Summary

# COMMAND ----------

# DBTITLE 1,Audit Summary Table
import pandas as pd

del display  # restore built-in shadowed by audit loop variable

summary_df = pd.DataFrame([
    {
        "Custodian": e["custodian"],
        "Canonical ID": e["canonical_id"],
        "Variants Found": e["total_variants"],
        "Properly Aliased": e["aliased"],
        "Orphaned": e["orphans"],
        "Status": "OK" if e["orphans"] == 0 else "NEEDS FIX",
    }
    for e in audit_results
])

display(summary_df)

total_orphans = sum(e["orphans"] for e in audit_results)
print(f"\nTotal orphaned entity IDs: {total_orphans}")
if total_orphans > 0:
    print("Run the fix-up SQL above, then re-run this notebook to verify.")
else:
    print("Entity resolution is clean for all 15 key custodians.")