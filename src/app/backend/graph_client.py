"""Query the knowledge graph tables for visualization.

Routes to either SQL Warehouse (default) or Lakebase (psycopg + RLS) based on
the ``GRAPHRAG_DATA_BACKEND`` environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_USE_LAKEBASE = os.getenv("GRAPHRAG_DATA_BACKEND", "warehouse").lower() == "lakebase"
from typing import Any

from databricks.sdk.core import Config

_conn: Any | None = None
_conn_created: float = 0
_CONN_TTL = 1800  # refresh SQL connection every 30 min


def _get_dbsql_module():
    try:
        import databricks.sql as dbsql
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "databricks-sql-connector is required for warehouse-backed graph queries."
        ) from exc
    return dbsql


def _get_connection():
    global _conn, _conn_created
    import time
    now = time.time()
    if _conn is not None and (now - _conn_created) < _CONN_TTL:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    cfg = Config()
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    dbsql = _get_dbsql_module()
    _conn = dbsql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
    )
    _conn_created = now
    return _conn


def _fqn(table: str) -> str:
    catalog = os.getenv("DATABRICKS_CATALOG", "serverless_8e8gyh_catalog")
    schema = os.getenv("DATABRICKS_SCHEMA", "graphrag_enron")
    return f"{catalog}.{schema}.{table}"


# ---- Data classes for graph visualization ----

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


# ---- Query functions ----

def get_entity_neighborhood(entity_name: str, limit: int = 30) -> GraphData:
    """Return nodes and edges surrounding an entity for vis.js rendering."""
    conn = _get_connection()
    entity_id = "_".join(entity_name.lower().split())
    pattern = f"%{entity_id}%"

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(e1.name, r.source_entity) AS src_name,
                COALESCE(e1.entity_type, 'Unknown') AS src_type,
                r.relationship_type,
                COALESCE(e2.name, r.target_entity) AS tgt_name,
                COALESCE(e2.entity_type, 'Unknown') AS tgt_type,
                r.description,
                r.book
            FROM {_fqn('relationships')} r
            LEFT JOIN {_fqn('entities')} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {_fqn('entities')} e2 ON r.target_entity = e2.entity_id
            WHERE r.source_entity LIKE %(pattern)s
               OR r.target_entity LIKE %(pattern)s
            ORDER BY r.book, r.chapter
            LIMIT {limit}
        """, {"pattern": pattern})
        rows = cur.fetchall()

    nodes_map: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    for row in rows:
        src_name, src_type, rel_type, tgt_name, tgt_type, desc, book = row
        src_id = "_".join(src_name.lower().split())
        tgt_id = "_".join(tgt_name.lower().split())

        if src_id not in nodes_map:
            nodes_map[src_id] = GraphNode(id=src_id, label=src_name, entity_type=src_type)
        if tgt_id not in nodes_map:
            nodes_map[tgt_id] = GraphNode(id=tgt_id, label=tgt_name, entity_type=tgt_type)

        edges.append(GraphEdge(
            source=src_id,
            target=tgt_id,
            label=rel_type,
            description=desc or "",
            book=book or "",
        ))

    return GraphData(nodes=list(nodes_map.values()), edges=edges)


def get_path_between(entity_a: str, entity_b: str) -> GraphData:
    """Trace a path (up to 3 hops) between two entities."""
    conn = _get_connection()
    id_a = "_".join(entity_a.lower().split())
    id_b = "_".join(entity_b.lower().split())

    # Try 1-hop, 2-hop, 3-hop (same logic as src/agent/tools.py trace_path)
    for hops, query, params in _path_queries(id_a, id_b):
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        if rows:
            return _rows_to_graph(rows, hops)

    return GraphData()


