# Databricks notebook source
# MAGIC %md
# MAGIC ### Graph Traversal Tools
# MAGIC `@tool` functions that query the knowledge graph Delta tables via Spark SQL.
# MAGIC
# MAGIC Each tool supports optional document-scoped retrieval via a `permitted_books`
# MAGIC parameter. When set, queries are restricted to entities and relationships from
# MAGIC the permitted books only. The parameter is injected by the agent framework —
# MAGIC not exposed to the LLM — to enforce document-level access control.

# COMMAND ----------

import json
import time
import logging

from langchain_core.tools import tool
from functools import partial, wraps


def _row_get(row, key, default=None):
    """Safely get a field from a PySpark Row or dict.

    PySpark Row objects don't support .get(), so bracket-access with a try/except
    is needed for optional columns.
    """
    try:
        val = row[key]
        return val if val is not None else default
    except (KeyError, IndexError, ValueError):
        return default

# COMMAND ----------

# DBTITLE 1,Tool Latency Instrumentation
_latency_log = logging.getLogger(__name__ + ".latency")
_tool_latency_buffer: list[dict] = []


def _instrument_tool(fn, tool_name: str):
    """Wrap a tool implementation to record per-invocation latency.

    Records timing via:
    1. In-process buffer (for local eval within a single process)
    2. MLflow trace span (persists to tracking server — survives Model Serving restarts)
    3. MLflow run metric (backward compat with Cycle 5)
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        import mlflow

        start = time.perf_counter()
        span = None
        try:
            span = mlflow.start_span(name=f"tool.{tool_name}")
            span.set_attributes({
                "tool.name": tool_name,
                "tool.args": json.dumps(
                    {k: str(v)[:200] for k, v in (kwargs or {}).items()},
                    default=str,
                ),
            })
        except Exception:
            span = None

        try:
            result = fn(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            _tool_latency_buffer.append({"tool": tool_name, "latency_ms": elapsed_ms})
            _latency_log.debug("tool=%s latency=%.1fms", tool_name, elapsed_ms)

            if span is not None:
                try:
                    span.set_attributes({
                        "tool.latency_ms": elapsed_ms,
                        "tool.sla_threshold_ms": config.get("tool_sla_thresholds_ms", {}).get(tool_name, -1),
                    })
                    span.end()
                except Exception:
                    pass

            try:
                if mlflow.active_run():
                    mlflow.log_metric(f"tool_latency_{tool_name}_ms", elapsed_ms)
            except Exception:
                pass
        return result
    return wrapper


def get_latency_report(from_traces: bool = False, experiment_id: str = None,
                       max_traces: int = 100) -> dict:
    """Aggregate p50/p95/p99 latency with percentile stats and SLA compliance.

    Sources (tried in order):
    1. In-process buffer (always available during local eval)
    2. MLflow trace spans (when from_traces=True or buffer is empty)
       Requires an active MLflow experiment or explicit experiment_id.
    """
    from collections import defaultdict
    import math

    by_tool: dict[str, list[float]] = defaultdict(list)

    for entry in _tool_latency_buffer:
        by_tool[entry["tool"]].append(entry["latency_ms"])

    if (from_traces or not by_tool):
        try:
            import mlflow
            traces = mlflow.search_traces(
                experiment_ids=[experiment_id] if experiment_id else None,
                max_results=max_traces,
            )
            for trace in traces if traces is not None else []:
                for span in getattr(trace, "data", {}).get("spans", []):
                    attrs = getattr(span, "attributes", {}) or {}
                    tname = attrs.get("tool.name")
                    lat = attrs.get("tool.latency_ms")
                    if tname and lat is not None:
                        by_tool[tname].append(float(lat))
        except Exception:
            pass

    sla = config.get("tool_sla_thresholds_ms", {})
    report = {}
    for tool_name, latencies in by_tool.items():
        latencies.sort()
        n = len(latencies)

        def _percentile(pct, _lats=latencies, _n=n):
            idx = math.ceil(pct / 100 * _n) - 1
            return round(_lats[max(0, min(idx, _n - 1))], 1)

        p95 = _percentile(95)
        threshold = sla.get(tool_name)
        report[tool_name] = {
            "count": n,
            "p50_ms": _percentile(50),
            "p95_ms": p95,
            "p99_ms": _percentile(99),
            "sla_threshold_ms": threshold,
            "sla_compliant": p95 <= threshold if threshold else None,
        }
    return report

# COMMAND ----------

# DBTITLE 1,SQL Helpers
def _books_in_clause(permitted_books):
    """Build a SQL IN clause from a list of book names. Returns None if no filtering."""
    if not permitted_books:
        return None
    escaped = ", ".join(f"'{b}'" for b in permitted_books)
    return f"({escaped})"

# COMMAND ----------

# DBTITLE 1,Tool: Find Entity
def _find_entity(name: str, permitted_books: list = None) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    books_clause = _books_in_clause(permitted_books)
    if books_clause:
        # Only return entities that have at least one mention in a permitted book
        results = spark.sql(f"""
            SELECT DISTINCT e.name, e.entity_type, e.description,
                   e.first_mention_book, e.first_mention_chapter
            FROM {config['entities_table']} e
            JOIN {config['entity_mentions_table']} em ON e.entity_id = em.entity_id
            WHERE LOWER(e.name) LIKE LOWER('%{name}%')
              AND em.book IN {books_clause}
            ORDER BY e.name
            LIMIT 10
        """).collect()
    else:
        results = spark.sql(f"""
            SELECT name, entity_type, description, first_mention_book, first_mention_chapter
            FROM {config['entities_table']}
            WHERE LOWER(name) LIKE LOWER('%{name}%')
            ORDER BY name
            LIMIT 10
        """).collect()

    if not results:
        return f"No entity found matching '{name}'."

    lines = []
    for r in results:
        lines.append(
            f"- **{r['name']}** ({r['entity_type']}): {r['description']} "
            f"[First mentioned: {r['first_mention_book']} ch.{r['first_mention_chapter']}]"
        )
    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Tool: Find Connections
def _find_connections(entity_name: str, permitted_books: list = None) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())
    books_clause = _books_in_clause(permitted_books)
    book_filter = f"AND r.book IN {books_clause}" if books_clause else ""

    results = spark.sql(f"""
        SELECT source_name, relationship_type, target_name,
               MAX(description) as description,
               MAX(book) as book, MAX(chapter) as chapter,
               COUNT(*) as frequency
        FROM (
            SELECT
                COALESCE(e1.name, r.source_entity) as source_name,
                r.relationship_type,
                COALESCE(e2.name, r.target_entity) as target_name,
                r.description,
                r.book,
                r.chapter
            FROM {config['relationships_table']} r
            LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
            WHERE (r.source_entity LIKE '%{entity_id}%'
               OR r.target_entity LIKE '%{entity_id}%')
            {book_filter}
        ) sub
        GROUP BY source_name, relationship_type, target_name
        ORDER BY frequency DESC
        LIMIT 100
    """).collect()

    if not results:
        return f"No connections found for '{entity_name}'."

    lines = [f"Connections for '{entity_name}' ({len(results)} found, ranked by frequency):"]
    for r in results:
        freq = int(r['frequency']) if _row_get(r, 'frequency') else 1
        lines.append(
            f"- {r['source_name']} --[{r['relationship_type']}]--> {r['target_name']}: "
            f"{r['description']} ({r['book']} ch.{r['chapter']}) [freq={freq}]"
        )
    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Tool: Trace Path (Pre-computed via GraphFrames BFS)
def _trace_path(entity_a: str, entity_b: str, permitted_books: list = None) -> str:
    """Find the shortest path between two entities using pre-computed GraphFrames BFS results.

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    import re
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    def slugify(name):
        return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

    id_a = slugify(entity_a)
    id_b = slugify(entity_b)

    paths = spark.sql(f"""
        SELECT p.source_id, p.target_id, p.distance, p.path_names,
               e1.name as source_name, e2.name as target_name
        FROM {config['entity_paths_table']} p
        LEFT JOIN {config['entities_table']} e1 ON p.source_id = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON p.target_id = e2.entity_id
        WHERE (p.source_id LIKE '%{id_a}%' AND p.target_id LIKE '%{id_b}%')
           OR (p.source_id LIKE '%{id_b}%' AND p.target_id LIKE '%{id_a}%')
        ORDER BY p.distance
        LIMIT 5
    """).collect()

    if not paths:
        return f"No path found between '{entity_a}' and '{entity_b}' in the knowledge graph."

    lines = [f"Shortest paths between {entity_a} and {entity_b}:"]
    for r in paths:
        lines.append(
            f"  {r['source_name']} -> {r['target_name']}: "
            f"distance={r['distance']}, path: {r['path_names']}"
        )

    books_clause = _books_in_clause(permitted_books)
    book_filter = f"AND r.book IN {books_clause}" if books_clause else ""

    rels = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book, r.chapter
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE ((r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
           OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%'))
        {book_filter}
        LIMIT 10
    """).collect()

    if rels:
        lines.append("\nDirect relationships:")
        for r in rels:
            lines.append(
                f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: "
                f"{r['description']} ({r['book']} ch.{r['chapter']})"
            )

    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Tool: Get Context Verses
def _get_source_evidence(entity_name: str, book: str = "", permitted_books: list = None) -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    books_clause = _books_in_clause(permitted_books)

    if book:
        book_filter = f"AND v.book = '{book}'"
        if books_clause and book not in permitted_books:
            return f"Book '{book}' is not in the permitted document set."
    elif books_clause:
        book_filter = f"AND v.book IN {books_clause}"
    else:
        book_filter = ""

    results = spark.sql(f"""
        SELECT v.book, v.chapter, v.verse_number, v.text
        FROM {config['verses_table']} v
        WHERE v.text LIKE '%{entity_name}%'
        {book_filter}
        ORDER BY v.book, v.chapter, v.verse_number
        LIMIT 15
    """).collect()

    if not results:
        return f"No verses found mentioning '{entity_name}'" + (f" in {book}" if book else "") + "."

    lines = [f"Verses mentioning '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(f"  {r['book']} {r['chapter']}:{r['verse_number']} — {r['text']}")
    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Tool: Get Entity Summary
def _get_entity_summary(entity_name: str, permitted_books: list = None) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())
    books_clause = _books_in_clause(permitted_books)

    # Entity info — scoped via entity_mentions if permitted_books is set
    if books_clause:
        entity_rows = spark.sql(f"""
            SELECT DISTINCT e.name, e.entity_type, e.description,
                   e.first_mention_book, e.first_mention_chapter
            FROM {config['entities_table']} e
            JOIN {config['entity_mentions_table']} em ON e.entity_id = em.entity_id
            WHERE e.entity_id LIKE '%{entity_id}%'
              AND em.book IN {books_clause}
            LIMIT 1
        """).collect()
    else:
        entity_rows = spark.sql(f"""
            SELECT name, entity_type, description, first_mention_book, first_mention_chapter
            FROM {config['entities_table']}
            WHERE entity_id LIKE '%{entity_id}%'
            LIMIT 1
        """).collect()

    if not entity_rows:
        return f"Entity '{entity_name}' not found in the knowledge graph."

    ent = entity_rows[0]
    lines = [
        f"**{ent['name']}** ({ent['entity_type']})",
        f"Description: {ent['description']}",
        f"First mentioned: {ent['first_mention_book']} ch.{ent['first_mention_chapter']}",
    ]

    book_filter = f"AND r.book IN {books_clause}" if books_clause else ""

    # Books mentioned in (scoped)
    books = spark.sql(f"""
        SELECT DISTINCT book FROM {config['relationships_table']} r
        WHERE (r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%')
        {book_filter}
        ORDER BY book
    """).collect()
    if books:
        lines.append(f"Appears in: {', '.join(r['book'] for r in books)}")

    # Key relationships (scoped)
    rels = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE (r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%')
        {book_filter}
        LIMIT 20
    """).collect()

    if rels:
        lines.append(f"\nKey relationships ({len(rels)}):")
        for r in rels:
            lines.append(f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: {r['description']}")

    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Instrumented Internal Functions
_find_entity_i = _instrument_tool(_find_entity, "find_entity")
_find_connections_i = _instrument_tool(_find_connections, "find_connections")
_trace_path_i = _instrument_tool(_trace_path, "trace_path")
_get_source_evidence_i = _instrument_tool(_get_source_evidence, "get_source_evidence")
_get_entity_summary_i = _instrument_tool(_get_entity_summary, "get_entity_summary")

# COMMAND ----------

# DBTITLE 1,Tool Registration — Default (Unscoped) Tools
@tool
def find_entity(name: str) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    return _find_entity_i(name)

@tool
def find_connections(entity_name: str) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    return _find_connections_i(entity_name)

@tool
def trace_path(entity_a: str, entity_b: str) -> str:
    """Find the shortest path between two entities using pre-computed GraphFrames BFS.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    return _trace_path_i(entity_a, entity_b)

@tool
def get_source_evidence(entity_name: str, book: str = "") -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    return _get_source_evidence_i(entity_name, book)

@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    return _get_entity_summary_i(entity_name)


# COMMAND ----------

# DBTITLE 1,Tool: Graph Exhaustion Check
def _graph_exhaustion_check(entity_name: str, max_depth: int = 3,
                            tables: dict = None, permitted_books: list = None) -> str:
    """BFS reachability report from a starting entity.

    Returns JSON with nodes visited, frontier size, evidence density, and
    whether the traversal is exhausted (frontier == 0).
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    ent_table = (tables or {}).get("entities", config["entities_table"])
    rel_table = (tables or {}).get("relationships", config["relationships_table"])
    books_clause = _books_in_clause(permitted_books) if permitted_books else None
    book_filter = f"AND r.book IN {books_clause}" if books_clause else ""

    entity_id = "_".join(entity_name.lower().split())
    seed_rows = spark.sql(f"""
        SELECT entity_id FROM {ent_table}
        WHERE entity_id LIKE '%{entity_id}%' LIMIT 1
    """).collect()
    if not seed_rows:
        return json.dumps({"error": f"Entity '{entity_name}' not found.", "exhausted": False})

    seed_id = seed_rows[0]["entity_id"]
    visited: set[str] = {seed_id}
    current_frontier: set[str] = {seed_id}
    evidence_count = 0

    for depth in range(max_depth):
        if not current_frontier:
            break
        frontier_ids = ", ".join(f"'{eid}'" for eid in current_frontier)
        neighbors = spark.sql(f"""
            SELECT DISTINCT
                CASE WHEN r.source_entity IN ({frontier_ids}) THEN r.target_entity
                     ELSE r.source_entity END AS neighbor_id
            FROM {rel_table} r
            WHERE (r.source_entity IN ({frontier_ids}) OR r.target_entity IN ({frontier_ids}))
            {book_filter}
        """).collect()

        evidence_count += len(neighbors)
        next_frontier: set[str] = set()
        for row in neighbors:
            nid = row["neighbor_id"]
            if nid not in visited:
                visited.add(nid)
                next_frontier.add(nid)
        current_frontier = next_frontier

    density = round(evidence_count / len(visited), 2) if visited else 0
    exhausted = len(current_frontier) == 0

    return json.dumps({
        "entity": entity_name,
        "max_depth": max_depth,
        "nodes_visited": len(visited),
        "frontier_size": len(current_frontier),
        "evidence_edges": evidence_count,
        "evidence_density": density,
        "exhausted": exhausted,
        "status": "ALL_REACHABLE_NODES_TRAVERSED" if exhausted else "FRONTIER_REMAINING",
    })


_graph_exhaustion_check_i = _instrument_tool(_graph_exhaustion_check, "graph_exhaustion_check")


@tool
def graph_exhaustion_check(entity_name: str, max_depth: int = 3) -> str:
    """Check graph traversal completeness from a starting entity.

    Returns a reachability report: nodes visited, frontier size, evidence density,
    and whether all reachable nodes have been traversed. Use this when the attorney
    workflow needs to confirm a search thread is exhausted.

    Args:
        entity_name: The entity to start the reachability check from (e.g., "Moses")
        max_depth: Maximum BFS depth (default 3)
    """
    return _graph_exhaustion_check_i(entity_name, max_depth)


GRAPH_TOOLS = [find_entity, find_connections, trace_path, get_source_evidence, get_entity_summary, graph_exhaustion_check]

# COMMAND ----------

# DBTITLE 1,Query Entity Pre-Lookup
import json
import re
import logging

_prelookup_log = logging.getLogger(__name__)

_BIBLE_QUERY_ENTITY_PROMPT = """You are an expert biblical scholar. Extract all significant entities and concepts from the following user question.

For each entity, provide:
- name: The canonical name (e.g., Abraham not Abram unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept (treat God/Lord as Person)

Rules:
- Use canonical biblical names consistently
- Include divine figures (God, Lord, Holy Spirit) as Person type
- Include non-biblical terms exactly as the user stated them (e.g., "Arabs" stays "Arabs")
- Extract ALL nouns that could refer to entities, even if uncertain whether they appear in the Bible

Return a JSON array of objects, each with "name" and "entity_type" keys. Return ONLY the JSON array, no other text.

Question:
"""

_CORPORATE_QUERY_ENTITY_PROMPT = """You are a corporate communications analyst. Extract all significant entities and concepts from the following user question about the Enron email corpus.

For each entity, provide:
- name: The canonical name (e.g., "Kenneth Lay" not "Ken"; "Enron Broadband Services" not "broadband")
- entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

Rules:
- Use full canonical names for people when possible
- NEVER use title prefixes (Dr., Mr., Mrs.) — just the bare name
- Include company and division names as stated by the user
- Extract ALL nouns that could refer to entities in a corporate context
- Terms like "executives", "leadership", "management" should be extracted as Group-type concepts

Return a JSON array of objects, each with "name" and "entity_type" keys. Return ONLY the JSON array, no other text.

Question:
"""

QUERY_ENTITY_PROMPT = _BIBLE_QUERY_ENTITY_PROMPT


def _slugify(name: str) -> str:
    """Same normalisation used during corpus build (src/extraction/extraction.py)."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def extract_query_entities(question: str, corpus: str = "bible") -> list[dict]:
    """Call the small LLM to extract entity mentions from a user question."""
    from databricks_langchain import ChatDatabricks
    prompt = _CORPORATE_QUERY_ENTITY_PROMPT if corpus == "enron" else _BIBLE_QUERY_ENTITY_PROMPT
    llm = ChatDatabricks(endpoint=config['small_llm_endpoint'], temperature=0.0, max_tokens=512)
    response = llm.invoke(prompt + question)
    text = response.content.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        entities = json.loads(text)
        if isinstance(entities, list):
            return [e for e in entities if isinstance(e, dict) and "name" in e]
    except json.JSONDecodeError:
        _prelookup_log.warning("Failed to parse entity extraction response: %s", text)
    return []


def pre_lookup_entities(entity_names: list[str]) -> tuple[list[str], list[str]]:
    """Look up extracted query entities against the graph.

    Returns (found, not_found) where each is a list of display strings.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    found: list[str] = []
    not_found: list[str] = []

    for name in entity_names:
        eid = _slugify(name)
        rows = spark.sql(f"""
            SELECT name, entity_type
            FROM {config['entities_table']}
            WHERE entity_id LIKE '%{eid}%'
            LIMIT 3
        """).collect()
        if rows:
            matches = ", ".join(f"{r['name']} ({r['entity_type']})" for r in rows)
            found.append(f"{name} -> {matches}")
        else:
            not_found.append(name)

    return found, not_found


def build_prelookup_context(question: str, corpus: str = "bible") -> str:
    """Run entity extraction + graph lookup and return a system-prompt appendix.

    Returns an empty string when extraction finds nothing or fails.
    """
    try:
        entities = extract_query_entities(question, corpus=corpus)
        if not entities:
            return ""
        names = [e["name"] for e in entities]
        found, not_found = pre_lookup_entities(names)
    except Exception:
        _prelookup_log.exception("Entity pre-lookup failed; proceeding without constraint")
        return ""

    found_str = "; ".join(found) if found else "(none)"
    not_found_str = ", ".join(not_found) if not_found else "(none)"

    return (
        "\n\n---\n"
        "PRE-LOOKUP RESULTS (DEFINITIVE — produced by an automated system, not the user):\n"
        f"  FOUND IN GRAPH: {found_str}\n"
        f"  NOT IN GRAPH: {not_found_str}\n"
        "Any answer that makes claims about entities listed under \"NOT IN GRAPH\" is WRONG.\n"
        "---"
    )

# COMMAND ----------

# DBTITLE 1,Scoped Tool Factory
def build_scoped_tools(permitted_books: list):
    """Create a set of graph tools scoped to a specific document set.

    The LLM sees the same tool signatures (no permitted_books argument exposed).
    Filtering is enforced at the SQL layer via closure.

    Args:
        permitted_books: List of book names the user is permitted to access.
                         e.g. ["Genesis", "Exodus", "Matthew"]

    Returns:
        List of 5 LangChain tools with document-scoped queries.
    """
    _fe = _instrument_tool(lambda n: _find_entity(n, permitted_books=permitted_books), "find_entity")
    _fc = _instrument_tool(lambda n: _find_connections(n, permitted_books=permitted_books), "find_connections")
    _tp = _instrument_tool(lambda a, b: _trace_path(a, b, permitted_books=permitted_books), "trace_path")
    _se = _instrument_tool(lambda n, bk="": _get_source_evidence(n, bk, permitted_books=permitted_books), "get_source_evidence")
    _es = _instrument_tool(lambda n: _get_entity_summary(n, permitted_books=permitted_books), "get_entity_summary")
    _gec = _instrument_tool(
        lambda n, d=3: _graph_exhaustion_check(n, d, permitted_books=permitted_books),
        "graph_exhaustion_check",
    )

    @tool
    def find_entity(name: str) -> str:
        """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
        Use this when the user asks about a specific person, place, event, or concept.

        Args:
            name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
        """
        return _fe(name)

    @tool
    def find_connections(entity_name: str) -> str:
        """Find all relationships involving a given entity — both as source and target.
        Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

        Args:
            entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
        """
        return _fc(entity_name)

    @tool
    def trace_path(entity_a: str, entity_b: str) -> str:
        """Find the shortest path between two entities using pre-computed GraphFrames BFS.
        Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

        Args:
            entity_a: Starting entity name (e.g., "Ruth")
            entity_b: Ending entity name (e.g., "Jesus")
        """
        return _tp(entity_a, entity_b)

    @tool
    def get_source_evidence(entity_name: str, book: str = "") -> str:
        """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

        Args:
            entity_name: The entity name to find verses for (e.g., "Moses")
            book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
        """
        return _se(entity_name, book)

    @tool
    def get_entity_summary(entity_name: str) -> str:
        """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
        Use this for broad questions about who someone is or what role they play.

        Args:
            entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
        """
        return _es(entity_name)

    @tool
    def graph_exhaustion_check(entity_name: str, max_depth: int = 3) -> str:
        """Check graph traversal completeness from a starting entity.

        Args:
            entity_name: The entity to start the reachability check from
            max_depth: Maximum BFS depth (default 3)
        """
        return _gec(entity_name, max_depth)

    return [find_entity, find_connections, trace_path, get_source_evidence, get_entity_summary, graph_exhaustion_check]

# COMMAND ----------

# DBTITLE 1,Corpus Table Config
def _get_corpus_tables(corpus: str = "bible") -> dict:
    """Return the table config dict for a given corpus."""
    if corpus == "enron":
        return {
            "entities": config['enron_entities_table'],
            "relationships": config['enron_relationships_table'],
            "entity_mentions": config['enron_entity_mentions_table'],
            "entity_analytics": config['enron_entity_analytics_table'],
            "entity_paths": config['enron_entity_paths_table'],
            "source_table": config['enron_emails_table'],
            "source_type": "email",
        }
    return {
        "entities": config['entities_table'],
        "relationships": config['relationships_table'],
        "entity_mentions": config['entity_mentions_table'],
        "entity_analytics": config['entity_analytics_table'],
        "entity_paths": config['entity_paths_table'],
        "source_table": config['verses_table'],
        "source_type": "verse",
    }

# COMMAND ----------

# DBTITLE 1,Enron: Get Source Emails Tool
def _parse_search_terms(entity_name: str) -> list:
    """Split 'A AND B' into multiple search terms; returns single-element list otherwise."""
    if " AND " in entity_name:
        return [t.strip() for t in entity_name.split(" AND ") if t.strip()]
    return [entity_name]


def _get_source_emails(entity_name: str, thread_id: str = "") -> str:
    """Get actual Enron emails that mention a specific entity. Provides source text for grounding answers.

    Supports 'A AND B' syntax to find emails mentioning both entities.

    Args:
        entity_name: The entity name to find emails for (e.g., "Kenneth Lay",
            or "Kathy Dodgen AND Kenneth Lay" for emails mentioning both)
        thread_id: Optional — filter to a specific thread. Leave empty for all threads.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    terms = _parse_search_terms(entity_name)
    like_clauses = " AND ".join(f"e.body LIKE '%{t}%'" for t in terms)
    thread_filter = f"AND e.thread_id = '{thread_id}'" if thread_id else ""

    results = spark.sql(f"""
        SELECT e.date, e.sender, e.subject, SUBSTRING(e.body, 1, 500) as body_preview,
               COALESCE(SIZE(e.to_recipients), 0) + COALESCE(SIZE(e.cc_recipients), 0) as recipient_count
        FROM {config['enron_emails_table']} e
        WHERE {like_clauses}
        {thread_filter}
        ORDER BY e.date DESC
        LIMIT 10
    """).collect()

    search_desc = " AND ".join(terms)
    if not results:
        return f"No emails found mentioning '{search_desc}'."

    lines = [f"Emails mentioning '{search_desc}' ({len(results)} found):"]
    for r in results:
        date_str = str(r['date'])[:10] if r['date'] else "unknown date"
        rc = r['recipient_count'] if _row_get(r, 'recipient_count') else 0
        email_type = "direct" if rc <= 5 else "group" if rc <= 20 else "mass"
        lines.append(f"  [{date_str}] From: {r['sender']} | Subject: {r['subject']} | [{email_type}, {rc} recipients]")
        lines.append(f"    {r['body_preview']}...")
    return "\n".join(lines)

# COMMAND ----------

# DBTITLE 1,Corpus-Aware Tool Factory
def build_corpus_tools(corpus: str = "bible"):
    """Create a set of graph tools for a specific corpus (bible or enron).

    The LLM sees the same tool signatures regardless of corpus.
    Table references are resolved via closure.

    Args:
        corpus: Either "bible" or "enron".

    Returns:
        List of 5 LangChain tools targeting the specified corpus tables.
    """
    tables = _get_corpus_tables(corpus)

    @tool
    def find_entity(name: str) -> str:
        """Search for an entity by name. Returns matching entities with their type and description.

        Args:
            name: The name to search for (e.g., "Moses", "Kenneth Lay")
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        results = spark.sql(f"""
            SELECT name, entity_type, description
            FROM {tables['entities']}
            WHERE LOWER(name) LIKE LOWER('%{name}%')
            ORDER BY name
            LIMIT 10
        """).collect()
        if not results:
            return f"No entity found matching '{name}'."
        return json.dumps([{
            "name": r["name"], "type": r["entity_type"], "description": r["description"],
        } for r in results], ensure_ascii=False)

    @tool
    def find_connections(entity_name: str) -> str:
        """Find all relationships involving a given entity — both as source and target.

        Args:
            entity_name: The entity name to find connections for
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        entity_id = "_".join(entity_name.lower().split())
        freq_expr = "SUM(COALESCE(edge_count, 1))" if corpus == "enron" else "COUNT(*)"
        edge_col = ", r.edge_count" if corpus == "enron" else ""
        results = spark.sql(f"""
            SELECT source_name, relationship_type, target_name,
                   MAX(description) as description,
                   {freq_expr} as frequency
            FROM (
                SELECT
                    COALESCE(e1.name, r.source_entity) as source_name,
                    r.relationship_type,
                    COALESCE(e2.name, r.target_entity) as target_name,
                    r.description{edge_col}
                FROM {tables['relationships']} r
                LEFT JOIN {tables['entities']} e1 ON r.source_entity = e1.entity_id
                LEFT JOIN {tables['entities']} e2 ON r.target_entity = e2.entity_id
                WHERE r.source_entity LIKE '%{entity_id}%'
                   OR r.target_entity LIKE '%{entity_id}%'
            ) sub
            GROUP BY source_name, relationship_type, target_name
            ORDER BY frequency DESC
            LIMIT 100
        """).collect()
        if not results:
            return f"No connections found for '{entity_name}'."
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            entry = {
                "source": r["source_name"], "target": r["target_name"],
                "description": r["description"],
            }
            freq = _row_get(r, "frequency")
            if freq is not None:
                try:
                    entry["frequency"] = int(freq)
                except (ValueError, TypeError):
                    pass
            groups[r["relationship_type"]].append(entry)
        for rel_type in groups:
            groups[rel_type].sort(key=lambda e: e.get("frequency", 0), reverse=True)
        return json.dumps({
            "entity": entity_name, "total": len(results),
            "by_type": dict(groups),
        }, ensure_ascii=False)

    @tool
    def trace_path(entity_a: str, entity_b: str) -> str:
        """Find the shortest path between two entities.

        Args:
            entity_a: Starting entity name
            entity_b: Ending entity name
        """
        import re as _re
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        def _slug(n):
            return _re.sub(r'[^a-z0-9]+', '_', n.lower()).strip('_')
        id_a, id_b = _slug(entity_a), _slug(entity_b)
        paths = spark.sql(f"""
            SELECT p.source_id, p.target_id, p.distance, p.path_names,
                   e1.name as source_name, e2.name as target_name
            FROM {tables['entity_paths']} p
            LEFT JOIN {tables['entities']} e1 ON p.source_id = e1.entity_id
            LEFT JOIN {tables['entities']} e2 ON p.target_id = e2.entity_id
            WHERE (p.source_id LIKE '%{id_a}%' AND p.target_id LIKE '%{id_b}%')
               OR (p.source_id LIKE '%{id_b}%' AND p.target_id LIKE '%{id_a}%')
            ORDER BY p.distance
            LIMIT 5
        """).collect()
        if not paths:
            return f"No path found between '{entity_a}' and '{entity_b}' in the knowledge graph."
        return json.dumps({
            "from": entity_a, "to": entity_b,
            "paths": [{"source": r["source_name"], "target": r["target_name"],
                        "distance": r["distance"], "path_names": r["path_names"]}
                       for r in paths],
        }, ensure_ascii=False)

    if corpus == "enron":
        @tool
        def get_source_context(entity_name: str, thread_id: str = "") -> str:
            """Get actual Enron emails mentioning an entity. Provides source text for grounding answers.

            Args:
                entity_name: The entity name to find emails for (e.g., "Kenneth Lay")
                thread_id: Optional — filter to a specific thread. Leave empty for all.
            """
            return _get_source_emails(entity_name, thread_id)
    else:
        @tool
        def get_source_context(entity_name: str, book: str = "") -> str:
            """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

            Args:
                entity_name: The entity name to find verses for (e.g., "Moses")
                book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
            """
            return _get_source_evidence(entity_name, book)

    @tool
    def get_entity_summary(entity_name: str) -> str:
        """Get a comprehensive profile of an entity: type, description, and all relationships.

        Args:
            entity_name: The entity to summarize
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        entity_id = "_".join(entity_name.lower().split())
        entity_rows = spark.sql(f"""
            SELECT name, entity_type, description
            FROM {tables['entities']}
            WHERE entity_id LIKE '%{entity_id}%'
            LIMIT 1
        """).collect()
        if not entity_rows:
            return f"Entity '{entity_name}' not found in the knowledge graph."
        ent = entity_rows[0]
        summary = {
            "name": ent["name"], "type": ent["entity_type"],
            "description": ent["description"],
        }
        rels = spark.sql(f"""
            SELECT COALESCE(e1.name, r.source_entity) as src,
                   r.relationship_type,
                   COALESCE(e2.name, r.target_entity) as tgt,
                   r.description
            FROM {tables['relationships']} r
            LEFT JOIN {tables['entities']} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {tables['entities']} e2 ON r.target_entity = e2.entity_id
            WHERE r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%'
            LIMIT 20
        """).collect()
        if rels:
            from collections import defaultdict
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in rels:
                groups[r["relationship_type"]].append({
                    "source": r["src"], "target": r["tgt"], "description": r["description"],
                })
            summary["relationships"] = {"total": len(rels), "by_type": dict(groups)}
        return json.dumps(summary, ensure_ascii=False)

    _gec_corpus = _instrument_tool(
        lambda n, d=3: _graph_exhaustion_check(n, d, tables=tables),
        "graph_exhaustion_check",
    )

    @tool
    def graph_exhaustion_check(entity_name: str, max_depth: int = 3) -> str:
        """Check graph traversal completeness from a starting entity.

        Args:
            entity_name: The entity to start the reachability check from
            max_depth: Maximum BFS depth (default 3)
        """
        return _gec_corpus(entity_name, max_depth)

    return [find_entity, find_connections, trace_path, get_source_context, get_entity_summary, graph_exhaustion_check]

# COMMAND ----------

# DBTITLE 1,ABAC Table Config
def _get_abac_tables(tier: str = "legal_team") -> dict:
    """Return the ABAC view config dict for the Enron corpus.

    When Unity Catalog row filters are active, the ABAC views transparently
    restrict results based on the calling user's group.  The tier parameter
    is recorded for logging but does not change the SQL — the UC row filter
    handles enforcement.
    """
    return {
        "entities": config['enron_abac_entities_view'],
        "relationships": config['enron_abac_relationships_view'],
        "entity_mentions": config['enron_abac_entity_mentions_view'],
        "entity_analytics": config['enron_abac_entity_analytics_view'],
        "entity_paths": config['enron_abac_entity_paths_view'],
        "source_table": config['enron_abac_emails_view'],
        "source_type": "email",
        "tier": tier,
    }

# COMMAND ----------

# DBTITLE 1,ABAC-Aware Tool Factory
def build_abac_tools(tier: str = "legal_team"):
    """Create Enron graph tools that query through ABAC views.

    The UC row filter on the emails table cascades into the views, so
    no application-level sensitivity filtering is needed.  The tier
    argument is metadata for logging; actual enforcement is at the
    SQL engine level via is_account_group_member().

    Args:
        tier: Access tier name (legal_team, executive_team, analyst_team).
              Included in tool output for audit trail.

    Returns:
        List of 5 LangChain tools targeting the ABAC views.
    """
    tables = _get_abac_tables(tier)

    @tool
    def find_entity(name: str) -> str:
        """Search for an entity by name in the access-controlled knowledge graph.

        Args:
            name: The name to search for (e.g., "Kenneth Lay", "Enron")
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        results = spark.sql(f"""
            SELECT name, entity_type, description
            FROM {tables['entities']}
            WHERE LOWER(name) LIKE LOWER('%{name}%')
            ORDER BY name
            LIMIT 10
        """).collect()
        if not results:
            return f"No entity found matching '{name}' at your access level."
        return json.dumps([{
            "name": r["name"], "type": r["entity_type"], "description": r["description"],
            "access_tier": tier,
        } for r in results], ensure_ascii=False)

    @tool
    def find_connections(entity_name: str) -> str:
        """Find relationships involving an entity in the access-controlled graph.

        Args:
            entity_name: The entity name to find connections for
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        entity_id = "_".join(entity_name.lower().split())
        results = spark.sql(f"""
            SELECT
                COALESCE(e1.name, r.source_entity) as source_name,
                r.relationship_type,
                COALESCE(e2.name, r.target_entity) as target_name,
                r.description
            FROM {tables['relationships']} r
            LEFT JOIN {tables['entities']} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {tables['entities']} e2 ON r.target_entity = e2.entity_id
            WHERE r.source_entity LIKE '%{entity_id}%'
               OR r.target_entity LIKE '%{entity_id}%'
            LIMIT 30
        """).collect()
        if not results:
            return f"No connections found for '{entity_name}' at your access level."
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            groups[r["relationship_type"]].append({
                "source": r["source_name"], "target": r["target_name"],
                "description": r["description"],
            })
        return json.dumps({
            "entity": entity_name, "total": len(results),
            "by_type": dict(groups), "access_tier": tier,
        }, ensure_ascii=False)

    @tool
    def trace_path(entity_a: str, entity_b: str) -> str:
        """Find the shortest path between two entities in the access-controlled graph.

        Args:
            entity_a: Starting entity name
            entity_b: Ending entity name
        """
        import re as _re
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        def _slug(n):
            return _re.sub(r'[^a-z0-9]+', '_', n.lower()).strip('_')
        id_a, id_b = _slug(entity_a), _slug(entity_b)
        paths = spark.sql(f"""
            SELECT p.source_id, p.target_id, p.distance, p.path_names,
                   e1.name as source_name, e2.name as target_name
            FROM {tables['entity_paths']} p
            LEFT JOIN {tables['entities']} e1 ON p.source_id = e1.entity_id
            LEFT JOIN {tables['entities']} e2 ON p.target_id = e2.entity_id
            WHERE (p.source_id LIKE '%{id_a}%' AND p.target_id LIKE '%{id_b}%')
               OR (p.source_id LIKE '%{id_b}%' AND p.target_id LIKE '%{id_a}%')
            ORDER BY p.distance
            LIMIT 5
        """).collect()
        if not paths:
            return (
                f"No path found between '{entity_a}' and '{entity_b}' "
                f"at your access level ({tier}). The path may exist in the full "
                f"graph but passes through restricted entities."
            )
        return json.dumps({
            "from": entity_a, "to": entity_b, "access_tier": tier,
            "paths": [{"source": r["source_name"], "target": r["target_name"],
                        "distance": r["distance"], "path_names": r["path_names"]}
                       for r in paths],
        }, ensure_ascii=False)

    @tool
    def get_source_context(entity_name: str, thread_id: str = "") -> str:
        """Get Enron emails mentioning an entity (filtered by access tier).

        Args:
            entity_name: The entity name to find emails for
            thread_id: Optional thread filter
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        thread_filter = f"AND e.thread_id = '{thread_id}'" if thread_id else ""
        results = spark.sql(f"""
            SELECT e.date, e.sender, e.subject, SUBSTRING(e.body, 1, 500) as body_preview
            FROM {tables['source_table']} e
            WHERE e.body LIKE '%{entity_name}%'
            {thread_filter}
            ORDER BY e.date DESC
            LIMIT 10
        """).collect()
        if not results:
            return f"No emails found mentioning '{entity_name}' at your access level."
        emails = []
        for r in results:
            emails.append({
                "date": str(r["date"])[:10] if r["date"] else "unknown",
                "sender": r["sender"], "subject": r["subject"],
                "body_preview": (r["body_preview"] or "")[:200],
            })
        return json.dumps({
            "entity": entity_name, "total": len(emails),
            "emails": emails, "access_tier": tier,
        }, ensure_ascii=False)

    @tool
    def get_entity_summary(entity_name: str) -> str:
        """Get a profile of an entity from the access-controlled graph.

        Args:
            entity_name: The entity to summarize
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        entity_id = "_".join(entity_name.lower().split())
        entity_rows = spark.sql(f"""
            SELECT name, entity_type, description
            FROM {tables['entities']}
            WHERE entity_id LIKE '%{entity_id}%'
            LIMIT 1
        """).collect()
        if not entity_rows:
            return f"Entity '{entity_name}' not found at your access level ({tier})."
        ent = entity_rows[0]
        summary = {
            "name": ent["name"], "type": ent["entity_type"],
            "description": ent["description"], "access_tier": tier,
        }
        rels = spark.sql(f"""
            SELECT COALESCE(e1.name, r.source_entity) as src,
                   r.relationship_type,
                   COALESCE(e2.name, r.target_entity) as tgt,
                   r.description
            FROM {tables['relationships']} r
            LEFT JOIN {tables['entities']} e1 ON r.source_entity = e1.entity_id
            LEFT JOIN {tables['entities']} e2 ON r.target_entity = e2.entity_id
            WHERE r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%'
            LIMIT 20
        """).collect()
        if rels:
            from collections import defaultdict
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in rels:
                groups[r["relationship_type"]].append({
                    "source": r["src"], "target": r["tgt"], "description": r["description"],
                })
            summary["relationships"] = {"total": len(rels), "by_type": dict(groups)}
        return json.dumps(summary, ensure_ascii=False)

    _gec_abac = _instrument_tool(
        lambda n, d=3: _graph_exhaustion_check(n, d, tables=tables),
        "graph_exhaustion_check",
    )

    @tool
    def graph_exhaustion_check(entity_name: str, max_depth: int = 3) -> str:
        """Check graph traversal completeness from a starting entity in the access-controlled graph.

        Args:
            entity_name: The entity to start the reachability check from
            max_depth: Maximum BFS depth (default 3)
        """
        return _gec_abac(entity_name, max_depth)

    return [find_entity, find_connections, trace_path, get_source_context, get_entity_summary, graph_exhaustion_check]
