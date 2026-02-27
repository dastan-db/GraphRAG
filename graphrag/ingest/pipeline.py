"""Ingestion pipeline: walk a codebase, parse, and sync to Neo4j."""

from __future__ import annotations

import logging
from pathlib import Path

from neo4j import GraphDatabase

from graphrag.config import GraphRAGConfig
from graphrag.graph.schema import apply_schema, clear_graph
from graphrag.graph.sync import sync_to_neo4j
from graphrag.ingest.models import Entity, Relationship
from graphrag.ingest.parser import PythonParser

logger = logging.getLogger(__name__)


class IngestPipeline:
    """Parses a Python codebase and loads the knowledge graph into Neo4j."""

    def __init__(self, config: GraphRAGConfig):
        self.config = config
        self._parser = PythonParser()
        self._driver = GraphDatabase.driver(
            config.neo4j.uri,
            auth=(config.neo4j.username, config.neo4j.password) if config.neo4j.password else None,
        )

    @classmethod
    def from_config(cls, config: GraphRAGConfig | None = None) -> IngestPipeline:
        return cls(config or GraphRAGConfig())

    def run(
        self,
        codebase_path: str | Path,
        *,
        clear: bool = True,
    ) -> dict:
        """Run the full ingestion pipeline.

        Args:
            codebase_path: Root directory of the Python codebase to ingest.
            clear: If True, wipe the existing graph before ingesting.

        Returns:
            Summary dict with entity/relationship counts.
        """
        root = Path(codebase_path).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        logger.info("Ingesting codebase at %s", root)

        all_entities, all_relationships = self._parse_codebase(root)

        logger.info(
            "Parsed %d entities and %d relationships",
            len(all_entities),
            len(all_relationships),
        )

        db = self.config.neo4j.database
        if clear:
            clear_graph(self._driver, database=db)
        apply_schema(self._driver, database=db)

        counts = sync_to_neo4j(
            self._driver,
            all_entities,
            all_relationships,
            database=db,
        )

        logger.info("Synced to Neo4j: %s", counts)
        return {
            "codebase": str(root),
            "entities_parsed": len(all_entities),
            "relationships_parsed": len(all_relationships),
            **counts,
        }

    def _parse_codebase(
        self,
        root: Path,
    ) -> tuple[list[Entity], list[Relationship]]:
        all_entities: list[Entity] = []
        all_relationships: list[Relationship] = []

        py_files = sorted(root.rglob("*.py"))
        for py_file in py_files:
            if "__pycache__" in str(py_file):
                continue
            logger.debug("Parsing %s", py_file)
            entities, relationships = self._parser.parse_file(py_file, root_dir=root)
            all_entities.extend(entities)
            all_relationships.extend(relationships)

        return all_entities, all_relationships

    def close(self) -> None:
        self._driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