def _path_queries(id_a: str, id_b: str) -> list[tuple[int, str, dict]]:
    pa, pb = f"%{id_a}%", f"%{id_b}%"
    return [
        (1, f"""
            SELECT COALESCE(e1.name, r.source_entity) AS src,
                   COALESCE(e1.entity_type, 'Unknown') AS src_type,
                   r.relationship_type AS rel,
                   COALESCE(e2.name, r.target_entity) AS tgt,
                   COALESCE(e2.entity_type, 'Unknown') AS tgt_type,
                   r.book
            FROM {_fqn('relationships')} r
            LEFT JOIN {_fqn('entities')} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {_fqn('entities')} e2 ON r.target_entity = e2.entity_id
            WHERE (r.source_entity LIKE %(pa)s AND r.target_entity LIKE %(pb)s)
               OR (r.source_entity LIKE %(pb)s AND r.target_entity LIKE %(pa)s)
            LIMIT 10
        """, {"pa": pa, "pb": pb}),
        (2, f"""
            SELECT COALESCE(e1.name, r1.source_entity) AS src,
                   COALESCE(e1.entity_type, 'Unknown') AS src_type,
                   r1.relationship_type AS rel1,
                   COALESCE(e_mid.name, r1.target_entity) AS mid,
                   COALESCE(e_mid.entity_type, 'Unknown') AS mid_type,
                   r2.relationship_type AS rel2,
                   COALESCE(e2.name, r2.target_entity) AS tgt,
                   COALESCE(e2.entity_type, 'Unknown') AS tgt_type,
                   r1.book
            FROM {_fqn('relationships')} r1
            JOIN {_fqn('relationships')} r2 ON r1.target_entity = r2.source_entity
            LEFT JOIN {_fqn('entities')} e1 ON r1.source_entity = e1.entity_id
            LEFT JOIN {_fqn('entities')} e_mid ON r1.target_entity = e_mid.entity_id
            LEFT JOIN {_fqn('entities')} e2 ON r2.target_entity = e2.entity_id
            WHERE r1.source_entity LIKE %(pa)s AND r2.target_entity LIKE %(pb)s
            LIMIT 10
        """, {"pa": pa, "pb": pb}),
        (3, f"""
            SELECT COALESCE(e1.name, r1.source_entity) AS src,
                   COALESCE(e1.entity_type, 'Unknown') AS src_type,
                   r1.relationship_type AS rel1,
                   COALESCE(e_m1.name, r1.target_entity) AS mid1,
                   COALESCE(e_m1.entity_type, 'Unknown') AS mid1_type,
                   r2.relationship_type AS rel2,
                   COALESCE(e_m2.name, r2.target_entity) AS mid2,
                   COALESCE(e_m2.entity_type, 'Unknown') AS mid2_type,
                   r3.relationship_type AS rel3,
                   COALESCE(e3.name, r3.target_entity) AS tgt,
                   COALESCE(e3.entity_type, 'Unknown') AS tgt_type,
                   r1.book
            FROM {_fqn('relationships')} r1
            JOIN {_fqn('relationships')} r2 ON r1.target_entity = r2.source_entity
            JOIN {_fqn('relationships')} r3 ON r2.target_entity = r3.source_entity
            LEFT JOIN {_fqn('entities')} e1 ON r1.source_entity = e1.entity_id
            LEFT JOIN {_fqn('entities')} e_m1 ON r1.target_entity = e_m1.entity_id
            LEFT JOIN {_fqn('entities')} e_m2 ON r2.target_entity = e_m2.entity_id
            LEFT JOIN {_fqn('entities')} e3 ON r3.target_entity = e3.entity_id
            WHERE r1.source_entity LIKE %(pa)s AND r3.target_entity LIKE %(pb)s
            LIMIT 10
        """, {"pa": pa, "pb": pb}),
    ]


