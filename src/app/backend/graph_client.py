"""Query the knowledge graph Delta tables via SQL Warehouse for visualization."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from databricks import sql as dbsql
from databricks.sdk.core import Config

_conn: Any | None = None
_conn_created: float = 0
_CONN_TTL = 1800  # refresh SQL connection every 30 min


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
    _conn = dbsql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
    )
    _conn_created = now
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


# ---- Verse lookup ----

def lookup_verses(references: list[str]) -> dict[str, str]:
    """Resolve verse references like 'Ruth 4:13' or 'Genesis 46:1-7' to actual text.

    Returns {reference_string: verse_text} for each reference that could be resolved.
    Range references (e.g. 'Exodus 14:21-22') fetch all verses in the range.
    """
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

    conn = _get_connection()
    result: dict[str, str] = {}

    for ref_str, book, chapter, v_start, v_end in parsed:
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT verse_number, text
                    FROM {_fqn('verses')}
                    WHERE book = %(book)s
                      AND chapter = %(chapter)s
                      AND verse_number BETWEEN %(v_start)s AND %(v_end)s
                    ORDER BY verse_number
                """, {"book": book, "chapter": chapter, "v_start": v_start, "v_end": v_end})
                rows = cur.fetchall()

            if rows:
                texts = [f"[{chapter}:{r[0]}] {r[1]}" for r in rows]
                result[ref_str] = " ".join(texts)
        except Exception:
            continue

    return result


def lookup_verses_mock(references: list[str]) -> dict[str, str]:
    """Return canned verse text for demo mode."""
    _MOCK_VERSES = {
        "Ruth 4:13": "[4:13] So Boaz took Ruth, and she was his wife: and when he went in unto her, the LORD gave her conception, and she bare a son.",
        "Ruth 4:17": "[4:17] And the women her neighbours gave it a name, saying, There is a son born to Naomi; and they called his name Obed: he is the father of Jesse, the father of David.",
        "Ruth 4:22": "[4:22] And Obed begat Jesse, and Jesse begat David.",
        "Matthew 1:6": "[1:6] And Jesse begat David the king; and David the king begat Solomon of her that had been the wife of Urias;",
        "Matthew 1:16": "[1:16] And Jacob begat Joseph the husband of Mary, of whom was born Jesus, who is called Christ.",
        "Genesis 46:6": "[46:6] And they took their cattle, and their goods, which they had gotten in the land of Canaan, and came into Egypt, Jacob, and all his seed with him.",
        "Exodus 1:11": "[1:11] Therefore they did set over them taskmasters to afflict them with their burdens. And they built for Pharaoh treasure cities, Pithom and Raamses.",
        "Exodus 12:31": "[12:31] And he called for Moses and Aaron by night, and said, Rise up, and get you forth from among my people, both ye and the children of Israel; and go, serve the LORD, as ye have said.",
        "Exodus 14:21": "[14:21] And Moses stretched out his hand over the sea; and the LORD caused the sea to go back by a strong east wind all that night, and made the sea dry land, and the waters were divided.",
        "Exodus 19:1": "[19:1] In the third month, when the children of Israel were gone forth out of the land of Egypt, the same day came they into the wilderness of Sinai.",
        "Exodus 2:10": "[2:10] And the child grew, and she brought him unto Pharaoh's daughter, and he became her son. And she called his name Moses: and she said, Because I drew him out of the water.",
        "Exodus 3:4": "[3:4] And when the LORD saw that he turned aside to see, God called unto him out of the midst of the bush, and said, Moses, Moses. And he said, Here am I.",
        "Exodus 20:1": "[20:1] And God spake all these words, saying,",
        "Acts 7:20": "[7:20] In which time Moses was born, and was exceeding fair, and nourished up in his father's house three months.",
        "Acts 9:1": "[9:1] And Saul, yet breathing out threatenings and slaughter against the disciples of the Lord, went unto the high priest,",
        "Acts 9:3": "[9:3] And as he journeyed, he came near Damascus: and suddenly there shined round about him a light from heaven.",
        "Acts 9:5": "[9:5] And he said, Who art thou, Lord? And the Lord said, I am Jesus whom thou persecutest: it is hard for thee to kick against the pricks.",
        "Acts 9:17": "[9:17] And Ananias went his way, and entered into the house; and putting his hands on him said, Brother Saul, the Lord, even Jesus, that appeared unto thee in the way as thou camest, hath sent me, that thou mightest receive thy sight, and be filled with the Holy Ghost.",
        "Acts 13:9": "[13:9] Then Saul, (who also is called Paul,) filled with the Holy Ghost, set his eyes on him.",
        "Matthew 1:1": "[1:1] The book of the generation of Jesus Christ, the son of David, the son of Abraham.",
        "Genesis 50:26": "[50:26] So Joseph died, being an hundred and ten years old: and they embalmed him, and he was put in a coffin in Egypt.",
        "Exodus 1:6": "[1:6] And Joseph died, and all his brethren, and all that generation.",
        "Exodus 1:7": "[1:7] And the children of Israel were fruitful, and increased abundantly, and multiplied, and waxed exceeding mighty; and the land was filled with them.",
        "Exodus 1:8": "[1:8] Now there arose up a new king over Egypt, which knew not Joseph.",
    }
    result = {}
    for ref in references:
        ref = ref.strip()
        if ref in _MOCK_VERSES:
            result[ref] = _MOCK_VERSES[ref]
    return result


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
        ("Genesis", "OT", 50, "active", 85, 210, 1533),
        ("Exodus", "OT", 40, "active", 72, 180, 1213),
        ("Ruth", "OT", 4, "active", 18, 32, 85),
        ("Matthew", "NT", 28, "active", 95, 250, 1071),
        ("Acts", "NT", 28, "active", 88, 220, 1007),
        ("Leviticus", "OT", 27, "available", 0, 0, 0),
        ("Numbers", "OT", 36, "available", 0, 0, 0),
        ("Deuteronomy", "OT", 34, "available", 0, 0, 0),
        ("John", "NT", 21, "available", 0, 0, 0),
        ("Romans", "NT", 16, "available", 0, 0, 0),
        ("Revelation", "NT", 22, "available", 0, 0, 0),
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
        total_entities=358,
        total_relationships=892,
        total_verses=4909,
        active_books=5,
        cross_book_entities=47,
        entity_type_counts={"Person": 180, "Place": 85, "Group": 42, "Event": 30, "Concept": 21},
        relationship_type_counts={"FAMILY_OF": 120, "SPOKE_TO": 95, "TRAVELED_TO": 78, "PARENT_OF": 65},
    )


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
