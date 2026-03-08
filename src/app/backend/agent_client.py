"""Client for querying the GraphRAG agent via Model Serving endpoint."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient

_client: WorkspaceClient | None = None
_client_created: float = 0
_CLIENT_TTL = 1800  # refresh SDK client every 30 min


def _get_client() -> WorkspaceClient:
    global _client, _client_created
    import time
    now = time.time()
    if _client is None or (now - _client_created) > _CLIENT_TTL:
        _client = WorkspaceClient()
        _client_created = now
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
    verse_texts: dict[str, str] = field(default_factory=dict)


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


_VERSE_REF_RE = re.compile(
    r"\b(\d?\s*[A-Z][a-z]+)\s+(\d+):(\d+)(?:\s*[-–]\s*(\d+))?"
)


def _extract_verse_refs(text: str) -> list[str]:
    """Pull all unique verse references (e.g. 'Ruth 4:13', 'Exodus 1:6-8') from text."""
    seen: set[str] = set()
    refs: list[str] = []
    for m in _VERSE_REF_RE.finditer(text):
        ref = m.group(0).strip()
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


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
    from backend.graph_client import lookup_verses

    endpoint = os.getenv("GRAPHRAG_ENDPOINT_NAME", "graphrag-bible-agent")
    w = _get_client()

    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={"input": [{"role": "user", "content": question}]},
    )

    texts = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part["text"])
        elif "text" in item:
            texts.append(item["text"])
    text = "\n".join(texts) if texts else str(resp)

    answer, prov_raw, path, sources, grounding = _parse_provenance(text)
    entities = _extract_entities(path or answer)
    verse_refs = _extract_verse_refs(text)

    try:
        verse_texts = lookup_verses(verse_refs)
    except Exception:
        verse_texts = {}

    return AgentResponse(
        answer=answer,
        provenance_raw=prov_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        full_text=text,
        entities_mentioned=entities,
        verse_texts=verse_texts,
    )


# ---------------------------------------------------------------------------
# Mock mode: used when no endpoint is available
# ---------------------------------------------------------------------------

_MOCK_RESPONSES: dict[str, str] = {
    "ruth": (
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
    "genesis exodus": (
        "### Answer\n"
        "Several key figures appear in both Genesis and Exodus:\n\n"
        "- **Joseph** — his story concludes in Genesis 50 and his legacy is referenced in Exodus 1:6-8\n"
        "- **Jacob (Israel)** — patriarch in Genesis, his descendants form the nation in Exodus\n"
        "- **God** — covenants with Abraham in Genesis, delivers Israel in Exodus\n"
        "- **Pharaoh** — different rulers, but the title bridges both narratives\n\n"
        "### Provenance\n"
        "- **Path**: Joseph (Genesis 50:26) → Israelites (Exodus 1:7) → Moses (Exodus 3:10)\n"
        "- **Sources**: Genesis 50:26, Exodus 1:6, Exodus 1:7, Exodus 1:8\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "moses": (
        "### Answer\n"
        "Moses is the central figure of Exodus and one of the most connected entities in the knowledge graph:\n\n"
        "- Born in Egypt during Israelite slavery (Exodus 2:1-10)\n"
        "- Called by God at the burning bush (Exodus 3:1-4:17)\n"
        "- Led the Israelites out of Egypt through the Red Sea (Exodus 14)\n"
        "- Received the Ten Commandments at Sinai (Exodus 20)\n"
        "- Referenced in Acts as a prophet (Acts 7:20-44)\n\n"
        "### Provenance\n"
        "- **Path**: Moses → God (CALLED_BY, Exodus 3:4) → Pharaoh (CONFRONTED, Exodus 7:10) "
        "→ Israelites (LED, Exodus 14:21) → Sinai (TRAVELED_TO, Exodus 19:1)\n"
        "- **Sources**: Exodus 2:10, Exodus 3:4, Exodus 14:21, Exodus 20:1, Acts 7:20\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "damascus": (
        "### Answer\n"
        "The road to Damascus is the setting for one of the most significant events in Acts:\n\n"
        "- Saul was traveling to Damascus to arrest Christians (Acts 9:1-2)\n"
        "- A light from heaven blinded him and he heard the voice of Jesus (Acts 9:3-6)\n"
        "- Saul was blind for three days in Damascus (Acts 9:8-9)\n"
        "- Ananias healed Saul and baptized him (Acts 9:17-18)\n"
        "- Saul became Paul, the apostle to the Gentiles\n\n"
        "### Provenance\n"
        "- **Path**: Saul → Damascus (TRAVELED_TO, Acts 9:3) → Jesus (ENCOUNTERED, Acts 9:5) "
        "→ Ananias (HEALED_BY, Acts 9:17) → Paul (BECAME, Acts 13:9)\n"
        "- **Sources**: Acts 9:1, Acts 9:3, Acts 9:5, Acts 9:17, Acts 13:9\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "david": (
        "### Answer\n"
        "David is a pivotal connecting entity between Ruth and Jesus in the knowledge graph:\n\n"
        "- David is the great-grandson of Ruth and Boaz through the lineage: Ruth → Obed → Jesse → David (Ruth 4:17-22)\n"
        "- Jesus is identified as a descendant of David (Matthew 1:6-16)\n"
        "- David is thus the bridge between the Old Testament lineage and the New Testament narrative\n\n"
        "### Provenance\n"
        "- **Path**: Ruth → Obed (PARENT_OF, Ruth 4:17) → Jesse (PARENT_OF, Ruth 4:22) "
        "→ David (PARENT_OF, Ruth 4:22) → Jesus (ANCESTOR_OF, Matthew 1:6)\n"
        "- **Sources**: Ruth 4:17, Ruth 4:22, Matthew 1:1, Matthew 1:6, Matthew 1:16\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "egypt": (
        "### Answer\n"
        "The Israelites' journey from Egypt is traced across multiple books:\n\n"
        "- Jacob's family migrated to Egypt during the famine (Genesis 46:1-7)\n"
        "- The Israelites were enslaved in Egypt for generations (Exodus 1:8-14)\n"
        "- God sent Moses to confront Pharaoh with ten plagues (Exodus 7-12)\n"
        "- The Israelites departed Egypt on Passover night (Exodus 12:31-42)\n"
        "- They crossed the Red Sea and journeyed to Sinai (Exodus 14-19)\n\n"
        "### Provenance\n"
        "- **Path**: Jacob → Egypt (TRAVELED_TO, Genesis 46:6) → Israelites (ENSLAVED_IN, Exodus 1:11) "
        "→ Moses (LED_BY, Exodus 14:21) → Red Sea (CROSSED, Exodus 14:22) → Sinai (ARRIVED_AT, Exodus 19:1)\n"
        "- **Sources**: Genesis 46:6, Exodus 1:11, Exodus 12:31, Exodus 14:21, Exodus 19:1\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
}


def _match_mock(question: str) -> str:
    """Pick the best mock response based on keywords in the question."""
    q = question.lower()
    if "ruth" in q and ("jesus" in q or "connected" in q or "lineage" in q):
        return _MOCK_RESPONSES["ruth"]
    if "genesis" in q and "exodus" in q:
        return _MOCK_RESPONSES["genesis exodus"]
    if "moses" in q:
        return _MOCK_RESPONSES["moses"]
    if "damascus" in q:
        return _MOCK_RESPONSES["damascus"]
    if "david" in q:
        return _MOCK_RESPONSES["david"]
    if "egypt" in q or "israelite" in q or "journey" in q:
        return _MOCK_RESPONSES["egypt"]
    return _MOCK_RESPONSES["ruth"]


def query_agent_mock(question: str) -> AgentResponse:
    """Return a canned response for demo/testing without a live endpoint."""
    from backend.graph_client import lookup_verses_mock

    text = _match_mock(question)
    answer, prov_raw, path, sources, grounding = _parse_provenance(text)
    entities = _extract_entities(path or answer)
    verse_refs = _extract_verse_refs(text)
    verse_texts = lookup_verses_mock(verse_refs)

    return AgentResponse(
        answer=answer,
        provenance_raw=prov_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        full_text=text,
        entities_mentioned=entities,
        verse_texts=verse_texts,
    )
