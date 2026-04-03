"""Client for querying the shared GraphRAG runtime or endpoint."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from runtime import RuntimeQuery, SharedRuntimeOrchestrator
from runtime.config import RuntimeConfig

_client: WorkspaceClient | None = None
_client_created: float = 0
_CLIENT_TTL = 1800  # refresh SDK client every 30 min
_orchestrators: dict[tuple[str, ...], SharedRuntimeOrchestrator] = {}


def _get_client() -> WorkspaceClient:
    global _client, _client_created
    import time
    now = time.time()
    if _client is None or (now - _client_created) > _CLIENT_TTL:
        _client = WorkspaceClient()
        _client_created = now
    return _client


def _runtime_transport_for_app() -> str:
    mode = (os.environ.get("GRAPHRAG_APP_RUNTIME_MODE") or "shared").strip().lower()
    if mode == "endpoint":
        return "endpoint"
    return "direct"


def _get_orchestrator(*, transport: str | None = None) -> SharedRuntimeOrchestrator:
    env = dict(os.environ)
    env["GRAPHRAG_RUNTIME_TRANSPORT"] = transport or _runtime_transport_for_app()
    config = RuntimeConfig.from_env(env)
    key = (
        config.transport.value,
        config.data_backend.value,
        config.llm_provider,
        config.router_transport.value,
        config.planner_transport.value,
        config.graph_transport.value,
        config.evidence_transport.value,
        config.analytics_transport.value,
    )
    if key not in _orchestrators:
        _orchestrators[key] = SharedRuntimeOrchestrator(config)
    return _orchestrators[key]


def _query_runtime(
    *,
    corpus: str,
    question: str,
    permitted_books: list[str] | None = None,
    tier: str = "",
    endpoint_name: str = "",
):
    orchestrator = _get_orchestrator()
    return orchestrator.query(
        RuntimeQuery(
            question=question,
            corpus=corpus,
            permitted_books=permitted_books or [],
            user_tier=tier,
            endpoint_name=endpoint_name,
        )
    )


@dataclass
class ToolCall:
    name: str
    arguments: dict
    output: str = ""


@dataclass
class AgentResponse:
    answer: str
    provenance_raw: str
    path: str
    sources: list[str]
    grounding: str
    full_text: str
    coverage: str = ""
    entities_mentioned: list[str] = field(default_factory=list)
    verse_texts: dict[str, str] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


def _extract_tool_calls(output_items: list[dict]) -> list[ToolCall]:
    """Extract tool call name/args/output from Responses API output items."""
    calls: dict[str, ToolCall] = {}
    for item in output_items:
        itype = item.get("type", "")
        if itype == "function_call":
            call_id = item.get("call_id", item.get("id", ""))
            args_raw = item.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except (json.JSONDecodeError, TypeError):
                args = {"raw": args_raw}
            calls[call_id] = ToolCall(name=item.get("name", "unknown"), arguments=args)
        elif itype == "function_call_output":
            call_id = item.get("call_id", "")
            if call_id in calls:
                out = item.get("output", "")
                calls[call_id].output = out[:2000] if len(out) > 2000 else out
    return list(calls.values())


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


def _parse_provenance(text: str) -> tuple[str, str, str, list[str], str, str]:
    """Split agent response into answer and provenance components."""
    parts = re.split(r"###?\s*Provenance", text, maxsplit=1)
    answer = parts[0].strip()
    if len(parts) < 2:
        return answer, "", "", [], "", ""

    prov = parts[1].strip()
    fields: dict[str, str | list[str]] = {
        "path": "",
        "sources": [],
        "grounding": "",
        "coverage": "",
    }
    current_field = ""
    field_names = {"path", "sources", "grounding", "coverage"}

    for raw_line in prov.splitlines():
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
                cast_sources = list(fields.get("sources", []))
                cast_sources.append(nested.replace("`", ""))
                fields["sources"] = cast_sources
            continue

        if current_field in field_names - {"sources"}:
            current_value = str(fields.get(current_field, "") or "")
            fields[current_field] = f"{current_value} {stripped}".strip()

    return (
        answer,
        prov,
        str(fields["path"]),
        list(fields["sources"]),
        str(fields["grounding"]),
        str(fields["coverage"]),
    )


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


def query_agent(question: str, permitted_books: list[str] | None = None) -> AgentResponse:
    """Query the Bible runtime and return the parsed response.

    Args:
        permitted_books: Optional list of book names the user is permitted to
            access. When set, the agent restricts graph traversal to these books
            via runtime-scoped access controls.
    """
    from backend.graph_client import lookup_verses

    parsed = _query_runtime(
        corpus="bible",
        question=question,
        permitted_books=permitted_books,
    )
    verse_refs = _extract_verse_refs(parsed.full_text)

    try:
        verse_texts = lookup_verses(verse_refs)
    except Exception:
        verse_texts = {}

    return AgentResponse(
        answer=parsed.answer,
        provenance_raw=parsed.provenance_raw,
        path=parsed.path,
        sources=parsed.sources,
        grounding=parsed.grounding,
        full_text=parsed.full_text,
        coverage=parsed.coverage,
        entities_mentioned=parsed.entities_mentioned,
        verse_texts=verse_texts,
        tool_calls=[
            ToolCall(name=tc.name, arguments=tc.arguments, output=tc.output)
            for tc in parsed.tool_calls
        ],
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


def query_agent_enron(question: str, tier: str = "") -> AgentResponse:
    """Query the Enron runtime and return the parsed response.

    Args:
        tier: Access tier (legal_team, executive_team, analyst_team).  When set,
            the runtime restricts graph visibility via environment-backed access
            controls and can be rolled between shared/direct and endpoint modes.
    """
    parsed = _query_runtime(
        corpus="enron",
        question=question,
        tier=tier,
        endpoint_name=os.environ.get("GRAPHRAG_ENRON_ENDPOINT_NAME", ""),
    )

    return AgentResponse(
        answer=parsed.answer,
        provenance_raw=parsed.provenance_raw,
        path=parsed.path,
        sources=parsed.sources,
        grounding=parsed.grounding,
        full_text=parsed.full_text,
        coverage=parsed.coverage,
        entities_mentioned=parsed.entities_mentioned,
        tool_calls=[
            ToolCall(name=tc.name, arguments=tc.arguments, output=tc.output)
            for tc in parsed.tool_calls
        ],
    )


# ---------------------------------------------------------------------------
# Enron mock responses
# ---------------------------------------------------------------------------

_ENRON_MOCK_RESPONSES: dict[str, str] = {
    "california": (
        "### Answer\n"
        "Several key executives were involved in California energy trading decisions:\n\n"
        "- **Kenneth Lay** — CEO, received briefings on California energy strategy (emails from Nov 2000)\n"
        "- **Jeffrey Skilling** — COO/CEO, directed trading strategy through Enron Energy Trading\n"
        "- **Tim Belden** — Head of West Coast trading, directly managed California operations\n"
        "- **David Delainey** — CEO of Enron Energy Services, involved in retail energy decisions\n\n"
        "Communication flow: Tim Belden reported to David Delainey, who reported to Jeffrey Skilling. "
        "Kenneth Lay received summary briefings from Skilling.\n\n"
        "### Provenance\n"
        "- **Path**: Tim Belden \u2192 David Delainey (REPORTS_TO) \u2192 Jeffrey Skilling (REPORTS_TO) \u2192 Kenneth Lay (REPORTS_TO)\n"
        "- **Sources**: [2000-11-15] belden-t to delainey-d, Subject: California Update; "
        "[2000-12-03] skilling-j to lay-k, Subject: Energy Trading Summary\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "skilling": (
        "### Answer\n"
        "Jeff Skilling managed several key projects between 2000-2001:\n\n"
        "1. **Enron Broadband Services** — oversaw the broadband division's strategic direction\n"
        "2. **Enron Energy Trading** — directed the core trading business\n"
        "3. **Enron International** — managed international expansion initiatives\n"
        "4. **Project Raptor** — involved in financial restructuring discussions\n\n"
        "Skilling was particularly active in Broadband communications, with 47 emails referencing "
        "the division between Jan 2000 and Aug 2001.\n\n"
        "### Provenance\n"
        "- **Path**: Jeffrey Skilling \u2192 Enron Broadband Services (MANAGES) \u2192 Kenneth Rice (REPORTS_TO)\n"
        "- **Sources**: [2000-03-15] skilling-j to rice-k, Subject: Broadband Strategy; "
        "[2001-02-20] skilling-j to lay-k, Subject: Q4 Results\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "broadband": (
        "### Answer\n"
        "Information about the Broadband division flowed through several channels:\n\n"
        "- **Kenneth Rice** (CEO, Enron Broadband) reported directly to **Jeffrey Skilling**\n"
        "- Weekly status emails went from Rice to Skilling and **Kenneth Lay**\n"
        "- Technical updates flowed from engineering leads to Rice\n"
        "- Financial projections were shared with **Andrew Fastow** (CFO) for quarterly reporting\n\n"
        "The communication pattern shows a hub-and-spoke model centered on Kenneth Rice, "
        "with Skilling as the primary executive recipient.\n\n"
        "### Provenance\n"
        "- **Path**: Kenneth Rice \u2192 Jeffrey Skilling (REPORTS_TO) \u2192 Kenneth Lay (REPORTS_TO)\n"
        "- **Sources**: [2000-06-10] rice-k to skilling-j, Subject: Broadband Weekly; "
        "[2000-09-22] rice-k to fastow-a, Subject: Broadband Financials\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "fastow": (
        "### Answer\n"
        "Several executives discussed Fastow's partnerships in email communications:\n\n"
        "- **Jeffrey Skilling** — discussed LJM partnership structure with Fastow directly\n"
        "- **Kenneth Lay** — received briefings about partnership arrangements\n"
        "- **Rick Causey** (CAO) — involved in accounting treatment discussions\n"
        "- **Ben Glisan** (Treasurer) — discussed financial terms of SPE structures\n\n"
        "Most communications about the partnerships were between Fastow and Glisan (32 emails), "
        "followed by Fastow and Causey (18 emails).\n\n"
        "### Provenance\n"
        "- **Path**: Andrew Fastow \u2192 LJM Partnership (MANAGES) \u2192 Ben Glisan (COLLABORATES_WITH)\n"
        "- **Sources**: [2001-01-15] fastow-a to glisan-b, Subject: LJM Structure; "
        "[2001-03-20] fastow-a to causey-r, Subject: SPE Accounting\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "lay": (
        "### Answer\n"
        "The most frequent communicators with Kenneth Lay based on email volume:\n\n"
        "1. **Rosalee Fleming** (Executive Assistant) — 245 emails\n"
        "2. **Jeffrey Skilling** (COO/CEO) — 187 emails\n"
        "3. **Steven Kean** (VP Public Affairs) — 134 emails\n"
        "4. **Richard Shapiro** (VP Government Affairs) — 98 emails\n"
        "5. **James Derrick** (General Counsel) — 76 emails\n\n"
        "The communication pattern shows Lay relied heavily on his executive assistant for "
        "scheduling and information routing, while Skilling was his primary strategic counterpart.\n\n"
        "### Provenance\n"
        "- **Path**: Kenneth Lay \u2192 Rosalee Fleming (SENT_TO, 245 emails) \u2192 Jeffrey Skilling (SENT_TO, 187 emails)\n"
        "- **Sources**: Communication volume analysis from SENT_TO relationship weights\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
    "trading": (
        "### Answer\n"
        "The Enron Energy Trading division had the following organizational structure:\n\n"
        "- **Jeffrey Skilling** (COO/CEO) — ultimate oversight\n"
        "- **David Delainey** — CEO of Enron Energy Services\n"
        "- **John Lavorato** — Co-CEO of Enron Americas\n"
        "- **Tim Belden** — Head of West Coast Trading\n"
        "- **John Arnold** — Head of Natural Gas Trading\n\n"
        "The division was structured with regional trading desks reporting to Delainey and Lavorato, "
        "who in turn reported to Skilling.\n\n"
        "### Provenance\n"
        "- **Path**: Tim Belden \u2192 David Delainey (REPORTS_TO) \u2192 Jeffrey Skilling (REPORTS_TO)\n"
        "- **Sources**: [2001-04-10] delainey-d to skilling-j, Subject: Trading Desk Reorg; "
        "[2000-08-15] lavorato-j to delainey-d, Subject: Americas Update\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    ),
}


def _match_enron_mock(question: str) -> str:
    q = question.lower()
    if "california" in q or "energy trading decision" in q:
        return _ENRON_MOCK_RESPONSES["california"]
    if "skilling" in q and ("project" in q or "manage" in q):
        return _ENRON_MOCK_RESPONSES["skilling"]
    if "broadband" in q or "information flow" in q:
        return _ENRON_MOCK_RESPONSES["broadband"]
    if "fastow" in q or "partnership" in q:
        return _ENRON_MOCK_RESPONSES["fastow"]
    if "lay" in q and ("communicat" in q or "frequent" in q):
        return _ENRON_MOCK_RESPONSES["lay"]
    if "trading" in q or "organizational" in q or "structure" in q:
        return _ENRON_MOCK_RESPONSES["trading"]
    return _ENRON_MOCK_RESPONSES["california"]


_MOCK_TOOL_CALLS: dict[str, list[ToolCall]] = {
    "california": [
        ToolCall(
            name="find_entity",
            arguments={"name": "California energy trading"},
            output=json.dumps([
                {"name": "California Energy Trading", "type": "ORGANIZATION",
                 "description": "West coast power trading desk",
                 "sensitivity": "executive_confidential"},
            ]),
        ),
        ToolCall(
            name="find_entity",
            arguments={"name": "Enron California"},
            output=json.dumps([
                {"name": "Enron California", "type": "ORGANIZATION",
                 "description": "Enron's California operations",
                 "sensitivity": "general"},
            ]),
        ),
        ToolCall(
            name="find_connections",
            arguments={"entity_name": "Enron California"},
            output=json.dumps({
                "entity": "Enron California", "total": 4,
                "by_type": {
                    "MANAGES": [
                        {"source": "Tim Belden", "target": "California Energy Trading",
                         "description": "Managed trading desk",
                         "sensitivity": "executive_confidential",
                         "bcc": "legal-review@enron.com"},
                    ],
                    "BRIEFED_ON": [
                        {"source": "Kenneth Lay", "target": "California Energy Trading",
                         "description": "Received briefings", "sensitivity": "general"},
                    ],
                    "SUPERVISES": [
                        {"source": "David Delainey", "target": "Tim Belden",
                         "description": "Direct report", "sensitivity": "general"},
                        {"source": "Jeff Skilling", "target": "David Delainey",
                         "description": "Executive oversight",
                         "sensitivity": "attorney_client_privileged",
                         "bcc": "vkaminski@enron.com"},
                    ],
                },
            }),
        ),
        ToolCall(
            name="get_source_emails",
            arguments={"entity_name": "Enron California"},
            output=json.dumps({
                "entity": "Enron California", "total": 2,
                "emails": [
                    {"date": "2000-11-15", "sender": "belden-t",
                     "subject": "California Update",
                     "body_preview": "Attached is the weekly California trading summary...",
                     "sensitivity": "executive_confidential",
                     "bcc": "legal-review@enron.com"},
                    {"date": "2000-12-03", "sender": "skilling-j",
                     "subject": "Energy Trading Summary",
                     "body_preview": "Per counsel's advice, the following trading positions...",
                     "sensitivity": "attorney_client_privileged",
                     "bcc": "derrick-j@enron.com"},
                ],
            }),
        ),
    ],
    "skilling": [
        ToolCall(
            name="find_entity",
            arguments={"name": "Jeffrey Skilling"},
            output=json.dumps([
                {"name": "Jeffrey Skilling", "type": "PERSON",
                 "description": "COO/CEO of Enron (2001)", "sensitivity": "general"},
            ]),
        ),
        ToolCall(
            name="find_connections",
            arguments={"entity_name": "Jeffrey Skilling"},
            output=json.dumps({
                "entity": "Jeffrey Skilling", "total": 3,
                "by_type": {
                    "MANAGES": [
                        {"source": "Jeffrey Skilling", "target": "Enron Broadband Services",
                         "description": "Division leadership", "sensitivity": "general"},
                    ],
                    "SUPERVISES": [
                        {"source": "Jeffrey Skilling", "target": "Kenneth Rice",
                         "description": "Executive report", "sensitivity": "executive_confidential"},
                    ],
                    "INVOLVED_IN": [
                        {"source": "Jeffrey Skilling", "target": "Project Raptor",
                         "description": "SPE involvement",
                         "sensitivity": "attorney_client_privileged",
                         "bcc": "causey-r@enron.com"},
                    ],
                },
            }),
        ),
    ],
}


def _get_mock_tool_calls(question: str) -> list[ToolCall]:
    q = question.lower()
    if "california" in q or "energy trading decision" in q:
        return _MOCK_TOOL_CALLS["california"]
    if "skilling" in q:
        return _MOCK_TOOL_CALLS["skilling"]
    return _MOCK_TOOL_CALLS.get("california", [])


def query_agent_enron_mock(question: str) -> AgentResponse:
    """Return a canned Enron response for demo/testing without a live endpoint."""
    text = _match_enron_mock(question)
    answer, prov_raw, path, sources, grounding, coverage = _parse_provenance(text)
    entities = _extract_entities(path or answer)

    return AgentResponse(
        answer=answer,
        provenance_raw=prov_raw,
        path=path,
        sources=sources,
        grounding=grounding,
        full_text=text,
        coverage=coverage,
        entities_mentioned=entities,
        tool_calls=_get_mock_tool_calls(question),
    )


def query_agent_mock(question: str) -> AgentResponse:
    """Return a canned response for demo/testing without a live endpoint."""
    from backend.graph_client import lookup_verses_mock

    text = _match_mock(question)
    answer, prov_raw, path, sources, grounding, coverage = _parse_provenance(text)
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
        coverage=coverage,
        entities_mentioned=entities,
        verse_texts=verse_texts,
    )
