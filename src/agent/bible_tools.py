"""Bible-focused graph tools surfaced from the serving core."""

from src.agent.agent_serving import (
    build_scoped_tools_local,
    compare_entity_sets,
    find_connections,
    find_cross_book_entities,
    find_entity,
    get_entity_summary,
    get_relationship_evidence,
    get_source_evidence,
    list_entities_by_book,
    trace_path,
)

__all__ = [
    "build_scoped_tools_local",
    "compare_entity_sets",
    "find_connections",
    "find_cross_book_entities",
    "find_entity",
    "get_entity_summary",
    "get_relationship_evidence",
    "get_source_evidence",
    "list_entities_by_book",
    "trace_path",
]