def _rows_to_graph(rows: list, hops: int) -> GraphData:
    nodes_map: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    for row in rows:
        if hops == 1:
            src, src_type, rel, tgt, tgt_type, book = row
            _add_node(nodes_map, src, src_type)
            _add_node(nodes_map, tgt, tgt_type)
            edges.append(GraphEdge(source=_nid(src), target=_nid(tgt), label=rel, book=book or ""))
        elif hops == 2:
            src, src_type, rel1, mid, mid_type, rel2, tgt, tgt_type, book = row
            _add_node(nodes_map, src, src_type)
            _add_node(nodes_map, mid, mid_type)
            _add_node(nodes_map, tgt, tgt_type)
            edges.append(GraphEdge(source=_nid(src), target=_nid(mid), label=rel1, book=book or ""))
            edges.append(GraphEdge(source=_nid(mid), target=_nid(tgt), label=rel2, book=book or ""))
        elif hops == 3:
            src, src_type, rel1, m1, m1_type, rel2, m2, m2_type, rel3, tgt, tgt_type, book = row
            _add_node(nodes_map, src, src_type)
            _add_node(nodes_map, m1, m1_type)
            _add_node(nodes_map, m2, m2_type)
            _add_node(nodes_map, tgt, tgt_type)
            edges.append(GraphEdge(source=_nid(src), target=_nid(m1), label=rel1, book=book or ""))
            edges.append(GraphEdge(source=_nid(m1), target=_nid(m2), label=rel2, book=book or ""))
            edges.append(GraphEdge(source=_nid(m2), target=_nid(tgt), label=rel3, book=book or ""))

    return GraphData(nodes=list(nodes_map.values()), edges=edges)


def _nid(name: str) -> str:
    return "_".join(name.lower().split())


def _add_node(m: dict[str, GraphNode], name: str, etype: str) -> None:
    nid = _nid(name)
    if nid not in m:
        m[nid] = GraphNode(id=nid, label=name, entity_type=etype)


# ---- Mock mode ----

# ---- Book registry and graph stats ----

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


def get_book_statuses() -> list[BookStatus]:
    """Read the book_registry table and return all book statuses."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT book_name, testament, total_chapters, status,
                       COALESCE(entity_count, 0), COALESCE(relationship_count, 0),
                       COALESCE(verse_count, 0)
                FROM {_fqn('book_registry')}
                ORDER BY testament, book_name
            """)
            rows = cur.fetchall()
    except Exception:
        return []

    return [
        BookStatus(
            book_name=r[0], testament=r[1], total_chapters=r[2],
            status=r[3], entity_count=r[4], relationship_count=r[5],
            verse_count=r[6],
        )
        for r in rows
    ]


def get_graph_stats() -> GraphStats:
    """Compute aggregate graph statistics from the knowledge graph tables."""
    conn = _get_connection()
    stats = GraphStats()

    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_fqn('entities')}")
            stats.total_entities = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM {_fqn('relationships')}")
            stats.total_relationships = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM {_fqn('verses')}")
            stats.total_verses = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM {_fqn('book_registry')}
                WHERE status = 'active'
            """)
            stats.active_books = cur.fetchone()[0]

            cur.execute(f"""
                SELECT entity_type, COUNT(*) as cnt
                FROM {_fqn('entities')}
                GROUP BY entity_type
                ORDER BY cnt DESC
            """)
            stats.entity_type_counts = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(f"""
                SELECT relationship_type, COUNT(*) as cnt
                FROM {_fqn('relationships')}
                GROUP BY relationship_type
                ORDER BY cnt DESC
                LIMIT 15
            """)
            stats.relationship_type_counts = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT entity_id
                    FROM {_fqn('entity_mentions')}
                    GROUP BY entity_id
                    HAVING COUNT(DISTINCT book) > 1
                )
            """)
            stats.cross_book_entities = cur.fetchone()[0]

    except Exception:
        pass

    return stats


