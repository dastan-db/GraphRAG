"""One-time setup for Lakebase Autoscaling backend.

Provisions the Lakebase project, creates PostgreSQL tables matching the Delta
schema, loads initial data from Delta via SQL warehouse, creates indexes, and
configures Row-Level Security policies for both the Bible and Enron corpora.

Enron table **names** match ``src/runtime/enron_corpus_tables.py`` (same list as
``scripts/export_local_data.py`` for DuckDB). Materialization follows
``src/runtime/enron_corpus_load.py``: mirror Delta types (e.g. ``emails`` uses
``TEXT[]`` for recipient arrays, not CSV ``TEXT``). Tables with explicit DDL live
in ``ENRON_TABLE_SCHEMAS``; any other name in that list gets ``CREATE TABLE``
from the warehouse manifest and a full load.

Known Enron tables include ``communication_dyads``, ``person_activity``, and a
``relationships`` row shape aligned with the Unity Catalog graph (``edge_count``,
``source_threads``, …). ``migrate_enron_lakebase_schema`` adds missing columns on
older deployments; re-run ``--refresh`` after upgrading this script.

Usage:
    python scripts/setup_lakebase.py                 # full setup (project + tables + data + indexes + RLS)
    python scripts/setup_lakebase.py --refresh       # reload data from Delta into existing tables
    python scripts/setup_lakebase.py --indexes-only  # just create/recreate indexes
    python scripts/setup_lakebase.py --rls-only      # just create/recreate RLS policies
    python scripts/setup_lakebase.py --teardown      # delete project
    python scripts/setup_lakebase.py --enron          # include Enron corpus tables
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.runtime.enron_corpus_tables import ENRON_CORPUS_TABLE_NAMES
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
        "dedupe_key": "entity_id",
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
        "dedupe_key": "source_entity, target_entity, book, chapter",
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
        "dedupe_key": "entity_id",
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
        "dedupe_key": "entity_id, book, chapter, verse_number",
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
        "dedupe_key": "book, chapter, verse_number",
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
                to_recipients TEXT[],
                cc_recipients TEXT[],
                bcc_recipients TEXT[],
                subject TEXT,
                body TEXT,
                thread_id TEXT,
                sensitivity TEXT
            )
        """,
        # Same projection as Delta (no CONCAT_WS) — matches DuckDB export SELECT *.
        "columns": (
            "message_id, date, sender, to_recipients, cc_recipients, bcc_recipients, "
            "subject, body, thread_id, sensitivity"
        ),
        "dedupe_key": "message_id",
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
        "dedupe_key": "entity_id",
    },
    "enron.relationships": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.relationships",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.relationships (
                source_entity TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                description TEXT,
                source_threads TEXT[],
                edge_count BIGINT,
                first_observed TEXT,
                last_observed TEXT,
                evidence_type TEXT,
                confidence DOUBLE PRECISION,
                PRIMARY KEY (source_entity, target_entity, relationship_type)
            )
        """,
        "columns": (
            "source_entity, target_entity, relationship_type, description, "
            "source_threads, edge_count, first_observed, last_observed, evidence_type, confidence"
        ),
        "dedupe_key": "source_entity, target_entity, relationship_type",
    },
    "enron.communication_dyads": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.communication_dyads",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.communication_dyads (
                person_a TEXT NOT NULL,
                person_b TEXT NOT NULL,
                period TIMESTAMP,
                total_count BIGINT,
                to_count BIGINT,
                cc_count BIGINT,
                bcc_count BIGINT,
                PRIMARY KEY (person_a, person_b, period)
            )
        """,
        "columns": (
            "person_a, person_b, period, total_count, to_count, cc_count, bcc_count"
        ),
        "dedupe_key": "person_a, person_b, period",
    },
    "enron.person_activity": {
        "source": f"{CATALOG}.{ENRON_SCHEMA}.person_activity",
        "ddl": """
            CREATE TABLE IF NOT EXISTS enron.person_activity (
                person_id TEXT NOT NULL,
                period TIMESTAMP,
                emails_sent BIGINT,
                emails_received BIGINT,
                unique_contacts_sent BIGINT,
                bcc_emails_sent BIGINT,
                after_hours_count BIGINT,
                weekend_count BIGINT,
                PRIMARY KEY (person_id, period)
            )
        """,
        "columns": (
            "person_id, period, emails_sent, emails_received, unique_contacts_sent, "
            "bcc_emails_sent, after_hours_count, weekend_count"
        ),
        "dedupe_key": "person_id, period",
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
        "dedupe_key": "entity_id, message_id",
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
        "dedupe_key": "entity_id",
    },
    # enron.threads: loaded via _load_enron_dynamic (manifest DDL + SELECT *) so Lakebase matches
    # whatever columns exist in Delta (summary/key_topics may be absent until KG notebook runs).
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
    "CREATE INDEX IF NOT EXISTS idx_enron_dyads_a ON enron.communication_dyads (person_a)",
    "CREATE INDEX IF NOT EXISTS idx_enron_dyads_b ON enron.communication_dyads (person_b)",
    "CREATE INDEX IF NOT EXISTS idx_enron_activity_person ON enron.person_activity (person_id)",
]

