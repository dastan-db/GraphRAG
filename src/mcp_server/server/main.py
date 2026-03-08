"""GraphRAG MCP Server — graph analytics tools backed by Lakebase (PostgreSQL).

Tools exposed here serve pre-computed graph analytics (PageRank, degree centrality,
cross-testament analysis) and live graph traversal (BFS shortest paths via recursive
CTEs). The server queries Lakebase Autoscaling — a managed PostgreSQL instance
populated via reverse ETL synced tables from Delta.
"""

import os
import re
import logging
from contextlib import contextmanager

import psycopg
from fastmcp import FastMCP

log = logging.getLogger(__name__)

LAKEBASE_ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "")
LAKEBASE_HOST = os.environ.get("LAKEBASE_HOST", "")
LAKEBASE_DBNAME = os.environ.get("LAKEBASE_DBNAME", "databricks_postgres")

mcp = FastMCP("GraphRAG Graph Analytics")


def _resolve_credentials():
    """Resolve Lakebase host and OAuth token, caching the host across calls."""
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    host = LAKEBASE_HOST
    if not host:
        endpoint = w.postgres.get_endpoint(name=LAKEBASE_ENDPOINT)
        host = endpoint.status.hosts.host

    cred = w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT)
    username = w.current_user.me().user_name
    return host, username, cred.token


@contextmanager
def _get_connection():
    """Open a Lakebase PostgreSQL connection using OAuth credentials."""
    host, username, token = _resolve_credentials()
    conn = psycopg.connect(
        host=host,
        dbname=LAKEBASE_DBNAME,
        user=username,
        password=token,
        sslmode="require",
    )
    try:
        yield conn
    finally:
        conn.close()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a SQL query and return rows as dicts."""
    with _get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _slugify(name: str) -> str:
    """Normalise entity name to match stored entity_ids."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@mcp.tool()
def bfs_path(source: str, target: str) -> str:
    """Find the shortest path between two biblical entities.

    Computes paths on the fly using a recursive CTE over the relationships
    table — no pre-computed paths needed. Returns the shortest distance
    and direct relationships between the entities.

    Args:
        source: Source entity name (e.g. "Moses", "Ruth")
        target: Target entity name (e.g. "Jesus", "David")
    """
    source_slug = _slugify(source)
    target_slug = _slugify(target)

    rows = _query(
        """
        WITH RECURSIVE paths AS (
            SELECT source_entity AS current_node,
                   ARRAY[source_entity] AS path,
                   1 AS distance
            FROM relationships
            WHERE source_entity = %s

            UNION ALL

            SELECT r.target_entity,
                   p.path || r.target_entity,
                   p.distance + 1
            FROM paths p
            JOIN relationships r
              ON p.current_node = r.source_entity
             OR p.current_node = r.target_entity
            WHERE CASE
                    WHEN p.current_node = r.source_entity THEN r.target_entity
                    ELSE r.source_entity
                  END != ALL(p.path)
              AND p.distance < 6
        )
        SELECT path, distance
        FROM paths
        WHERE current_node = %s
        ORDER BY distance
        LIMIT 5
        """,
        (source_slug, target_slug),
    )

    if not rows:
        rows = _query(
            """
            WITH RECURSIVE paths AS (
                SELECT target_entity AS current_node,
                       ARRAY[target_entity] AS path,
                       1 AS distance
                FROM relationships
                WHERE target_entity = %s

                UNION ALL

                SELECT CASE
                         WHEN p.current_node = r.source_entity THEN r.target_entity
                         ELSE r.source_entity
                       END,
                       p.path || CASE
                         WHEN p.current_node = r.source_entity THEN r.target_entity
                         ELSE r.source_entity
                       END,
                       p.distance + 1
                FROM paths p
                JOIN relationships r
                  ON p.current_node = r.source_entity
                  OR p.current_node = r.target_entity
                WHERE CASE
                        WHEN p.current_node = r.source_entity THEN r.target_entity
                        ELSE r.source_entity
                      END != ALL(p.path)
                  AND p.distance < 6
            )
            SELECT path, distance
            FROM paths
            WHERE current_node = %s
            ORDER BY distance
            LIMIT 5
            """,
            (source_slug, target_slug),
        )

    if not rows:
        return f"No path found between '{source}' and '{target}' in the knowledge graph."

    source_name = _entity_name(source_slug) or source
    target_name = _entity_name(target_slug) or target

    lines = [f"Shortest paths between {source_name} and {target_name}:"]
    for r in rows:
        names = [_entity_name(eid) or eid for eid in r["path"]]
        lines.append(f"  distance={r['distance']}, path: {' -> '.join(names)}")

    rels = _query(
        """
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book, r.chapter
        FROM relationships r
        LEFT JOIN entities e1 ON r.source_entity = e1.entity_id
        LEFT JOIN entities e2 ON r.target_entity = e2.entity_id
        WHERE (r.source_entity = %s AND r.target_entity = %s)
           OR (r.source_entity = %s AND r.target_entity = %s)
        LIMIT 10
        """,
        (source_slug, target_slug, target_slug, source_slug),
    )

    if rels:
        lines.append("\nDirect relationships:")
        for r in rels:
            lines.append(
                f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: "
                f"{r['description']} ({r['book']} ch.{r['chapter']})"
            )

    return "\n".join(lines)


