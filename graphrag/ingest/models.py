from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EntityType(str, Enum):
    MODULE = "Module"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"


class RelationshipType(str, Enum):
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INHERITS = "INHERITS"
    CONTAINS = "CONTAINS"
    INSTANTIATES = "INSTANTIATES"


@dataclass(frozen=True)
class Entity:
    name: str
    qualified_name: str
    entity_type: EntityType
    file_path: str
    start_line: int
    end_line: int
    docstring: str = ""
    decorators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return self.entity_type.value


@dataclass(frozen=True)
class Relationship:
    source: str
    target: str
    relationship_type: RelationshipType
    file_path: str
    line: int

    @property
    def rel_type(self) -> str:
        return self.relationship_type.value
