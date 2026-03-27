# Databricks notebook source
# MAGIC %md
# MAGIC # 11 — Enron ABAC Demo
# MAGIC
# MAGIC Side-by-side demonstration of how Unity Catalog attribute-based access
# MAGIC controls cascade through the knowledge graph.  The same graph queries
# MAGIC produce dramatically different results depending on the caller's access
# MAGIC tier.
# MAGIC
# MAGIC **Prerequisites:** Notebooks 06, 07, and 10 (ABAC setup) must have been
# MAGIC run first.

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Imports
import pyspark.sql.functions as F
from pyspark.sql.types import StringType

# COMMAND ----------

# MAGIC %md
# MAGIC ## Section 1: Graph Metrics by Tier
# MAGIC
# MAGIC To demonstrate the cascading impact without switching users, we simulate
# MAGIC each tier by querying the base tables with an explicit sensitivity filter
# MAGIC that mirrors what the UC row filter would enforce.

# COMMAND ----------

# DBTITLE 1,Tier Simulation Helper
def simulate_tier(tier_name: str):
    """Return entity/relationship/path counts for a given access tier.

    Simulates the UC row filter by applying sensitivity IN (...) directly.
    In production, the row filter handles this transparently.
    """
    allowed = config['enron_sensitivity_tiers'][tier_name]
    allowed_sql = ", ".join(f"'{s}'" for s in allowed)

    visible_emails = spark.sql(f"""
        SELECT COUNT(*) AS cnt
        FROM {config['enron_emails_table']}
        WHERE sensitivity IN ({allowed_sql})
    """).collect()[0]['cnt']

    visible_entities = spark.sql(f"""
        SELECT COUNT(DISTINCT ent.entity_id) AS cnt
        FROM {config['enron_entities_table']} ent
        WHERE EXISTS (
            SELECT 1
            FROM {config['enron_entity_mentions_table']} em
            JOIN {config['enron_emails_table']} e ON em.message_id = e.message_id
            WHERE em.entity_id = ent.entity_id
              AND e.sensitivity IN ({allowed_sql})
        )
    """).collect()[0]['cnt']

    visible_relationships = spark.sql(f"""
        WITH visible_ent AS (
            SELECT DISTINCT ent.entity_id
            FROM {config['enron_entities_table']} ent
            WHERE EXISTS (
                SELECT 1
                FROM {config['enron_entity_mentions_table']} em
                JOIN {config['enron_emails_table']} e ON em.message_id = e.message_id
                WHERE em.entity_id = ent.entity_id
                  AND e.sensitivity IN ({allowed_sql})
            )
        )
        SELECT COUNT(*) AS cnt
        FROM {config['enron_relationships_table']} r
        WHERE EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = r.source_entity)
          AND EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = r.target_entity)
    """).collect()[0]['cnt']

    visible_paths = spark.sql(f"""
        WITH visible_ent AS (
            SELECT DISTINCT ent.entity_id
            FROM {config['enron_entities_table']} ent
            WHERE EXISTS (
                SELECT 1
                FROM {config['enron_entity_mentions_table']} em
                JOIN {config['enron_emails_table']} e ON em.message_id = e.message_id
                WHERE em.entity_id = ent.entity_id
                  AND e.sensitivity IN ({allowed_sql})
            )
        )
        SELECT COUNT(*) AS cnt
        FROM {config['enron_entity_paths_table']} p
        WHERE EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = p.source_id)
          AND EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = p.target_id)
    """).collect()[0]['cnt']

    return {
        'tier': tier_name,
        'emails': visible_emails,
        'entities': visible_entities,
        'relationships': visible_relationships,
        'paths': visible_paths,
    }

# COMMAND ----------

# DBTITLE 1,Compute and Display Tier Comparison
from pyspark.sql.types import StructType, StructField, StringType, LongType

total_emails = spark.table(config['enron_emails_table']).count()
total_entities = spark.table(config['enron_entities_table']).count()
total_rels = spark.table(config['enron_relationships_table']).count()
total_paths = spark.table(config['enron_entity_paths_table']).count()

