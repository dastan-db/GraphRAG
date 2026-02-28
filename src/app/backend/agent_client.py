"""Client for querying the GraphRAG agent via Model Serving endpoint."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

_client: WorkspaceClient | None = None


def _get_client() -> WorkspaceClient:
    global _client
    if _client is None:
        _client = WorkspaceClient()
    return _client


@dataclass
class AgentResponse:
    answer: str
    provenance_raw: str
    path: str
    sources: list[str]
    grounding: str
    full_text: str
    entities_mentioned: list[str] = field(default_factory=list)


def _parse_provenance(text: str) -> tuple[str, str, str, list[str], str]:
    """Split agent response into answer and provenance components."""
    parts = re.split(r"###?\s*Provenance", text, maxsplit=1)
    answer = parts[0].strip()
    if len(parts) < 2:
        return answer, "", "", [], ""

    prov = parts[1].strip()

    path_match = re.search(r"\*?\*?Path\*?\*?\s*:\s*(.+)", prov)
    path = path_match.group(1).strip() if path_match else ""

    sources_match = re.search(r"\*?\*?Sources\*?\*?\s*:\s*(.+)", prov)
    sources_raw = sources_match.group(1).strip() if sources_match else ""
    sources = [s.strip() for s in sources_raw.split(",") if s.strip()]

    grounding_match = re.search(r"\*?\*?Grounding\*?\*?\s*:\s*(.+)", prov)
    grounding = grounding_match.group(1).strip() if grounding_match else ""

    return answer, prov, path, sources, grounding


def _extract_entities(text: str) -> list[str]:
    """Pull entity names from bold markdown or arrow-path notation."""
    bold = re.findall(r"\*\*([^*]+)\*\*", text)
    arrows = re.findall(r"(\w[\w\s]*?)(?:\s*→|$)", text)
    seen: set[str] = set()
    result: list[str] = []
    for name in bold + arrows:
        name = name.strip()
        if name and name not in seen and len(name) > 1:
            seen.add(name)
            result.append(name)
    return result


def query_agent(question: str) -> AgentResponse:
    """Send a question to the GraphRAG agent endpoint and return parsed result."""
    endpoint = os.getenv("GRAPHRAG_ENDPOINT_NAME", "graphrag-bible-agent")
    w = _get_client()

    response = w.serving_endpoints.query(
        name=endpoint,
        messages=[ChatMessage(role=ChatMessageRole.USER, content=question)],
    )

    text = response.choices[0].message.content
    answer, prov_raw, path, sources, grounding = _parse_provenance(text)
    entities = _extract_entities(path or answer)

    return AgentResponse(
        answer=answer,
        provenance_raw=prov_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        full_text=text,
        entities_mentioned=entities,
    )


# ---------------------------------------------------------------------------
# Mock mode: used when no endpoint is available
# ---------------------------------------------------------------------------

_MOCK_RESPONSES: dict[str, str] = {
    "default": (
        "### Answer\n"
        "Ruth is connected to Jesus through a multi-generational lineage:\n\n"
        "- Ruth married Boaz (Ruth 4:13)\n"
        "- Boaz and Ruth had a son named Obed (Ruth 4:17)\n"
        "- Obed was the father of Jesse (Ruth 4:17)\n"
        "- Jesse was the father of David (Ruth 4:22)\n"
        "- Jesus descended from the line of David (Matthew 1:6-16)\n\n"
        "### Provenance\n"
        "- **Path**: Ruth → Boaz (MARRIED_TO, Ruth 4:13) → Obed (PARENT_OF, Ruth 4:17) "
        "→ Jesse (PARENT_OF, Ruth 4:22) → David (ANCESTOR_OF, Matthew 1:6) → Jesus\n"
        "- **Sources**: Ruth 4:13, Ruth 4:17, Ruth 4:22, Matthew 1:6, Matthew 1:16\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
}


def query_agent_mock(question: str) -> AgentResponse:
    """Return a canned response for demo/testing without a live endpoint."""
    text = _MOCK_RESPONSES["default"]
    answer, prov_raw, path, sources, grounding = _parse_provenance(text)
    entities = _extract_entities(path or answer)
    return AgentResponse(
        answer=answer,
        provenance_raw=prov_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        full_text=text,
        entities_mentioned=entities,
    )