# ALTERs for Lakebase DBs created before communication_dyads / person_activity / full relationships
ENRON_LAKEBASE_MIGRATIONS = [
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS source_threads TEXT[]
    """,
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS edge_count BIGINT
    """,
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS first_observed TEXT
    """,
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS last_observed TEXT
    """,
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS evidence_type TEXT
    """,
    """
    ALTER TABLE enron.relationships ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION
    """,
]

# ---------------------------------------------------------------------------
# RLS policies
# ---------------------------------------------------------------------------

BIBLE_RLS_POLICIES = [
    # --- relationships: direct book filtering ---
    "ALTER TABLE relationships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE relationships FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS bible_book_access ON relationships",
    """
    CREATE POLICY bible_book_access ON relationships
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- entity_mentions: direct book filtering ---
    "ALTER TABLE entity_mentions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entity_mentions FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS bible_book_access ON entity_mentions",
    """
    CREATE POLICY bible_book_access ON entity_mentions
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- verses: direct book filtering ---
    "ALTER TABLE verses ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE verses FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS bible_book_access ON verses",
    """
    CREATE POLICY bible_book_access ON verses
        USING (
            COALESCE(NULLIF(current_setting('app.permitted_books', true), ''), NULL) IS NULL
            OR book = ANY(string_to_array(current_setting('app.permitted_books', true), ','))
        )
    """,

    # --- entities: cascades through entity_mentions ---
    "ALTER TABLE entities ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE entities FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS bible_book_access ON entities",
    """
    CREATE POLICY bible_book_access ON entities
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
    "DROP POLICY IF EXISTS bible_book_access ON entity_analytics",
    """
    CREATE POLICY bible_book_access ON entity_analytics
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
    "DROP POLICY IF EXISTS enron_tier_access ON enron.emails",
    """
    CREATE POLICY enron_tier_access ON enron.emails
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
    "DROP POLICY IF EXISTS enron_tier_access ON enron.entity_mentions",
    """
    CREATE POLICY enron_tier_access ON enron.entity_mentions
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
    "DROP POLICY IF EXISTS enron_tier_access ON enron.entities",
    """
    CREATE POLICY enron_tier_access ON enron.entities
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
    "DROP POLICY IF EXISTS enron_tier_access ON enron.relationships",
    """
    CREATE POLICY enron_tier_access ON enron.relationships
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
    "DROP POLICY IF EXISTS enron_tier_access ON enron.entity_analytics",
    """
    CREATE POLICY enron_tier_access ON enron.entity_analytics
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


def _migrate_enron_emails_recipients_to_text_array(cur) -> None:
    """Older Lakebase stored recipients as TEXT (CSV); Delta/DuckDB use arrays — align to TEXT[]."""
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'enron' AND table_name = 'emails'
        )
        """
    )
    if not cur.fetchone()[0]:
        return
    for col in ("to_recipients", "cc_recipients", "bcc_recipients"):
        cur.execute(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'enron' AND table_name = 'emails' AND column_name = %s
            """,
            (col,),
        )
        row = cur.fetchone()
        if not row or row[0] != "text":
            continue
        cur.execute(
            f"""
            ALTER TABLE enron.emails ALTER COLUMN {col} TYPE TEXT[] USING
            CASE
                WHEN {col} IS NULL THEN NULL
                ELSE string_to_array(trim({col}::text), ',')
            END
            """
        )
        log.info("  Migrated enron.emails.%s from TEXT (CSV) to TEXT[]", col)


