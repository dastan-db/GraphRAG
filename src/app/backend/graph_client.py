"""Query the knowledge graph Delta tables via SQL Warehouse for visualization."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from databricks import sql as dbsql
from databricks.sdk.core import Config

_conn: Any | None = None


def _get_connection():
    global _conn
    if _conn is not None:
        return _conn
    cfg = Config()
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    _conn = dbsql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
    )
    return _conn


def _fqn(table: str) -> str:
    catalog = os.getenv("DATABRICKS_CATALOG", "serverless_8e8gyh_catalog")
    schema = os.getenv("DATABRICKS_SCHEMA", "graphrag_bible")
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
            WHERE r.source_entity LIKE '%{entity_id}%'
               OR r.target_entity LIKE '%{entity_id}%'
            ORDER BY r.book, r.chapter
            LIMIT {limit}
        """)
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
    for hops, query in _path_queries(id_a, id_b):
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
        if rows:
            return _rows_to_graph(rows, hops)

    return GraphData()


def _path_queries(id_a: str, id_b: str) -> list[tuple[int, str]]:
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
            WHERE (r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
               OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%')
            LIMIT 10
        """),
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
            WHERE r1.source_entity LIKE '%{id_a}%' AND r2.target_entity LIKE '%{id_b}%'
            LIMIT 10
        """),
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
            WHERE r1.source_entity LIKE '%{id_a}%' AND r3.target_entity LIKE '%{id_b}%'
            LIMIT 10
        """),
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

def get_entity_neighborhood_mock(entity_name: str) -> GraphData:
    """Return sample graph data for demo without a live warehouse."""
    nodes = [
        GraphNode(id="ruth", label="Ruth", entity_type="Person"),
        GraphNode(id="boaz", label="Boaz", entity_type="Person"),
        GraphNode(id="obed", label="Obed", entity_type="Person"),
        GraphNode(id="jesse", label="Jesse", entity_type="Person"),
        GraphNode(id="david", label="David", entity_type="Person"),
        GraphNode(id="jesus", label="Jesus", entity_type="Person"),
    ]
    edges = [
        GraphEdge(source="ruth", target="boaz", label="MARRIED_TO", book="Ruth"),
        GraphEdge(source="ruth", target="obed", label="PARENT_OF", book="Ruth"),
        GraphEdge(source="boaz", target="obed", label="PARENT_OF", book="Ruth"),
        GraphEdge(source="obed", target="jesse", label="PARENT_OF", book="Ruth"),
        GraphEdge(source="jesse", target="david", label="PARENT_OF", book="Ruth"),
        GraphEdge(source="david", target="jesus", label="ANCESTOR_OF", book="Matthew"),
    ]
    return GraphData(nodes=nodes, edges=edges)
