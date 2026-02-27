"""Sync entities and relationships from parsed results into Neo4j."""

from __future__ import annotations

from neo4j import Driver

from graphrag.ingest.models import Entity, EntityType, Relationship

_ENTITY_QUERIES: dict[EntityType, str] = {
    EntityType.MODULE: (
        "MERGE (n:Entity:Module {qualified_name: $qualified_name}) "
        "SET n.name = $name, n.entity_type = $entity_type, "
        "n.file_path = $file_path, n.start_line = $start_line, "
        "n.end_line = $end_line, n.docstring = $docstring"
    ),
    EntityType.CLASS: (
        "MERGE (n:Entity:Class {qualified_name: $qualified_name}) "
        "SET n.name = $name, n.entity_type = $entity_type, "
        "n.file_path = $file_path, n.start_line = $start_line, "
        "n.end_line = $end_line, n.docstring = $docstring"
    ),
    EntityType.FUNCTION: (
        "MERGE (n:Entity:Function {qualified_name: $qualified_name}) "
        "SET n.name = $name, n.entity_type = $entity_type, "
        "n.file_path = $file_path, n.start_line = $start_line, "
        "n.end_line = $end_line, n.docstring = $docstring"
    ),
    EntityType.METHOD: (
        "MERGE (n:Entity:Method {qualified_name: $qualified_name}) "
        "SET n.name = $name, n.entity_type = $entity_type, "
        "n.file_path = $file_path, n.start_line = $start_line, "
        "n.end_line = $end_line, n.docstring = $docstring"
    ),
}

_MERGE_RELATIONSHIP = """
MERGE (a:Entity {{qualified_name: $source}})
MERGE (b:Entity {{qualified_name: $target}})
MERGE (a)-[r:{rel_type}]->(b)
SET r.file_path = $file_path,
    r.line = $line
"""


def _entity_params(entity: Entity) -> dict:
    return {
        "qualified_name": entity.qualified_name,
        "name": entity.name,
        "entity_type": entity.entity_type.value,
        "file_path": entity.file_path,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
        "docstring": entity.docstring,
    }


def _rel_params(rel: Relationship) -> dict:
    return {
        "source": rel.source,
        "target": rel.target,
        "file_path": rel.file_path,
        "line": rel.line,
    }


def sync_to_neo4j(
    driver: Driver,
    entities: list[Entity],
    relationships: list[Relationship],
    database: str = "neo4j",
) -> dict[str, int]:
    """Write parsed entities and relationships into Neo4j. Returns counts."""
    entity_count = 0
    rel_count = 0

    with driver.session(database=database) as session:
        for entity in entities:
            query = _ENTITY_QUERIES[entity.entity_type]
            session.run(query, **_entity_params(entity))
            entity_count += 1

        for rel in relationships:
            query = _MERGE_RELATIONSHIP.format(rel_type=rel.rel_type)
            session.run(query, **_rel_params(rel))
            rel_count += 1

    return {"entities": entity_count, "relationships": rel_count}