def migrate_enron_lakebase_schema(w: WorkspaceClient):
    """Ensure analytics tables exist; add relationship columns from older deployments."""
    log.info("Applying Enron Lakebase schema migrations ...")
    with _pg_connect(w) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS enron")
            _migrate_enron_emails_recipients_to_text_array(cur)
            for key in ("enron.communication_dyads", "enron.person_activity"):
                cur.execute(ENRON_TABLE_SCHEMAS[key]["ddl"])
                log.info("  Ensured table %s", key)
            for stmt in ENRON_LAKEBASE_MIGRATIONS:
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                    log.info("  OK: %s", stmt.split()[0:6])
                except Exception as e:
                    log.warning("  Migration skipped or failed: %s — %s", stmt[:80], e)
    log.info("Migrations applied")


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
    if include_enron:
        migrate_enron_lakebase_schema(w)
    log.info("Tables created")


def _inline_byte_limit_exceeded(err: str | None) -> bool:
    if not err:
        return False
    return "inline byte limit" in err.lower()


def _fetch_external_link_rows(link) -> list[list]:
    """Download a Statement API EXTERNAL_LINKS chunk and decode its JSON rows."""
    if not link or not getattr(link, "external_link", None):
        return []

    req = Request(link.external_link, headers=link.http_headers or {})
    try:
        with urlopen(req, timeout=60) as resp:
            payload = resp.read()
    except URLError as exc:
        log.warning("    External chunk fetch failed for chunk %s: %s", link.chunk_index, exc)
        return []

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        log.warning("    External chunk decode failed for chunk %s: %s", link.chunk_index, exc)
        return []

    if isinstance(decoded, list):
        return decoded
    if isinstance(decoded, dict):
        data = decoded.get("data_array")
        if isinstance(data, list):
            return data
    return []


def _result_next_chunk_index(result) -> int | None:
    """Find the next chunk pointer on either inline or EXTERNAL_LINKS results."""
    if not result:
        return None
    if getattr(result, "next_chunk_index", None) is not None:
        return result.next_chunk_index

    external_links = getattr(result, "external_links", None) or []
    for link in reversed(external_links):
        if getattr(link, "next_chunk_index", None) is not None:
            return link.next_chunk_index
    return None


def _result_rows(result) -> list[list]:
    rows = list(getattr(result, "data_array", None) or [])
    for link in getattr(result, "external_links", None) or []:
        rows.extend(_fetch_external_link_rows(link))
    return rows


def _collect_statement_result_rows(w: WorkspaceClient, resp) -> list[list]:
    """Merge inline rows and EXTERNAL_LINKS chunks into one row list."""
    if not resp.result:
        return []
    all_rows = _result_rows(resp.result)
    statement_id = resp.statement_id
    next_chunk = _result_next_chunk_index(resp.result)
    seen_chunks: set[int] = set()
    while next_chunk is not None:
        if next_chunk in seen_chunks:
            log.warning("    Repeated chunk index %s detected; stopping pagination", next_chunk)
            break
        seen_chunks.add(next_chunk)
        log.info("    Fetching chunk %d ...", next_chunk)
        chunk = w.statement_execution.get_statement_result_chunk_n(
            statement_id=statement_id,
            chunk_index=next_chunk,
        )
        all_rows.extend(_result_rows(chunk))
        next_chunk = _result_next_chunk_index(chunk)

    if (
        not all_rows
        and resp.manifest
        and getattr(resp.manifest, "total_chunk_count", None)
    ):
        n = int(resp.manifest.total_chunk_count)
        for idx in range(n):
            chunk = w.statement_execution.get_statement_result_chunk_n(
                statement_id=statement_id,
                chunk_index=idx,
            )
            all_rows.extend(_result_rows(chunk))
    return all_rows


def _fetch_all_rows(
    w: WorkspaceClient,
    warehouse_id: str,
    sql: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
) -> list[list] | None:
    """Execute SQL via Statement Execution API; use EXTERNAL_LINKS if INLINE exceeds ~25 MiB."""
    from databricks.sdk.service.sql import Disposition, Format

    for disposition in (Disposition.INLINE, Disposition.EXTERNAL_LINKS):
        kwargs: dict = {
            "warehouse_id": warehouse_id,
            "statement": sql,
            "wait_timeout": "50s",
            "disposition": disposition,
            "format": Format.JSON_ARRAY,
        }
        if catalog is not None:
            kwargs["catalog"] = catalog
        if schema is not None:
            kwargs["schema"] = schema

        resp = w.statement_execution.execute_statement(**kwargs)

        if not resp.status:
            return None
        if resp.status.state == StatementState.FAILED:
            err = (
                resp.status.error.message
                if resp.status.error
                else str(resp.status.state)
            )
            if disposition == Disposition.INLINE and _inline_byte_limit_exceeded(err):
                log.info(
                    "  Result set too large for INLINE — retrying with EXTERNAL_LINKS ...",
                )
                continue
            log.warning("  Warehouse SQL failed: %s", (err or "")[:800])
            return None
        if resp.status.state != StatementState.SUCCEEDED:
            log.warning("  Warehouse SQL state: %s", resp.status.state)
            return None

        if not resp.result:
            return []

        if disposition == Disposition.EXTERNAL_LINKS:
            log.info("  Using EXTERNAL_LINKS result chunks (large result set).")

        return _collect_statement_result_rows(w, resp)

    return None


