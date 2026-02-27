"""Neo4j schema definition: node labels, relationship types, and constraints."""

from __future__ import annotations

from neo4j import Driver

NODE_LABELS = ("Module", "Class", "Function", "Method")

RELATIONSHIP_TYPES = ("IMPORTS", "CALLS", "INHERITS", "CONTAINS", "INSTANTIATES")

CONSTRAINTS = [
    "CREATE CONSTRAINT entity_qualified_name IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.qualified_name IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX entity_name_idx IF NOT EXISTS FOR (n:Entity) ON (n.name)",
    "CREATE INDEX entity_file_idx IF NOT EXISTS FOR (n:Entity) ON (n.file_path)",
]


def apply_schema(driver: Driver, database: str = "neo4j") -> None:
    """Create constraints and indexes in Neo4j. Idempotent."""
    with driver.session(database=database) as session:
        for stmt in CONSTRAINTS + INDEXES:
            session.run(stmt)


def clear_graph(driver: Driver, database: str = "neo4j") -> None:
    """Delete all nodes and relationships. Use only in dev/test."""
    with driver.session(database=database) as session:
        session.run("MATCH (n) DETACH DELETE n")