tier_results = []
for tier_name in ['legal_team', 'executive_team', 'analyst_team']:
    result = simulate_tier(tier_name)
    result['emails_pct'] = round(100 * result['emails'] / max(total_emails, 1), 1)
    result['entities_pct'] = round(100 * result['entities'] / max(total_entities, 1), 1)
    result['relationships_pct'] = round(100 * result['relationships'] / max(total_rels, 1), 1)
    result['paths_pct'] = round(100 * result['paths'] / max(total_paths, 1), 1)
    tier_results.append(result)

schema = StructType([
    StructField("tier", StringType()),
    StructField("emails", LongType()),
    StructField("emails_pct", StringType()),
    StructField("entities", LongType()),
    StructField("entities_pct", StringType()),
    StructField("relationships", LongType()),
    StructField("relationships_pct", StringType()),
    StructField("paths", LongType()),
    StructField("paths_pct", StringType()),
])

display_rows = []
for r in tier_results:
    display_rows.append((
        r['tier'],
        r['emails'], f"{r['emails_pct']}%",
        r['entities'], f"{r['entities_pct']}%",
        r['relationships'], f"{r['relationships_pct']}%",
        r['paths'], f"{r['paths_pct']}%",
    ))

display(spark.createDataFrame(display_rows, schema))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Key Insight
# MAGIC
# MAGIC Restricting a fraction of source emails has a **non-linear** cascading
# MAGIC effect on the graph.  Removing 30% of emails can break 50-70% of the
# MAGIC relationship paths because graph connectivity depends on hub nodes that
# MAGIC disproportionately appear in privileged communications.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Section 2: Entity Visibility by Tier
# MAGIC
# MAGIC Which key Enron figures become invisible at lower access tiers?

# COMMAND ----------

# DBTITLE 1,Key Entity Visibility Matrix
KEY_ENTITIES = [
    'kenneth_lay', 'jeffrey_skilling', 'andrew_fastow',
    'richard_causey', 'enron', 'arthur_andersen',
    'vince_kaminski', 'louise_kitchen',
]

def entity_visible_at_tier(entity_id: str, tier_name: str) -> bool:
    allowed = config['enron_sensitivity_tiers'][tier_name]
    allowed_sql = ", ".join(f"'{s}'" for s in allowed)
    cnt = spark.sql(f"""
        SELECT COUNT(*) AS cnt
        FROM {config['enron_entity_mentions_table']} em
        JOIN {config['enron_emails_table']} e ON em.message_id = e.message_id
        WHERE em.entity_id = '{entity_id}'
          AND e.sensitivity IN ({allowed_sql})
    """).collect()[0]['cnt']
    return cnt > 0

visibility_rows = []
tiers = ['legal_team', 'executive_team', 'analyst_team']
for eid in KEY_ENTITIES:
    row = [eid]
    for tier in tiers:
        row.append('visible' if entity_visible_at_tier(eid, tier) else 'HIDDEN')
    visibility_rows.append(tuple(row))

vis_schema = StructType([
    StructField("entity", StringType()),
    StructField("legal_team", StringType()),
    StructField("executive_team", StringType()),
    StructField("analyst_team", StringType()),
])
display(spark.createDataFrame(visibility_rows, vis_schema))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Section 3: Path Breakage Analysis
# MAGIC
# MAGIC Take entity pairs that have short paths in the full graph and show
# MAGIC how those paths break or lengthen at lower tiers.

# COMMAND ----------

# DBTITLE 1,Path Breakage Comparison
ENTITY_PAIRS = [
    ('kenneth_lay', 'jeffrey_skilling'),
    ('kenneth_lay', 'andrew_fastow'),
    ('andrew_fastow', 'arthur_andersen'),
    ('vince_kaminski', 'louise_kitchen'),
    ('kenneth_lay', 'enron'),
]