def _coerce_value(v):
    """Coerce a single cell for COPY (arrays from Spark/JSON become Python lists for PG TEXT[])."""
    if v is None or v == "null":
        return None
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return str(v)


def _insert_from_staging(
    cur,
    name: str,
    cols: str,
    staging: str,
    dedupe_key: str | None,
) -> None:
    """Insert rows from staging; Delta can return duplicate PK rows — dedupe with DISTINCT ON."""
    if dedupe_key:
        keys = ", ".join(k.strip() for k in dedupe_key.split(","))
        cur.execute(
            f"""
            INSERT INTO {name} ({cols})
            SELECT DISTINCT ON ({keys}) {cols}
            FROM {staging}
            ORDER BY {keys}
            """
        )
    else:
        cur.execute(f"INSERT INTO {name} ({cols}) SELECT {cols} FROM {staging}")


def _resolve_spark_manifest_type(col) -> str:
    tn = col.type_name
    s = tn.value if hasattr(tn, "value") else str(tn)
    return s.rsplit(".", 1)[-1].upper()


def _spark_type_to_pg(type_name: str) -> str:
    u = type_name.upper()
    if "ARRAY" in u:
        return "TEXT[]"
    if u in ("STRING", "BINARY"):
        return "TEXT"
    if u == "BOOLEAN":
        return "BOOLEAN"
    if u in ("BYTE", "SHORT", "INT", "LONG"):
        return "BIGINT"
    if u in ("FLOAT", "DOUBLE", "DECIMAL"):
        return "DOUBLE PRECISION"
    if u == "DATE":
        return "DATE"
    if "TIMESTAMP" in u:
        return "TIMESTAMP"
    if u in ("STRUCT", "MAP"):
        return "TEXT"
    return "TEXT"


def _pg_quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_ddl_for_enron_table(pg_name: str, columns: list[tuple[str, str]]) -> str:
    parts = [f"  {_pg_quote_ident(n)} {_spark_type_to_pg(t)}" for n, t in columns]
    return f"CREATE TABLE IF NOT EXISTS {pg_name} (\n" + ",\n".join(parts) + "\n)"


def _fetch_manifest_columns(w, warehouse_id: str, fqn: str) -> list[tuple[str, str]] | None:
    """Column names and Spark type tags from Statement API manifest (same order as SELECT *)."""
    from databricks.sdk.service.sql import Disposition, Format

    for sql in (f"SELECT * FROM {fqn} LIMIT 0", f"SELECT * FROM {fqn} LIMIT 1"):
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=sql,
            wait_timeout="50s",
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
        )
        if resp.status and resp.status.state == StatementState.FAILED:
            log.debug("Manifest query failed for %s: %s", fqn, resp.status.error)
            continue
        if resp.manifest and resp.manifest.schema and resp.manifest.schema.columns:
            return [
                (col.name, _resolve_spark_manifest_type(col))
                for col in resp.manifest.schema.columns
            ]
    return None


def _load_specified_table(
    w: WorkspaceClient,
    warehouse_id: str,
    cur,
    conn,
    name: str,
    spec: dict,
    *,
    warehouse_catalog: str | None = None,
    warehouse_schema: str | None = None,
) -> None:
    log.info("  Loading '%s' from %s ...", name, spec["source"])

    select_cols = spec.get("select_expr", spec["columns"])
    rows = _fetch_all_rows(
        w,
        warehouse_id,
        f"SELECT {select_cols} FROM {spec['source']}",
        catalog=warehouse_catalog,
        schema=warehouse_schema,
    )

    if rows is None:
        log.warning("  Query failed for %s — skipping", name)
        conn.rollback()
        return

    cols = spec["columns"]
    staging = f"_staging_{name.replace('.', '_')}"
    cur.execute(f"DROP TABLE IF EXISTS {staging}")
    cur.execute(f"CREATE TEMP TABLE {staging} (LIKE {name})")

    copy_sql = f"COPY {staging} ({cols}) FROM STDIN"
    with cur.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row([_coerce_value(v) for v in row])

    cur.execute(f"TRUNCATE {name}")
    _insert_from_staging(
        cur, name, cols, staging, spec.get("dedupe_key"),
    )
    inserted = cur.rowcount
    cur.execute(f"DROP TABLE IF EXISTS {staging}")

    conn.commit()
    log.info(
        "  Loaded %d rows into '%s' (%d source, %d deduped)",
        inserted,
        name,
        len(rows),
        len(rows) - inserted,
    )


