"""Tree-sitter based Python parser for entity and relationship extraction."""

from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from graphrag.ingest.models import Entity, EntityType, Relationship, RelationshipType

PY_LANGUAGE = Language(tspython.language())


def _text(node: Node) -> str:
    return node.text.decode("utf-8") if node.text else ""


def _docstring(body_node: Node) -> str:
    """Extract docstring from the first statement of a body block."""
    if not body_node or body_node.child_count == 0:
        return ""
    first = body_node.children[0]
    if first.type == "expression_statement" and first.child_count > 0:
        expr = first.children[0]
        if expr.type == "string":
            raw = _text(expr)
            return raw.strip("\"'").strip()
    return ""


def _decorators(node: Node) -> tuple[str, ...]:
    """Collect decorator names from a decorated_definition or direct node."""
    results: list[str] = []
    parent = node.parent
    if parent and parent.type == "decorated_definition":
        for child in parent.children:
            if child.type == "decorator":
                name_part = child.child_by_field_name("value") or (
                    child.children[1] if child.child_count > 1 else None
                )
                if name_part:
                    results.append(_text(name_part))
    return tuple(results)


class PythonParser:
    """Parses a single Python file and extracts entities and relationships."""

    def __init__(self):
        self._parser = Parser(PY_LANGUAGE)

    def parse_file(
        self,
        file_path: Path,
        root_dir: Path | None = None,
    ) -> tuple[list[Entity], list[Relationship]]:
        source = file_path.read_bytes()
        tree = self._parser.parse(source)

        if root_dir:
            module_name = _module_name_from_path(file_path, root_dir)
        else:
            module_name = file_path.stem

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        module_entity = Entity(
            name=file_path.stem,
            qualified_name=module_name,
            entity_type=EntityType.MODULE,
            file_path=str(file_path),
            start_line=1,
            end_line=_text(tree.root_node).count("\n") + 1,
        )
        entities.append(module_entity)

        self._extract(
            tree.root_node,
            module_name,
            entities,
            relationships,
            str(file_path),
        )

        return entities, relationships

    def _extract(
        self,
        node: Node,
        scope: str,
        entities: list[Entity],
        relationships: list[Relationship],
        file_path: str,
    ) -> None:
        for child in node.children:
            actual = child
            if child.type == "decorated_definition":
                for sub in child.children:
                    if sub.type in ("function_definition", "class_definition"):
                        actual = sub
                        break

            if actual.type == "function_definition":
                self._handle_function(actual, scope, entities, relationships, file_path)
            elif actual.type == "class_definition":
                self._handle_class(actual, scope, entities, relationships, file_path)
            elif actual.type in ("import_statement", "import_from_statement"):
                self._handle_import(actual, scope, relationships, file_path)
            elif actual.type == "expression_statement":
                self._extract_calls_from_node(actual, scope, relationships, file_path)

    def _handle_function(
        self,
        node: Node,
        scope: str,
        entities: list[Entity],
        relationships: list[Relationship],
        file_path: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = _text(name_node)
        qualified = f"{scope}.{name}"

        is_method = "." in scope and any(
            e.entity_type == EntityType.CLASS for e in entities if scope.startswith(e.qualified_name)
        )
        entity_type = EntityType.METHOD if is_method else EntityType.FUNCTION

        body = node.child_by_field_name("body")
        entities.append(
            Entity(
                name=name,
                qualified_name=qualified,
                entity_type=entity_type,
                file_path=file_path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring=_docstring(body) if body else "",
                decorators=_decorators(node),
            )
        )
        relationships.append(
            Relationship(
                source=scope,
                target=qualified,
                relationship_type=RelationshipType.CONTAINS,
                file_path=file_path,
                line=node.start_point[0] + 1,
            )
        )

        if body:
            self._extract_calls_from_node(body, qualified, relationships, file_path)

    def _handle_class(
        self,
        node: Node,
        scope: str,
        entities: list[Entity],
        relationships: list[Relationship],
        file_path: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = _text(name_node)
        qualified = f"{scope}.{name}"

        body = node.child_by_field_name("body")
        entities.append(
            Entity(
                name=name,
                qualified_name=qualified,
                entity_type=EntityType.CLASS,
                file_path=file_path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring=_docstring(body) if body else "",
                decorators=_decorators(node),
            )
        )
        relationships.append(
            Relationship(
                source=scope,
                target=qualified,
                relationship_type=RelationshipType.CONTAINS,
                file_path=file_path,
                line=node.start_point[0] + 1,
            )
        )

        superclasses = node.child_by_field_name("superclasses")
        if superclasses:
            for arg in superclasses.children:
                if arg.type in ("identifier", "attribute"):
                    base_name = _text(arg)
                    relationships.append(
                        Relationship(
                            source=qualified,
                            target=base_name,
                            relationship_type=RelationshipType.INHERITS,
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                        )
                    )

        if body:
            self._extract(body, qualified, entities, relationships, file_path)

    def _handle_import(
        self,
        node: Node,
        scope: str,
        relationships: list[Relationship],
        file_path: str,
    ) -> None:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    relationships.append(
                        Relationship(
                            source=scope,
                            target=_text(child),
                            relationship_type=RelationshipType.IMPORTS,
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        relationships.append(
                            Relationship(
                                source=scope,
                                target=_text(name_node),
                                relationship_type=RelationshipType.IMPORTS,
                                file_path=file_path,
                                line=node.start_point[0] + 1,
                            )
                        )

        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            module_name = _text(module_node) if module_node else ""

            for child in node.children:
                if child.type == "dotted_name" and child != module_node:
                    target = f"{module_name}.{_text(child)}" if module_name else _text(child)
                    relationships.append(
                        Relationship(
                            source=scope,
                            target=target,
                            relationship_type=RelationshipType.IMPORTS,
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                        )
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        target = f"{module_name}.{_text(name_node)}" if module_name else _text(name_node)
                        relationships.append(
                            Relationship(
                                source=scope,
                                target=target,
                                relationship_type=RelationshipType.IMPORTS,
                                file_path=file_path,
                                line=node.start_point[0] + 1,
                            )
                        )

    def _extract_calls_from_node(
        self,
        node: Node,
        scope: str,
        relationships: list[Relationship],
        file_path: str,
    ) -> None:
        """Recursively find all call expressions within a node."""
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func:
                callee = _text(func)
                relationships.append(
                    Relationship(
                        source=scope,
                        target=callee,
                        relationship_type=RelationshipType.CALLS,
                        file_path=file_path,
                        line=node.start_point[0] + 1,
                    )
                )
        for child in node.children:
            self._extract_calls_from_node(child, scope, relationships, file_path)


def _module_name_from_path(file_path: Path, root_dir: Path) -> str:
    """Convert a file path to a dotted module name relative to root_dir."""
    try:
        rel = file_path.relative_to(root_dir)
    except ValueError:
        return file_path.stem
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else file_path.stem
