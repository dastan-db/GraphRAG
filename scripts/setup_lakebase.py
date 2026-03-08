"""One-time setup for Lakebase Autoscaling backend.

Provisions the Lakebase project, creates PostgreSQL tables matching the Delta
schema, loads initial data from Delta via SQL warehouse, and creates indexes.

Usage:
    python scripts/setup_lakebase.py                 # full setup (project + tables + data + indexes)
    python scripts/setup_lakebase.py --refresh       # reload data from Delta into existing tables
    python scripts/setup_lakebase.py --indexes-only  # just create/recreate indexes
    python scripts/setup_lakebase.py --teardown      # delete project
"""

import argparse
import logging
import time

import psycopg
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Project, ProjectSpec
from databricks.sdk.service.sql import StatementState

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT_ID = "graphrag"
PROJECT_DISPLAY_NAME = "GraphRAG Knowledge Graph"
PG_VERSION = "17"

CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_bible"

TABLE_SCHEMAS = {
    "entities": {
        "source": f"{CATALOG}.{SCHEMA}.entities",
        "ddl": """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT,
                description TEXT,
                first_mention_book TEXT,
                first_mention_chapter INTEGER
            )
        """,
        "columns": "entity_id, name, entity_type, description, first_mention_book, first_mention_chapter",
    },
    "relationships": {
        "source": f"{CATALOG}.{SCHEMA}.relationships",
        "ddl": """
            CREATE TABLE IF NOT EXISTS relationships (
                source_entity TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                relationship_type TEXT,
                description TEXT,
                book TEXT,
                chapter INTEGER,
                PRIMARY KEY (source_entity, target_entity, book, chapter)
            )
        """,
        "columns": "source_entity, target_entity, relationship_type, description, book, chapter",
    },
    "entity_analytics": {
        "source": f"{CATALOG}.{SCHEMA}.entity_analytics",
        "ddl": """
            CREATE TABLE IF NOT EXISTS entity_analytics (
                entity_id TEXT PRIMARY KEY,
                name TEXT,
                entity_type TEXT,
                testament TEXT,
                pagerank DOUBLE PRECISION,
                in_degree INTEGER,
                out_degree INTEGER,
                total_degree INTEGER,
                cross_testament_connections INTEGER
            )
        """,
        "columns": "entity_id, name, entity_type, testament, pagerank, in_degree, out_degree, total_degree, cross_testament_connections",
    },
    "entity_mentions": {
        "source": f"{CATALOG}.{SCHEMA}.entity_mentions",
        "ddl": """
            CREATE TABLE IF NOT EXISTS entity_mentions (
                entity_id TEXT NOT NULL,
                book TEXT NOT NULL,
                chapter INTEGER NOT NULL,
                verse_number INTEGER NOT NULL,
                PRIMARY KEY (entity_id, book, chapter, verse_number)
            )
        """,
        "columns": "entity_id, book, chapter, verse_number",
    },
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_entities_name_lower ON entities (LOWER(name))",
    "CREATE INDEX IF NOT EXISTS idx_rels_source ON relationships (source_entity)",
    "CREATE INDEX IF NOT EXISTS idx_rels_target ON relationships (target_entity)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_pagerank ON entity_analytics (pagerank DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_cross_testament ON entity_analytics (cross_testament_connections DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_entity_id ON entity_mentions (entity_id)",
]


def get_endpoint_name():
    return f"projects/{PROJECT_ID}/branches/production/endpoints/primary"


def _pg_connect(w: WorkspaceClient):
    """Return a psycopg connection to the Lakebase project."""
    endpoint_name = get_endpoint_name()
    endpoint = w.postgres.get_endpoint(name=endpoint_name)
    host = endpoint.status.hosts.host
    cred = w.postgres.generate_database_credential(endpoint=endpoint_name)
    username = w.current_user.me().user_name

    return psycopg.connect(
        host=host,
        dbname="databricks_postgres",
        user=username,
        password=cred.token,
        sslmode="require",
    )


