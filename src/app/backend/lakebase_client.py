"""Lakebase-backed graph queries with RLS session variables.

Drop-in replacement for the SQL-warehouse functions in ``graph_client.py``.
Activated when ``GRAPHRAG_DATA_BACKEND=lakebase`` is set in the environment.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg
from databricks.sdk import WorkspaceClient

log = logging.getLogger(__name__)

LAKEBASE_ENDPOINT = os.environ.get(
    "LAKEBASE_ENDPOINT",
    "projects/graphrag/branches/production/endpoints/primary",
)
LAKEBASE_HOST = os.environ.get("LAKEBASE_HOST", "")
LAKEBASE_DBNAME = os.environ.get("LAKEBASE_DBNAME", "databricks_postgres")

_cached_host: str | None = None
_token_cache: tuple[str, str, float] = ("", "", 0)
_TOKEN_TTL = 2700  # refresh OAuth token every 45 min


def _resolve_credentials() -> tuple[str, str, str]:
    """Return (host, username, token) with lightweight caching."""
    global _cached_host, _token_cache

    now = time.time()
    if _token_cache[2] > now:
        return _cached_host or "", _token_cache[0], _token_cache[1]

    w = WorkspaceClient()
    host = _cached_host or LAKEBASE_HOST
    if not host:
        ep = w.postgres.get_endpoint(name=LAKEBASE_ENDPOINT)
        host = ep.status.hosts.host
        _cached_host = host

    cred = w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT)
    username = w.current_user.me().user_name
    _token_cache = (username, cred.token, now + _TOKEN_TTL)
    return host, username, cred.token


@contextmanager
def _get_connection(context: dict[str, str] | None = None):
    """Open a Lakebase connection and optionally set RLS session variables."""
    host, username, token = _resolve_credentials()
    conn = psycopg.connect(
        host=host,
        dbname=LAKEBASE_DBNAME,
        user=username,
        password=token,
        sslmode="require",
    )
    try:
        if context:
            with conn.cursor() as cur:
                for key, value in context.items():
                    if value:
                        cur.execute(
                            "SELECT set_config(%s, %s, true)",
                            (f"app.{key}", str(value)),
                        )
        yield conn
    finally:
        conn.close()


def _query(sql: str, params: dict | None = None, context: dict[str, str] | None = None) -> list[dict]:
    """Execute SQL and return rows as dicts, with optional RLS context."""
    with _get_connection(context) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            if cur.description is None:
                return []
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Data classes (shared with graph_client)
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    id: str
    label: str
    entity_type: str
    title: str = ""


@dataclass
class GraphEdge:
    source: str
    target: str
    label: str
    description: str = ""
    book: str = ""


@dataclass
class GraphData:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


@dataclass
class BookStatus:
    book_name: str
    testament: str
    total_chapters: int
    status: str
    entity_count: int
    relationship_count: int
    verse_count: int


@dataclass
class GraphStats:
    total_entities: int = 0
    total_relationships: int = 0
    total_verses: int = 0
    active_books: int = 0
    cross_book_entities: int = 0
    entity_type_counts: dict[str, int] = field(default_factory=dict)
    relationship_type_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nid(name: str) -> str:
    return "_".join(name.lower().split())


def _add_node(m: dict[str, GraphNode], name: str, etype: str) -> None:
    nid = _nid(name)
    if nid not in m:
        m[nid] = GraphNode(id=nid, label=name, entity_type=etype)


# ---------------------------------------------------------------------------
# Query functions (same API as graph_client.py)
# ---------------------------------------------------------------------------

def get_entity_neighborhood(entity_name: str, limit: int = 30,
                            context: dict[str, str] | None = None) -> GraphData:
    """Return nodes and edges surrounding an entity for vis.js rendering."""
    entity_id = "_".join(entity_name.lower().split())
    pattern = f"%{entity_id}%"

    rows = _query(
        """
        SELECT
            COALESCE(e1.name, r.source_entity) AS src_name,
            COALESCE(e1.entity_type, 'Unknown') AS src_type,
            r.relationship_type,
            COALESCE(e2.name, r.target_entity) AS tgt_name,
            COALESCE(e2.entity_type, 'Unknown') AS tgt_type,
            r.description,
            r.book
        FROM relationships r
        LEFT JOIN entities e1 ON r.source_entity = e1.entity_id
        LEFT JOIN entities e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE %(pattern)s
           OR r.target_entity LIKE %(pattern)s
        ORDER BY r.book, r.chapter
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
        context=context,
    )

    nodes_map: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    for row in rows:
        _add_node(nodes_map, row["src_name"], row["src_type"])
        _add_node(nodes_map, row["tgt_name"], row["tgt_type"])
        edges.append(GraphEdge(
            source=_nid(row["src_name"]),
            target=_nid(row["tgt_name"]),
            label=row["relationship_type"],
            description=row["description"] or "",
            book=row["book"] or "",
        ))

    return GraphData(nodes=list(nodes_map.values()), edges=edges)


