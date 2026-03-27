"""One-time setup for Lakebase Autoscaling backend.

Provisions the Lakebase project, creates PostgreSQL tables matching the Delta
schema, loads initial data from Delta via SQL warehouse, creates indexes, and
configures Row-Level Security policies for both the Bible and Enron corpora.

Usage:
    python scripts/setup_lakebase.py                 # full setup (project + tables + data + indexes + RLS)
    python scripts/setup_lakebase.py --refresh       # reload data from Delta into existing tables
    python scripts/setup_lakebase.py --indexes-only  # just create/recreate indexes
    python scripts/setup_lakebase.py --rls-only      # just create/recreate RLS policies
    python scripts/setup_lakebase.py --teardown      # delete project
    python scripts/setup_lakebase.py --enron          # include Enron corpus tables
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
BIBLE_SCHEMA = "graphrag_bible"
ENRON_SCHEMA = "graphrag_enron"

# ---------------------------------------------------------------------------
# Bible corpus tables (public schema in Postgres)
# ---------------------------------------------------------------------------

BIBLE_TABLE_SCHEMAS = {
    "entities": {
        "source": f"{CATALOG}.{BIBLE_SCHEMA}.entities",
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
        "source": f"{CATALOG}.{BIBLE_SCHEMA}.relationships",
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
        "source": f"{CATALOG}.{BIBLE_SCHEMA}.entity_analytics",
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
        "source": f"{CATALOG}.{BIBLE_SCHEMA}.entity_mentions",
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
    "verses": {
        "source": f"{CATALOG}.{BIBLE_SCHEMA}.verses",
        "ddl": """
            CREATE TABLE IF NOT EXISTS verses (
                book TEXT NOT NULL,
                chapter INTEGER NOT NULL,
                verse_number INTEGER NOT NULL,
                text TEXT,
                PRIMARY KEY (book, chapter, verse_number)
            )
        """,
        "columns": "book, chapter, verse_number, text",
    },
}

BIBLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_entities_name_lower ON entities (LOWER(name))",
    "CREATE INDEX IF NOT EXISTS idx_rels_source ON relationships (source_entity)",
    "CREATE INDEX IF NOT EXISTS idx_rels_target ON relationships (target_entity)",
    "CREATE INDEX IF NOT EXISTS idx_rels_book ON relationships (book)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_pagerank ON entity_analytics (pagerank DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_cross_testament ON entity_analytics (cross_testament_connections DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_entity_id ON entity_mentions (entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_book ON entity_mentions (book)",
    "CREATE INDEX IF NOT EXISTS idx_verses_book_ch ON verses (book, chapter)",
]

# ---------------------------------------------------------------------------
# Enron corpus tables (enron schema in Postgres)
# ---------------------------------------------------------------------------

ENRON_TABLE_SCHEMAS = {
    "enron.emails": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.emails",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.emails (
                message_id TEXT PRIMARY KEY,
                date TIMESTAMP,
                sender TEXT,
                to_recipients TEXT,
                cc_recipients TEXT,
                bcc_recipients TEXT,
                subject TEXT,
                body TEXT,
                thread_id TEXT,
                sensitivity TEXT
            )
        """,
        "columns": "message_id, date, sender, to_recipients, cc_recipients, bcc_recipients, subject, body, thread_id, sensitivity",
    },
    "enron.entities": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.entities",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.entities (
                entity_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT,
                description TEXT,
                first_mention_thread TEXT,
                first_mention_subject TEXT
            )
        """,
        "columns": "entity_id, name, entity_type, description, first_mention_thread, first_mention_subject",
    },
    "enron.relationships": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.relationships",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.relationships (
                source_entity TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                relationship_type TEXT,
                description TEXT,
                thread_id TEXT
            )
        """,
        "columns": "source_entity, target_entity, relationship_type, description, thread_id",
    },
    "enron.entity_mentions": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.entity_mentions (
                entity_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                thread_id TEXT
            )
        """,
        "columns": "entity_id, message_id, thread_id",
    },
    "enron.entity_analytics": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.entity_analytics (
                entity_id TEXT PRIMARY KEY,
                name TEXT,
                entity_type TEXT,
                pagerank DOUBLE PRECISION,
                in_degree INTEGER,
                out_degree INTEGER,
                total_degree INTEGER
            )
        """,
        "columns": "entity_id, name, entity_type, pagerank, in_degree, out_degree, total_degree",
    },
}