def _load_enron_dynamic(
    w: WorkspaceClient,
    warehouse_id: str,
    cur,
    conn,
    short: str,
) -> None:
    """Tables in ENRON_CORPUS_TABLE_NAMES without explicit ENRON_TABLE_SCHEMAS: DDL from manifest + full load."""
    fqn = f"{CATALOG}.{ENRON_SCHEMA}.{short}"
    pg_name = f"enron.{short}"
    manifest_cols = _fetch_manifest_columns(w, warehouse_id, fqn)
    if not manifest_cols:
        log.warning("  Skipping %s — no manifest (table missing?)", fqn)
        return

    ddl = _pg_ddl_for_enron_table(pg_name, manifest_cols)
    # Dynamic tables are manifest-owned, so recreate them on refresh to avoid
    # stale constraints or column drift from older Lakebase schemas.
    cur.execute(f"DROP TABLE IF EXISTS {pg_name}")
    cur.execute(ddl)

    col_list = ", ".join(_pg_quote_ident(n) for n, _ in manifest_cols)
    rows = _fetch_all_rows(
        w,
        warehouse_id,
        f"SELECT * FROM {fqn}",
        catalog=CATALOG,
        schema=ENRON_SCHEMA,
    )
    if rows is None:
        log.warning("  Query failed for %s — skipping", fqn)
        conn.rollback()
        return

    staging = f"_staging_dyn_{short}"
    cur.execute(f"DROP TABLE IF EXISTS {staging}")
    cur.execute(f"CREATE TEMP TABLE {staging} (LIKE {pg_name})")

    copy_sql = f"COPY {staging} ({col_list}) FROM STDIN"
    with cur.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row([_coerce_value(v) for v in row])

    cur.execute(f"TRUNCATE {pg_name}")
    _insert_from_staging(cur, pg_name, col_list, staging, None)
    inserted = cur.rowcount
    cur.execute(f"DROP TABLE IF EXISTS {staging}")

    conn.commit()
    log.info(
        "  Loaded %d rows into '%s' (manifest DDL, %d source rows)",
        inserted,
        pg_name,
        len(rows),
    )


def load_data(w: WorkspaceClient, include_enron: bool = False):
    """Load data from Delta tables into Lakebase via SQL warehouse + COPY FROM STDIN."""
    warehouse_list = list(w.warehouses.list())
    if not warehouse_list:
        log.error("No SQL warehouses found — cannot load data")
        return
    warehouse_id = warehouse_list[0].id

    if include_enron:
        migrate_enron_lakebase_schema(w)

    with _pg_connect(w) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            for name, spec in BIBLE_TABLE_SCHEMAS.items():
                _load_specified_table(
                    w,
                    warehouse_id,
                    cur,
                    conn,
                    name,
                    spec,
                    warehouse_catalog=CATALOG,
                    warehouse_schema=BIBLE_SCHEMA,
                )

            if include_enron:
                for short in ENRON_CORPUS_TABLE_NAMES:
                    key = f"enron.{short}"
                    if key in ENRON_TABLE_SCHEMAS:
                        _load_specified_table(
                            w,
                            warehouse_id,
                            cur,
                            conn,
                            key,
                            ENRON_TABLE_SCHEMAS[key],
                            warehouse_catalog=CATALOG,
                            warehouse_schema=ENRON_SCHEMA,
                        )
                    else:
                        _load_enron_dynamic(w, warehouse_id, cur, conn, short)

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
        all_tables.extend(f"enron.{n}" for n in ENRON_CORPUS_TABLE_NAMES)

    log.info("Lakebase setup complete")
    log.info("  Project: %s", PROJECT_ID)
    log.info("  Endpoint: %s", get_endpoint_name())
    log.info("  Tables: %s", ", ".join(all_tables))
    log.info("  RLS: Bible (app.permitted_books)%s",
             " + Enron (app.user_tier)" if args.enron else "")


if __name__ == "__main__":
    main()