def path_distance_at_tier(src: str, tgt: str, tier_name: str):
    allowed = config['enron_sensitivity_tiers'][tier_name]
    allowed_sql = ", ".join(f"'{s}'" for s in allowed)

    row = spark.sql(f"""
        WITH visible_ent AS (
            SELECT DISTINCT ent.entity_id
            FROM {config['enron_entities_table']} ent
            WHERE EXISTS (
                SELECT 1
                FROM {config['enron_entity_mentions_table']} em
                JOIN {config['enron_emails_table']} e ON em.message_id = e.message_id
                WHERE em.entity_id = ent.entity_id
                  AND e.sensitivity IN ({allowed_sql})
            )
        )
        SELECT MIN(p.distance) AS min_dist
        FROM {config['enron_entity_paths_table']} p
        WHERE p.source_id LIKE '%{src}%'
          AND p.target_id LIKE '%{tgt}%'
          AND EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = p.source_id)
          AND EXISTS (SELECT 1 FROM visible_ent v WHERE v.entity_id = p.target_id)
    """).collect()[0]
    return row['min_dist']

path_rows = []
for src, tgt in ENTITY_PAIRS:
    row = [f"{src} -> {tgt}"]
    for tier in tiers:
        dist = path_distance_at_tier(src, tgt, tier)
        row.append(str(int(dist)) if dist is not None else 'BROKEN')
    path_rows.append(tuple(row))

path_schema = StructType([
    StructField("entity_pair", StringType()),
    StructField("legal_team", StringType()),
    StructField("executive_team", StringType()),
    StructField("analyst_team", StringType()),
])
display(spark.createDataFrame(path_rows, path_schema))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Section 4: Agent Response Comparison
# MAGIC
# MAGIC Run the same queries through the Enron agent at each tier.  This
# MAGIC section uses the ABAC-aware tools from `src/agent/tools.py`.

# COMMAND ----------

# DBTITLE 1,Run Agent Query at Each Tier
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), '..', 'src'))
sys.path.insert(0, os.path.join(os.getcwd(), '..', 'src', 'agent'))

from agent.tools import build_abac_tools

DEMO_QUERIES = [
    "Who did Kenneth Lay communicate with most frequently?",
    "What is Andrew Fastow's role and who are his key connections?",
    "Trace the path between Kenneth Lay and Arthur Andersen.",
]

for query in DEMO_QUERIES:
    print(f"\n{'='*80}")
    print(f"QUERY: {query}")
    print('='*80)
    for tier in ['legal_team', 'executive_team', 'analyst_team']:
        tools = build_abac_tools(tier)
        find_entity_tool = tools[0]
        result = find_entity_tool.invoke(query.split()[-1].rstrip('?.'))
        print(f"\n--- {tier} ---")
        print(result[:500])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Section 5: Compliance Verification
# MAGIC
# MAGIC Demonstrate that the UC row filter enforces access at the SQL engine
# MAGIC level — not in application code.  Even a direct query against the base
# MAGIC `emails` table returns only rows the user is permitted to see.

# COMMAND ----------

# DBTITLE 1,Direct Query Test (run as different users to see different results)
print("Direct query against base emails table:")
print("(Results are filtered by UC row filter based on your group membership)\n")

result_df = spark.sql(f"""
    SELECT sensitivity, COUNT(*) AS cnt
    FROM {config['enron_emails_table']}
    GROUP BY sensitivity
    ORDER BY sensitivity
""")
display(result_df)

print("\nIf you see only 'general' rows, you are in the analyst_team group.")
print("If you see 'general' + 'executive_confidential', you are in executive_team.")
print("If you see all three tiers, you are in legal_team (or a workspace admin).")

# COMMAND ----------

# DBTITLE 1,Attempt to Read Privileged Email (compliance test)
privileged_attempt = spark.sql(f"""
    SELECT message_id, sender, subject, sensitivity
    FROM {config['enron_emails_table']}
    WHERE sensitivity = 'attorney_client_privileged'
    LIMIT 5
""")

cnt = privileged_attempt.count()
if cnt == 0:
    print("ACCESS DENIED: No attorney-client privileged emails visible.")
    print("The UC row filter correctly blocked access for your tier.")
else:
    print(f"ACCESS GRANTED: {cnt} privileged emails visible (legal_team tier).")

display(privileged_attempt)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC **Summary:** Unity Catalog row filters enforce access control at the SQL
# MAGIC engine level.  When applied to source documents, the restrictions cascade
# MAGIC automatically through ABAC views into the knowledge graph — entities
# MAGIC disappear, relationships thin out, and paths break.  The agent code
# MAGIC requires **zero changes** to respect these policies.