def create_project(w: WorkspaceClient):
    log.info("Creating Lakebase Autoscaling project '%s' ...", PROJECT_ID)
    try:
        operation = w.postgres.create_project(
            project=Project(
                spec=ProjectSpec(
                    display_name=PROJECT_DISPLAY_NAME,
                    pg_version=PG_VERSION,
                )
            ),
            project_id=PROJECT_ID,
        )
        result = operation.wait()
        log.info("Project created: %s", result.name)
    except Exception as e:
        if "already exists" in str(e).lower():
            log.info("Project '%s' already exists — skipping creation", PROJECT_ID)
        else:
            raise


def create_tables(w: WorkspaceClient):
    """Create PostgreSQL tables in Lakebase matching the Delta schema."""
    log.info("Creating PostgreSQL tables ...")
    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, spec in TABLE_SCHEMAS.items():
                log.info("  Creating table '%s' ...", name)
                cur.execute(spec["ddl"])
    log.info("Tables created")


def load_data(w: WorkspaceClient):
    """Load data from Delta tables into Lakebase via SQL warehouse + psycopg COPY."""
    warehouse_list = list(w.warehouses.list())
    if not warehouse_list:
        log.error("No SQL warehouses found — cannot load data")
        return
    warehouse_id = warehouse_list[0].id

    with _pg_connect(w) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            for name, spec in TABLE_SCHEMAS.items():
                log.info("  Loading '%s' from %s ...", name, spec["source"])

                cur.execute(f"DELETE FROM {name}")

                from databricks.sdk.service.sql import Disposition, Format

                resp = w.statement_execution.execute_statement(
                    warehouse_id=warehouse_id,
                    statement=f"SELECT {spec['columns']} FROM {spec['source']}",
                    wait_timeout="50s",
                    disposition=Disposition.INLINE,
                    format=Format.JSON_ARRAY,
                )

                if resp.status and resp.status.state == StatementState.FAILED:
                    log.warning("  Query failed for %s: %s — skipping", name, resp.status.error)
                    conn.rollback()
                    continue

                if not resp.result or not resp.result.data_array:
                    log.warning("  No data returned for %s — skipping", name)
                    conn.rollback()
                    continue

                rows = resp.result.data_array
                col_count = len(spec["columns"].split(", "))
                placeholders = ", ".join(["%s"] * col_count)
                insert_sql = f"INSERT INTO {name} ({spec['columns']}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

                for row in rows:
                    coerced = []
                    for val in row:
                        if val is None or val == "null":
                            coerced.append(None)
                        else:
                            coerced.append(val)
                    cur.execute(insert_sql, coerced)

                conn.commit()
                log.info("  Loaded %d rows into '%s'", len(rows), name)

    log.info("Data load complete")


def create_indexes(w: WorkspaceClient):
    """Create PostgreSQL indexes for query performance."""
    log.info("Creating indexes ...")
    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for idx_sql in INDEXES:
                log.info("  %s", idx_sql)
                try:
                    cur.execute(idx_sql)
                except Exception as e:
                    log.warning("  Index creation failed: %s", e)
    log.info("Indexes created")


def teardown(w: WorkspaceClient):
    log.info("Tearing down Lakebase project '%s' ...", PROJECT_ID)
    try:
        w.postgres.delete_project(name=f"projects/{PROJECT_ID}")
        log.info("Project deleted")
    except Exception as e:
        log.error("Teardown failed: %s", e)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Reload data from Delta")
    parser.add_argument("--indexes-only", action="store_true", help="Only create indexes")
    parser.add_argument("--teardown", action="store_true", help="Delete project")
    args = parser.parse_args()

    w = WorkspaceClient()

    if args.teardown:
        teardown(w)
        return

    if args.indexes_only:
        create_indexes(w)
        return

    if args.refresh:
        load_data(w)
        create_indexes(w)
        return

    create_project(w)
    log.info("Waiting for project endpoint to be ready ...")
    time.sleep(10)
    create_tables(w)
    load_data(w)
    create_indexes(w)

    log.info("Lakebase setup complete")
    log.info("  Project: %s", PROJECT_ID)
    log.info("  Endpoint: %s", get_endpoint_name())
    log.info("  Tables: %s", ", ".join(TABLE_SCHEMAS.keys()))


if __name__ == "__main__":
    main()