ENRON_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_enron_emails_sensitivity ON enron.emails (sensitivity)",
    "CREATE INDEX IF NOT EXISTS idx_enron_emails_sender ON enron.emails (sender)",
    "CREATE INDEX IF NOT EXISTS idx_enron_emails_thread ON enron.emails (thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_enron_entities_name ON enron.entities (LOWER(name))",
    "CREATE INDEX IF NOT EXISTS idx_enron_rels_source ON enron.relationships (source_entity)",
    "CREATE INDEX IF NOT EXISTS idx_enron_rels_target ON enron.relationships (target_entity)",
    "CREATE INDEX IF NOT EXISTS idx_enron_mentions_entity ON enron.entity_mentions (entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_enron_mentions_message ON enron.entity_mentions (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_enron_analytics_pagerank ON enron.entity_analytics (pagerank DESC)",
]

# ---------------------------------------------------------------------------
# RLS policies
# ---------------------------------------------------------------------------

BIBLE_RLS_POLICIES = [
    # --- relationships: direct book filtering ---
    "ALTER TABLE relationships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE relationships FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS bible_book_access ON relationships
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- entity_mentions: direct book filtering ---
    "ALTER TABLE entity_mentions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entity_mentions FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS bible_book_access ON entity_mentions
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- verses: direct book filtering ---
    "ALTER TABLE verses ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE verses FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS bible_book_access ON verses
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- entities: cascades through entity_mentions ---
    "ALTER TABLE entities ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entities FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS bible_book_access ON entities
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR EXISTS (
                SELECT 1 FROM entity_mentions em
                WHERE em.entity_id = entities.entity_id
                  AND em.book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
            )
        )
    """,

    # --- entity_analytics: cascades through entity_mentions ---
    "ALTER TABLE entity_analytics ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entity_analytics FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS bible_book_access ON entity_analytics
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR EXISTS (
                SELECT 1 FROM entity_mentions em
                WHERE em.entity_id = entity_analytics.entity_id
                  AND em.book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
            )
        )
    """,
]

