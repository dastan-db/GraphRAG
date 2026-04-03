from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    output: str = ""


@dataclass
class ParsedRuntimeResponse:
    answer: str
    provenance_raw: str
    path: str
    sources: list[str]
    grounding: str
    coverage: str
    full_text: str
    entities_mentioned: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


def _normalize_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump()
    normalized: dict[str, Any] = {}
    for attr in ("type", "name", "call_id", "id", "arguments", "output", "text", "content"):
        if hasattr(item, attr):
            normalized[attr] = getattr(item, attr)
    return normalized


def extract_output_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        output = response.get("output", [])
    else:
        output = getattr(response, "output", [])
    return [_normalize_item(item) for item in output]


def extract_tool_calls(output_items: list[dict[str, Any]]) -> list[ToolCallRecord]:
    calls: dict[str, ToolCallRecord] = {}
    for item in output_items:
        item_type = item.get("type", "")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or ""
            args_raw = item.get("arguments", "{}")
            try:
                arguments = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                arguments = {"raw": args_raw}
            calls[call_id] = ToolCallRecord(
                name=item.get("name", "unknown"),
                arguments=arguments,
            )
        elif item_type == "function_call_output":
            call_id = item.get("call_id", "")
            if call_id in calls:
                output = str(item.get("output", ""))
                calls[call_id].output = output[:4000]
    return list(calls.values())


def extract_output_text(output_items: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for item in output_items:
        if item.get("type") == "message":
            for part in item.get("content", []):
                part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", "")
                if part_type == "output_text":
                    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    if text:
                        texts.append(text)
        elif "text" in item:
            texts.append(str(item["text"]))
    return "\n".join(texts)


def _split_sources_field(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return []
    if ";" in cleaned:
        parts = cleaned.split(";")
    elif "→" not in cleaned and "," in cleaned:
        parts = cleaned.split(",")
    else:
        parts = [cleaned]
    return [part.strip().replace("`", "") for part in parts if part.strip()]


def split_provenance(text: str) -> tuple[str, str, str, list[str], str, str]:
    parts = re.split(r"###?\s*Provenance", text, maxsplit=1)
    answer = parts[0].strip()
    if len(parts) < 2:
        return answer, "", "", [], "", ""

    provenance_raw = parts[1].strip()
    fields: dict[str, str | list[str]] = {
        "path": "",
        "sources": [],
        "grounding": "",
        "coverage": "",
    }
    current_field = ""
    field_names = {"path", "sources", "grounding", "coverage"}

    for raw_line in provenance_raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        match = re.match(
            r"^[-*]?\s*\*?\*?(Path|Sources|Grounding|Coverage)\*?\*?\s*:\s*(.*)$",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            current_field = match.group(1).lower()
            value = match.group(2).strip()
            if current_field == "sources":
                fields["sources"] = _split_sources_field(value)
            else:
                fields[current_field] = value
            continue

        if current_field == "sources":
            nested = stripped.lstrip("- ").strip()
            if nested:
                source_list = list(fields.get("sources", []))
                source_list.append(nested.replace("`", ""))
                fields["sources"] = source_list
            continue

        if current_field in field_names - {"sources"}:
            current = str(fields.get(current_field, "") or "")
            fields[current_field] = f"{current} {stripped}".strip()

    return (
        answer,
        provenance_raw,
        str(fields["path"]),
        list(fields["sources"]),
        str(fields["grounding"]),
        str(fields["coverage"]),
    )


def extract_entities(text: str) -> list[str]:
    bold = re.findall(r"\*\*([^*]+)\*\*", text)
    arrows = re.findall(r"(\w[\w\s]*?)(?:\s*→|$)", text)
    seen: set[str] = set()
    entities: list[str] = []
    for name in bold + arrows:
        cleaned = name.strip()
        if cleaned and cleaned not in seen and len(cleaned) > 1:
            seen.add(cleaned)
            entities.append(cleaned)
    return entities


def parse_agent_response(response: Any) -> ParsedRuntimeResponse:
    output_items = extract_output_items(response)
    tool_calls = extract_tool_calls(output_items)
    full_text = extract_output_text(output_items)
    answer, provenance_raw, path, sources, grounding, coverage = split_provenance(full_text)
    entities = extract_entities(path or answer)
    return ParsedRuntimeResponse(
        answer=answer,
        provenance_raw=provenance_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        coverage=coverage,
        full_text=full_text,
        entities_mentioned=entities,
        tool_calls=tool_calls,
    )
