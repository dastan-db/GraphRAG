# Databricks notebook source
# MAGIC %md
# MAGIC ### Graph Traversal Tools
# MAGIC `@tool` functions that query the knowledge graph Delta tables via Spark SQL.

# COMMAND ----------

from langchain_core.tools import tool

# COMMAND ----------

# DBTITLE 1,Tool: Find Entity
@tool
def find_entity(name: str) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
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
@tool
def find_connections(entity_name: str) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

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
        WHERE r.source_entity LIKE '%{entity_id}%'
           OR r.target_entity LIKE '%{entity_id}%'
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
@tool
def trace_path(entity_a: str, entity_b: str) -> str:
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

    # 1-hop
    direct = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type as rel,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE (r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
           OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%')
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
@tool
def get_context_verses(entity_name: str, book: str = "") -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    book_filter = f"AND v.book = '{book}'" if book else ""

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
@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

    # Entity info
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

    # Books mentioned in
    books = spark.sql(f"""
        SELECT DISTINCT book FROM {config['relationships_table']}
        WHERE source_entity LIKE '%{entity_id}%' OR target_entity LIKE '%{entity_id}%'
        ORDER BY book
    """).collect()
    if books:
        lines.append(f"Appears in: {', '.join(r['book'] for r in books)}")

    # Key relationships
    rels = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%'
        LIMIT 20
    """).collect()

    if rels:
        lines.append(f"\nKey relationships ({len(rels)}):")
        for r in rels:
            lines.append(f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: {r['description']}")

    return "\n".join(lines)

# COMMAND ----------

GRAPH_TOOLS = [find_entity, find_connections, trace_path, get_context_verses, get_entity_summary]