def get_book_statuses_mock() -> list[BookStatus]:
    """Return mock book statuses for demo mode."""
    _BOOKS = [
        ("Email Corpus", "core", 15, "active", 242, 611, 20000),
        ("Investigation Timeline", "curated", 1, "active", 28, 27, 28),
        ("Org Hierarchy", "curated", 1, "active", 24, 23, 24),
        ("Topic Taxonomy", "derived", 1, "active", 41, 64, 41),
        ("ABAC Views", "governance", 6, "active", 0, 0, 0),
        ("Materialized Views", "derived", 2, "available", 0, 0, 0),
    ]
    return [
        BookStatus(book_name=b[0], testament=b[1], total_chapters=b[2],
                   status=b[3], entity_count=b[4], relationship_count=b[5],
                   verse_count=b[6])
        for b in _BOOKS
    ]


def get_graph_stats_mock() -> GraphStats:
    """Return mock graph stats for demo mode."""
    return GraphStats(
        total_entities=1242,
        total_relationships=3611,
        total_verses=20000,
        active_books=5,
        cross_book_entities=173,
        entity_type_counts={"Person": 421, "Organization": 163, "Group": 97, "Project": 56, "Location": 42},
        relationship_type_counts={"SENT_TO": 1260, "CC_TO": 702, "MENTIONS": 488, "REPORTS_TO": 73},
    )


def get_entity_neighborhood_mock(entity_name: str) -> GraphData:
    """Return sample graph data for demo without a live warehouse."""
    nodes = [
        GraphNode(id="kenneth_lay", label="Kenneth Lay", entity_type="Person"),
        GraphNode(id="jeff_skilling", label="Jeff Skilling", entity_type="Person"),
        GraphNode(id="andrew_fastow", label="Andrew Fastow", entity_type="Person"),
        GraphNode(id="david_delainey", label="David Delainey", entity_type="Person"),
        GraphNode(id="arthur_andersen", label="Arthur Andersen", entity_type="Organization"),
    ]
    edges = [
        GraphEdge(source="kenneth_lay", target="jeff_skilling", label="MANAGES", book="Org Hierarchy"),
        GraphEdge(source="jeff_skilling", target="andrew_fastow", label="WORKED_WITH", book="Email Corpus"),
        GraphEdge(source="jeff_skilling", target="david_delainey", label="MANAGES", book="Org Hierarchy"),
        GraphEdge(source="andrew_fastow", target="arthur_andersen", label="MENTIONS", book="Investigation Timeline"),
    ]
    return GraphData(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Lakebase routing — override live functions when GRAPHRAG_DATA_BACKEND=lakebase
# ---------------------------------------------------------------------------

if _USE_LAKEBASE:
    from backend import lakebase_client as _lb

    def get_entity_neighborhood(entity_name: str, limit: int = 30,  # noqa: F811
                                context: dict | None = None) -> GraphData:
        lb_data = _lb.get_entity_neighborhood(entity_name, limit=limit, context=context)
        nodes = [GraphNode(id=n.id, label=n.label, entity_type=n.entity_type) for n in lb_data.nodes]
        edges = [GraphEdge(source=e.source, target=e.target, label=e.label,
                           description=e.description, book=e.book) for e in lb_data.edges]
        return GraphData(nodes=nodes, edges=edges)

    def get_book_statuses() -> list[BookStatus]:  # noqa: F811
        lb_list = _lb.get_book_statuses()
        return [
            BookStatus(book_name=b.book_name, testament=b.testament,
                       total_chapters=b.total_chapters, status=b.status,
                       entity_count=b.entity_count, relationship_count=b.relationship_count,
                       verse_count=b.verse_count)
            for b in lb_list
        ]

    def get_graph_stats() -> GraphStats:  # noqa: F811
        lb_stats = _lb.get_graph_stats()
        return GraphStats(
            total_entities=lb_stats.total_entities,
            total_relationships=lb_stats.total_relationships,
            total_verses=lb_stats.total_verses,
            active_books=lb_stats.active_books,
            cross_book_entities=lb_stats.cross_book_entities,
            entity_type_counts=lb_stats.entity_type_counts,
            relationship_type_counts=lb_stats.relationship_type_counts,
        )
