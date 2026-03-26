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

from langchain_core.tools import tool
from functools import partial

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
        ORDER BY r.book, r.chapter
        LIMIT 30
    """).collect()

    if not results:
        return f"No connections found for '{entity_name}'."

    lines = [f"Connections for '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(
            f"- {r['source_name']} --[{r['relationship_type']}]--> {r['target_name']}: "
            f"{r['description']} ({r['book']} ch.{r['chapter']})"
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
def _get_context_verses(entity_name: str, book: str = "", permitted_books: list = None) -> str:
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

# DBTITLE 1,Tool Registration — Default (Unscoped) Tools
@tool
def find_entity(name: str) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    return _find_entity(name)

@tool
def find_connections(entity_name: str) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    return _find_connections(entity_name)

@tool
def trace_path(entity_a: str, entity_b: str) -> str:
    """Find the shortest path between two entities using pre-computed GraphFrames BFS.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    return _trace_path(entity_a, entity_b)

@tool
def get_context_verses(entity_name: str, book: str = "") -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    return _get_context_verses(entity_name, book)

@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    return _get_entity_summary(entity_name)

GRAPH_TOOLS = [find_entity, find_connections, trace_path, get_context_verses, get_entity_summary]

# COMMAND ----------

# DBTITLE 1,Query Entity Pre-Lookup
import json
import re
import logging

_prelookup_log = logging.getLogger(__name__)

QUERY_ENTITY_PROMPT = """You are an expert biblical scholar. Extract all significant entities and concepts from the following user question.

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


def _slugify(name: str) -> str:
    """Same normalisation used during corpus build (src/extraction/extraction.py)."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def extract_query_entities(question: str) -> list[dict]:
    """Call the small LLM to extract entity mentions from a user question."""
    from databricks_langchain import ChatDatabricks
    llm = ChatDatabricks(endpoint=config['small_llm_endpoint'], temperature=0.0, max_tokens=512)
    response = llm.invoke(QUERY_ENTITY_PROMPT + question)
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


def build_prelookup_context(question: str) -> str:
    """Run entity extraction + graph lookup and return a system-prompt appendix.

    Returns an empty string when extraction finds nothing or fails.
    """
    try:
        entities = extract_query_entities(question)
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
    @tool
    def find_entity(name: str) -> str:
        """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
        Use this when the user asks about a specific person, place, event, or concept.

        Args:
            name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
        """
        return _find_entity(name, permitted_books=permitted_books)

    @tool
    def find_connections(entity_name: str) -> str:
        """Find all relationships involving a given entity — both as source and target.
        Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

        Args:
            entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
        """
        return _find_connections(entity_name, permitted_books=permitted_books)

    @tool
    def trace_path(entity_a: str, entity_b: str) -> str:
        """Find the shortest path between two entities using pre-computed GraphFrames BFS.
        Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

        Args:
            entity_a: Starting entity name (e.g., "Ruth")
            entity_b: Ending entity name (e.g., "Jesus")
        """
        return _trace_path(entity_a, entity_b, permitted_books=permitted_books)

    @tool
    def get_context_verses(entity_name: str, book: str = "") -> str:
        """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

        Args:
            entity_name: The entity name to find verses for (e.g., "Moses")
            book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
        """
        return _get_context_verses(entity_name, book, permitted_books=permitted_books)

    @tool
    def get_entity_summary(entity_name: str) -> str:
        """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
        Use this for broad questions about who someone is or what role they play.

        Args:
            entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
        """
        return _get_entity_summary(entity_name, permitted_books=permitted_books)

    return [find_entity, find_connections, trace_path, get_context_verses, get_entity_summary]

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
def _get_source_emails(entity_name: str, thread_id: str = "") -> str:
    """Get actual Enron emails that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find emails for (e.g., "Kenneth Lay")
        thread_id: Optional — filter to a specific thread. Leave empty for all threads.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    thread_filter = f"AND e.thread_id = '{thread_id}'" if thread_id else ""

    results = spark.sql(f"""
        SELECT e.date, e.sender, e.subject, SUBSTRING(e.body, 1, 500) as body_preview
        FROM {config['enron_emails_table']} e
        WHERE e.body LIKE '%{entity_name}%'
        {thread_filter}
        ORDER BY e.date DESC
        LIMIT 10
    """).collect()

    if not results:
        return f"No emails found mentioning '{entity_name}'."

    lines = [f"Emails mentioning '{entity_name}' ({len(results)} found):"]
    for r in results:
        date_str = str(r['date'])[:10] if r['date'] else "unknown date"
        lines.append(f"  [{date_str}] From: {r['sender']} | Subject: {r['subject']}")
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
        lines = []
        for r in results:
            lines.append(f"- **{r['name']}** ({r['entity_type']}): {r['description']}")
        return "\n".join(lines)

    @tool
    def find_connections(entity_name: str) -> str:
        """Find all relationships involving a given entity — both as source and target.

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
            return f"No connections found for '{entity_name}'."
        lines = [f"Connections for '{entity_name}' ({len(results)} found):"]
        for r in results:
            lines.append(
                f"- {r['source_name']} --[{r['relationship_type']}]--> {r['target_name']}: "
                f"{r['description']}"
            )
        return "\n".join(lines)

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
        lines = [f"Shortest paths between {entity_a} and {entity_b}:"]
        for r in paths:
            lines.append(f"  {r['source_name']} -> {r['target_name']}: distance={r['distance']}, path: {r['path_names']}")
        return "\n".join(lines)

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
            return _get_context_verses(entity_name, book)

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
        lines = [
            f"**{ent['name']}** ({ent['entity_type']})",
            f"Description: {ent['description']}",
        ]
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
            lines.append(f"\nKey relationships ({len(rels)}):")
            for r in rels:
                lines.append(f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: {r['description']}")
        return "\n".join(lines)

    return [find_entity, find_connections, trace_path, get_source_context, get_entity_summary]