def lookup_verses(references: list[str],
                  context: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve verse references like 'Ruth 4:13' to actual text."""
    import re

    if not references:
        return {}

    parsed: list[tuple[str, str, int, int, int]] = []
    for ref in references:
        m = re.match(
            r"^(\d?\s*[A-Za-z]+)\s+(\d+):(\d+)(?:\s*[-–]\s*(\d+))?",
            ref.strip(),
        )
        if not m:
            continue
        book = m.group(1).strip()
        chapter = int(m.group(2))
        v_start = int(m.group(3))
        v_end = int(m.group(4)) if m.group(4) else v_start
        parsed.append((ref.strip(), book, chapter, v_start, v_end))

    if not parsed:
        return {}

    result: dict[str, str] = {}
    for ref_str, book, chapter, v_start, v_end in parsed:
        try:
            rows = _query(
                """
                SELECT verse_number, text
                FROM verses
                WHERE book = %(book)s
                  AND chapter = %(chapter)s
                  AND verse_number BETWEEN %(v_start)s AND %(v_end)s
                ORDER BY verse_number
                """,
                {"book": book, "chapter": chapter, "v_start": v_start, "v_end": v_end},
                context=context,
            )
            if rows:
                texts = [f"[{chapter}:{r['verse_number']}] {r['text']}" for r in rows]
                result[ref_str] = " ".join(texts)
        except Exception:
            continue

    return result


def get_book_statuses(context: dict[str, str] | None = None) -> list[BookStatus]:
    """Read the book_registry table and return all book statuses."""
    try:
        rows = _query(
            """
            SELECT book_name, testament, total_chapters, status,
                   COALESCE(entity_count, 0) AS entity_count,
                   COALESCE(relationship_count, 0) AS relationship_count,
                   COALESCE(verse_count, 0) AS verse_count
            FROM book_registry
            ORDER BY testament, book_name
            """,
            context=context,
        )
    except Exception:
        return []

    return [
        BookStatus(
            book_name=r["book_name"],
            testament=r["testament"],
            total_chapters=r["total_chapters"],
            status=r["status"],
            entity_count=r["entity_count"],
            relationship_count=r["relationship_count"],
            verse_count=r["verse_count"],
        )
        for r in rows
    ]


def get_graph_stats(context: dict[str, str] | None = None) -> GraphStats:
    """Compute aggregate graph statistics."""
    stats = GraphStats()

    try:
        rows = _query("SELECT COUNT(*) AS cnt FROM entities", context=context)
        stats.total_entities = rows[0]["cnt"] if rows else 0

        rows = _query("SELECT COUNT(*) AS cnt FROM relationships", context=context)
        stats.total_relationships = rows[0]["cnt"] if rows else 0

        rows = _query("SELECT COUNT(*) AS cnt FROM verses", context=context)
        stats.total_verses = rows[0]["cnt"] if rows else 0

        rows = _query(
            "SELECT COUNT(*) AS cnt FROM book_registry WHERE status = 'active'",
            context=context,
        )
        stats.active_books = rows[0]["cnt"] if rows else 0

        rows = _query(
            """
            SELECT entity_type, COUNT(*) AS cnt
            FROM entities
            GROUP BY entity_type
            ORDER BY cnt DESC
            """,
            context=context,
        )
        stats.entity_type_counts = {r["entity_type"]: r["cnt"] for r in rows}

        rows = _query(
            """
            SELECT relationship_type, COUNT(*) AS cnt
            FROM relationships
            GROUP BY relationship_type
            ORDER BY cnt DESC
            LIMIT 15
            """,
            context=context,
        )
        stats.relationship_type_counts = {r["relationship_type"]: r["cnt"] for r in rows}

        rows = _query(
            """
            SELECT COUNT(*) AS cnt FROM (
                SELECT entity_id
                FROM entity_mentions
                GROUP BY entity_id
                HAVING COUNT(DISTINCT book) > 1
            ) sub
            """,
            context=context,
        )
        stats.cross_book_entities = rows[0]["cnt"] if rows else 0

    except Exception:
        pass

    return stats
