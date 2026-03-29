# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Delta → Lakebase Autoscaling sync
# MAGIC
# MAGIC Batch-sync Enron Unity Catalog Delta tables into a **Lakebase Autoscaling** PostgreSQL schema for OLTP-style serving (MCP, apps).
# MAGIC
# MAGIC **Prerequisites**
# MAGIC - Lakebase project with primary endpoint (see `scripts/setup_lakebase.py` or workspace Lakebase UI).
# MAGIC - Cluster JDBC PostgreSQL driver (DBR ships `org.postgresql.Driver`).
# MAGIC - OAuth tokens expire ~1h — re-run if sync is long.
# MAGIC
# MAGIC **Widget** `LAKEBASE_ENDPOINT` defaults to `projects/graphrag/branches/production/endpoints/primary`.

# COMMAND ----------

# DBTITLE 1,Optional: psycopg for index DDL
# MAGIC %pip install psycopg[binary] --quiet

# COMMAND ----------

# DBTITLE 1,Load configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Widgets and imports
import traceback

from databricks.sdk import WorkspaceClient

try:
    dbutils.widgets.text(
        "LAKEBASE_ENDPOINT",
        "projects/graphrag/branches/production/endpoints/primary",
        "Lakebase endpoint resource name",
    )
    dbutils.widgets.text("LAKEBASE_PG_SCHEMA", "graphrag_enron", "Target PostgreSQL schema")
except Exception:
    pass

ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
PG_SCHEMA = dbutils.widgets.get("LAKEBASE_PG_SCHEMA")

CATALOG = config["catalog"]
ENRON_SCHEMA = config["enron_schema"]

TABLES = [
    "entities",
    "relationships",
    "emails",
    "entity_analytics",
    "entity_paths",
    "entity_mentions",
    "entity_aliases",
    "communication_dyads",
    "person_activity",
    "participants",
    "org_hierarchy",
    "investigation_timeline",
    "threads",
    "person_identity",
    "ontology_registry",
    "corpus_coverage",
    "extraction_provenance",
    "email_classification",
    "data_quality_report",
    "person_role_timeline",
    "topic_taxonomy",
    "pipeline_lineage",
    "entity_resolution_audit",
]

# COMMAND ----------

# DBTITLE 1,Connect via Databricks SDK (Lakebase postgres API)
w = WorkspaceClient()
endpoint = w.postgres.get_endpoint(name=ENDPOINT)
host = endpoint.status.hosts.host
cred = w.postgres.generate_database_credential(endpoint=ENDPOINT)
pg_user = w.current_user.me().user_name
pg_password = cred.token

jdbc_url = f"jdbc:postgresql://{host}:5432/databricks_postgres?sslmode=require"

print(f"Lakebase host: {host}")
print(f"JDBC (redacted): jdbc:postgresql://{host}:5432/databricks_postgres?sslmode=require")

# COMMAND ----------

# DBTITLE 1,Ensure PostgreSQL schema exists
import psycopg

conn_str = (
    f"host={host} dbname=databricks_postgres user={pg_user} "
    f"password={pg_password} sslmode=require"
)
with psycopg.connect(conn_str) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{PG_SCHEMA}"')
print(f"Schema ready: {PG_SCHEMA}")

# COMMAND ----------

# DBTITLE 1,Sync each table (Delta → JDBC overwrite)
write_opts = {
    "url": jdbc_url,
    "user": pg_user,
    "password": pg_password,
    "driver": "org.postgresql.Driver",
    "stringtype": "unspecified",
}

for t in TABLES:
    full_delta = f"{CATALOG}.{ENRON_SCHEMA}.{t}"
    try:
        exists = spark.catalog.tableExists(full_delta)
    except Exception:
        exists = False
    if not exists:
        print(f"[SKIP] {full_delta} — table missing")
        continue
    try:
        df = spark.table(full_delta)
        n = df.count()
        (
            df.write.format("jdbc")
            .mode("overwrite")
            .option("dbtable", f"{PG_SCHEMA}.{t}")
            .options(**write_opts)
            .save()
        )
        print(f"[OK] {full_delta} → {PG_SCHEMA}.{t}  rows={n:,}")
    except Exception as e:
        print(f"[FAIL] {full_delta}: {e}")
        traceback.print_exc()

# COMMAND ----------

# DBTITLE 1,Indexes on key columns (IF NOT EXISTS)
INDEX_SPECS = [
    ("entities", "entity_id", "entity_id"),
    ("entities", "name", "name"),
    ("entity_mentions", "entity_id", "entity_id"),
    ("entity_aliases", "alias_id", "alias_id"),
    ("entity_aliases", "entity_id", "entity_id_aliases"),
    ("emails", "message_id", "message_id"),
    ("emails", "thread_id", "thread_id_emails"),
    ("threads", "thread_id", "thread_id_threads"),
    ("participants", "email_address", "email_address"),
    ("person_identity", "email_address", "email_address_pi"),
    ("communication_dyads", "person_a", "person_a"),
    ("communication_dyads", "person_b", "person_b"),
    ("person_activity", "person_id", "person_id"),
]

with psycopg.connect(conn_str) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        for table, col, suffix in INDEX_SPECS:
            idx_name = f"idx_{table}_{suffix}"[:63]
            stmt = f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{PG_SCHEMA}"."{table}" ("{col}")'
            try:
                cur.execute(stmt)
                print(f"[INDEX OK] {table}.{col}")
            except Exception as e:
                print(f"[INDEX SKIP] {table}.{col} — {e}")

print("Lakebase Enron sync + indexes complete.")
