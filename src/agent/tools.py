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

# DBTITLE 1,Tool: Trace Path
def _trace_path(entity_a: str, entity_b: str, permitted_books: list = None) -> str:
    """Find how two entities are connected, tracing up to 3 hops through the knowledge graph.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    id_a = "_".join(entity_a.lower().split())
    id_b = "_".join(entity_b.lower().split())
    books_clause = _books_in_clause(permitted_books)

    # Book filter for each relationship alias
    def bf(alias):
        return f"AND {alias}.book IN {books_clause}" if books_clause else ""

    # 1-hop
    direct = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type as rel,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE ((r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
           OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%'))
        {bf('r')}
    """).collect()

    if direct:
        lines = [f"Direct connection between {entity_a} and {entity_b}:"]
        for r in direct:
            lines.append(f"  {r['src']} --[{r['rel']}]--> {r['tgt']}: {r['description']} ({r['book']})")
        return "\n".join(lines)

    # 2-hop
    two_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_mid.name, r1.target_entity) as mid,
               r2.relationship_type as rel2,
               COALESCE(e2.name, r2.target_entity) as tgt
        FROM {config['relationships_table']} r1
        JOIN {config['relationships_table']} r2 ON r1.target_entity = r2.source_entity
        LEFT JOIN {config['entities_table']} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e_mid ON r1.target_entity = e_mid.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r2.target_entity = e2.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r2.target_entity LIKE '%{id_b}%'
        {bf('r1')} {bf('r2')}
        LIMIT 10
    """).collect()

    if two_hop:
        lines = [f"2-hop path from {entity_a} to {entity_b}:"]
        for r in two_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid']} --[{r['rel2']}]--> {r['tgt']}")
        return "\n".join(lines)

    # 3-hop
    three_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_m1.name, r1.target_entity) as mid1,
               r2.relationship_type as rel2,
               COALESCE(e_m2.name, r2.target_entity) as mid2,
               r3.relationship_type as rel3,
               COALESCE(e3.name, r3.target_entity) as tgt
        FROM {config['relationships_table']} r1
        JOIN {config['relationships_table']} r2 ON r1.target_entity = r2.source_entity
        JOIN {config['relationships_table']} r3 ON r2.target_entity = r3.source_entity
        LEFT JOIN {config['entities_table']} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e_m1 ON r1.target_entity = e_m1.entity_id
        LEFT JOIN {config['entities_table']} e_m2 ON r2.target_entity = e_m2.entity_id
        LEFT JOIN {config['entities_table']} e3 ON r3.target_entity = e3.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r3.target_entity LIKE '%{id_b}%'
        {bf('r1')} {bf('r2')} {bf('r3')}
        LIMIT 10
    """).collect()

    if three_hop:
        lines = [f"3-hop path from {entity_a} to {entity_b}:"]
        for r in three_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid1']} --[{r['rel2']}]--> {r['mid2']} --[{r['rel3']}]--> {r['tgt']}")
        return "\n".join(lines)

    return f"No path found between '{entity_a}' and '{entity_b}' within 3 hops. Try using find_connections on each entity separately."

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
    """Find how two entities are connected, tracing up to 3 hops through the knowledge graph.
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
        """Find how two entities are connected, tracing up to 3 hops through the knowledge graph.
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