def _entity_name(entity_id: str) -> str | None:
    """Look up display name for an entity_id."""
    rows = _query(
        "SELECT name FROM entities WHERE entity_id = %s LIMIT 1",
        (entity_id,),
    )
    return rows[0]["name"] if rows else None


@mcp.tool()
def pagerank_ranking(entity_type: str = "", testament: str = "", limit: int = 10) -> str:
    """Find the most important/central entities by PageRank score.

    PageRank measures structural importance in the knowledge graph — entities
    with many incoming connections from other important entities rank higher.

    Args:
        entity_type: Filter by type (e.g. "Person", "Place"). Empty for all types.
        testament: Filter by testament ("OT" or "NT"). Empty for all.
        limit: Maximum results to return (default 10).
    """
    where_clauses = []
    params: list = []
    if entity_type:
        where_clauses.append("entity_type = %s")
        params.append(entity_type)
    if testament:
        where_clauses.append("testament = %s")
        params.append(testament.upper())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(int(limit))

    rows = _query(
        f"""
        SELECT name, entity_type, testament, pagerank, total_degree,
               cross_testament_connections
        FROM entity_analytics
        {where_sql}
        ORDER BY pagerank DESC
        LIMIT %s
        """,
        tuple(params),
    )

    if not rows:
        filters = []
        if entity_type:
            filters.append(f"type={entity_type}")
        if testament:
            filters.append(f"testament={testament}")
        return f"No entities found matching filters: {', '.join(filters) or 'none'}."

    lines = [
        f"Top {len(rows)} entities by PageRank"
        + (f" ({', '.join(where_clauses).replace('%s', '?')})" if where_clauses else "")
        + ":"
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"  {i}. {r['name']} ({r['entity_type']}, {r['testament']}) — "
            f"PageRank: {r['pagerank']:.4f}, degree: {r['total_degree']}, "
            f"cross-testament: {r['cross_testament_connections']}"
        )
    return "\n".join(lines)


@mcp.tool()
def cross_testament_analysis(
    source_testament: str = "", entity_type: str = "", limit: int = 10
) -> str:
    """Find entities with the most connections to entities from the other testament.

    Use this for questions like 'which NT person has the most OT connections'
    or 'which OT figure is most referenced in the New Testament'.

    Args:
        source_testament: Filter source entities by testament ("OT" or "NT"). Empty for all.
        entity_type: Filter by entity type (e.g. "Person"). Empty for all types.
        limit: Maximum results to return (default 10).
    """
    where_clauses = ["cross_testament_connections > 0"]
    params: list = []
    if source_testament:
        where_clauses.append("testament = %s")
        params.append(source_testament.upper())
    if entity_type:
        where_clauses.append("entity_type = %s")
        params.append(entity_type)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    params.append(int(limit))

    rows = _query(
        f"""
        SELECT name, entity_type, testament, cross_testament_connections,
               total_degree, pagerank
        FROM entity_analytics
        {where_sql}
        ORDER BY cross_testament_connections DESC
        LIMIT %s
        """,
        tuple(params),
    )

    if not rows:
        return "No entities found with cross-testament connections matching the given filters."

    lines = [f"Top {len(rows)} entities by cross-testament connections:"]
    for i, r in enumerate(rows, 1):
        other = "OT" if r["testament"] == "NT" else "NT"
        lines.append(
            f"  {i}. {r['name']} ({r['entity_type']}, {r['testament']}) — "
            f"{r['cross_testament_connections']} connections to {other} entities, "
            f"total degree: {r['total_degree']}, PageRank: {r['pagerank']:.4f}"
        )
    return "\n".join(lines)


@mcp.tool()
def entity_importance(entity_name: str) -> str:
    """Get graph analytics for a specific entity: PageRank rank, degree
    centrality, cross-testament connection count, and testament classification.

    Args:
        entity_name: The entity to look up (e.g. "Abraham", "Jesus", "Moses")
    """
    slug = _slugify(entity_name)

    rows = _query(
        """
        SELECT name, entity_type, testament, pagerank, in_degree, out_degree,
               total_degree, cross_testament_connections
        FROM entity_analytics
        WHERE entity_id = %s
        LIMIT 3
        """,
        (slug,),
    )

    if not rows:
        return f"Entity '{entity_name}' not found in graph analytics."

    rank_rows = _query(
        """
        SELECT COUNT(*) + 1 as rank
        FROM entity_analytics
        WHERE pagerank > (
            SELECT MAX(pagerank) FROM entity_analytics WHERE entity_id = %s
        )
        """,
        (slug,),
    )
    rank = rank_rows[0]["rank"] if rank_rows else "?"

    total_count = _query("SELECT COUNT(*) as cnt FROM entity_analytics")
    total = total_count[0]["cnt"] if total_count else "?"

    lines = []
    for r in rows:
        lines.append(f"Graph Analytics for {r['name']}:")
        lines.append(f"  Type: {r['entity_type']}")
        lines.append(f"  Testament: {r['testament']}")
        lines.append(f"  PageRank: {r['pagerank']:.4f} (rank #{rank} of {total})")
        lines.append(f"  In-degree: {r['in_degree']} (incoming relationships)")
        lines.append(f"  Out-degree: {r['out_degree']} (outgoing relationships)")
        lines.append(f"  Total degree: {r['total_degree']}")
        lines.append(f"  Cross-testament connections: {r['cross_testament_connections']}")
    return "\n".join(lines)


def main():
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