ENRON_RLS_POLICIES = [
    # --- emails: tier-based sensitivity filtering ---
    "ALTER TABLE enron.emails ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE enron.emails FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS enron_tier_access ON enron.emails
        USING (
            COALESCE(NULLIF(current_setting('app.user_tier', true), ''), NULL) IS NULL
            OR CASE current_setting('app.user_tier', true)
                WHEN 'legal_team' THEN TRUE
                WHEN 'executive_team' THEN sensitivity IN ('general', 'executive_confidential')
                WHEN 'analyst_team' THEN sensitivity = 'general'
                ELSE FALSE
            END
        )
    """,

    # --- entity_mentions: cascades through emails ---
    "ALTER TABLE enron.entity_mentions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE enron.entity_mentions FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS enron_tier_access ON enron.entity_mentions
        USING (
            COALESCE(NULLIF(current_setting('app.user_tier', true), ''), NULL) IS NULL
            OR EXISTS (
                SELECT 1 FROM enron.emails e
                WHERE e.message_id = enron.entity_mentions.message_id
            )
        )
    """,

    # --- entities: cascades through entity_mentions ---
    "ALTER TABLE enron.entities ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE enron.entities FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS enron_tier_access ON enron.entities
        USING (
            COALESCE(NULLIF(current_setting('app.user_tier', true), ''), NULL) IS NULL
            OR EXISTS (
                SELECT 1 FROM enron.entity_mentions em
                WHERE em.entity_id = enron.entities.entity_id
            )
        )
    """,

    # --- relationships: both endpoints must be visible ---
    "ALTER TABLE enron.relationships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE enron.relationships FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS enron_tier_access ON enron.relationships
        USING (
            COALESCE(NULLIF(current_setting('app.user_tier', true), ''), NULL) IS NULL
            OR (
                EXISTS (
                    SELECT 1 FROM enron.entities e
                    WHERE e.entity_id = enron.relationships.source_entity
                )
                AND EXISTS (
                    SELECT 1 FROM enron.entities e
                    WHERE e.entity_id = enron.relationships.target_entity
                )
            )
        )
    """,

    # --- entity_analytics: entity must be visible ---
    "ALTER TABLE enron.entity_analytics ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE enron.entity_analytics FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY IF NOT EXISTS enron_tier_access ON enron.entity_analytics
        USING (
            COALESCE(NULLIF(current_setting('app.user_tier', true), ''), NULL) IS NULL
            OR EXISTS (
                SELECT 1 FROM enron.entities e
                WHERE e.entity_id = enron.entity_analytics.entity_id
            )
        )
    """,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Setup functions
# ---------------------------------------------------------------------------

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


def create_tables(w: WorkspaceClient, include_enron: bool = False):
    """Create PostgreSQL tables in Lakebase matching the Delta schema."""
    log.info("Creating PostgreSQL tables ...")
    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            if include_enron:
                cur.execute("CREATE SCHEMA IF NOT EXISTS enron")

            schemas = dict(BIBLE_TABLE_SCHEMAS)
            if include_enron:
                schemas.update(ENRON_TABLE_SCHEMAS)

            for name, spec in schemas.items():
                log.info("  Creating table '%s' ...", name)
                cur.execute(spec["ddl"])
    log.info("Tables created")


def load_data(w: WorkspaceClient, include_enron: bool = False):
    """Load data from Delta tables into Lakebase via SQL warehouse + psycopg."""
    warehouse_list = list(w.warehouses.list())
    if not warehouse_list:
        log.error("No SQL warehouses found — cannot load data")
        return
    warehouse_id = warehouse_list[0].id

    schemas = dict(BIBLE_TABLE_SCHEMAS)
    if include_enron:
        schemas.update(ENRON_TABLE_SCHEMAS)

    with _pg_connect(w) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            for name, spec in schemas.items():
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


def create_indexes(w: WorkspaceClient, include_enron: bool = False):
    """Create PostgreSQL indexes for query performance."""
    log.info("Creating indexes ...")
    indexes = list(BIBLE_INDEXES)
    if include_enron:
        indexes.extend(ENRON_INDEXES)

    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for idx_sql in indexes:
                log.info("  %s", idx_sql)
                try:
                    cur.execute(idx_sql)
                except Exception as e:
                    log.warning("  Index creation failed: %s", e)
    log.info("Indexes created")


def create_rls_policies(w: WorkspaceClient, include_enron: bool = False):
    """Create Row-Level Security policies for session-variable-based access control.

    Bible tables use ``app.permitted_books`` (comma-separated book names).
    Enron tables use ``app.user_tier`` (legal_team|executive_team|analyst_team).
    When the session variable is empty or unset, all rows are visible (no filtering).
    """
    log.info("Creating RLS policies ...")
    policies = list(BIBLE_RLS_POLICIES)
    if include_enron:
        policies.extend(ENRON_RLS_POLICIES)

    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for sql in policies:
                sql = sql.strip()
                if not sql:
                    continue
                try:
                    cur.execute(sql)
                    first_line = sql.split("\n")[0].strip()[:80]
                    log.info("  OK: %s", first_line)
                except Exception as e:
                    if "already exists" in str(e).lower():
                        log.info("  (already exists) %s", sql.split("\n")[0].strip()[:60])
                    else:
                        log.warning("  RLS statement failed: %s — %s", sql[:60], e)
    log.info("RLS policies created")


def teardown(w: WorkspaceClient):
    log.info("Tearing down Lakebase project '%s' ...", PROJECT_ID)
    try:
        w.postgres.delete_project(name=f"projects/{PROJECT_ID}")
        log.info("Project deleted")
    except Exception as e:
        log.error("Teardown failed: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="Reload data from Delta")
    parser.add_argument("--indexes-only", action="store_true", help="Only create indexes")
    parser.add_argument("--rls-only", action="store_true", help="Only create RLS policies")
    parser.add_argument("--teardown", action="store_true", help="Delete project")
    parser.add_argument("--enron", action="store_true", help="Include Enron corpus tables")
    args = parser.parse_args()

    w = WorkspaceClient()

    if args.teardown:
        teardown(w)
        return

    if args.indexes_only:
        create_indexes(w, include_enron=args.enron)
        return

    if args.rls_only:
        create_rls_policies(w, include_enron=args.enron)
        return

    if args.refresh:
        load_data(w, include_enron=args.enron)
        create_indexes(w, include_enron=args.enron)
        return

    create_project(w)
    log.info("Waiting for project endpoint to be ready ...")
    time.sleep(10)
    create_tables(w, include_enron=args.enron)
    load_data(w, include_enron=args.enron)
    create_indexes(w, include_enron=args.enron)
    create_rls_policies(w, include_enron=args.enron)

    all_tables = list(BIBLE_TABLE_SCHEMAS.keys())
    if args.enron:
        all_tables.extend(ENRON_TABLE_SCHEMAS.keys())

    log.info("Lakebase setup complete")
    log.info("  Project: %s", PROJECT_ID)
    log.info("  Endpoint: %s", get_endpoint_name())
    log.info("  Tables: %s", ", ".join(all_tables))
    log.info("  RLS: Bible (app.permitted_books)%s",
             " + Enron (app.user_tier)" if args.enron else "")


if __name__ == "__main__":
    main()
