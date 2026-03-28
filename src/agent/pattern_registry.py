"""Pattern registry for the adaptive legal-audit agent.

Maps classified question patterns to pre-defined execution plans.
Fast-path patterns skip the ReAct tool-selection loop — they execute
a fixed sequence of tool queries and synthesize with a single LLM call.

New patterns are promoted from the slow path via trace analysis
(see notebooks/12_Enron_Pattern_Analysis.py).

## Pattern Promotion Workflow

When the trace analysis notebook (12) identifies a candidate for promotion:

1. **Add to PATTERN_REGISTRY** — Create a new Pattern entry below with:
   - A descriptive name matching the classifier categories
   - Execution steps using existing tools (or new tools if needed)
   - A focused synthesis prompt for the pattern type
   - min_confidence threshold (start at 0.85, lower after validation)

2. **Build supporting data** (if needed) — If the pattern requires a new
   pre-aggregation table, create it in a new notebook cell or notebook
   (e.g., 07e_Enron_<feature>.py). Add the table to config.py and
   export_local_data.py.

3. **Create composite tool** (if needed) — If the execution plan requires
   a tool that doesn't exist, add it to src/agent/agent_serving.py and
   include it in LOCAL_TOOLS.

4. **Update classifier prompt** — Add the new pattern name and description
   to _CORPORATE_CLASSIFY_AND_EXTRACT_PROMPT in agent_serving.py so the
   8B LLM can classify questions into the new category.

5. **Test locally** — Run with GRAPHRAG_BACKEND=local:
   python scripts/test_local.py "<sample question for new pattern>"
   Verify the log shows FAST_PATH: <new_pattern_name>.

6. **Run evaluation** — Execute notebook 08 to validate the new pattern
   doesn't degrade existing scores.

7. **Deploy** — Redeploy the agent endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExecutionStep:
    """A single tool invocation in a fast-path execution plan.

    Params may contain placeholders:
      $ENTITY   — replaced with the primary entity name from classifier output
      $ENTITY_B — replaced with the secondary entity (for dyad queries)
      $TIME_RANGE — replaced with extracted time range (if any)
    """
    tool_name: str
    params: dict = field(default_factory=dict)


@dataclass
class Pattern:
    name: str
    synthesis_prompt: str
    steps: list[ExecutionStep]
    min_confidence: float = 0.8


ORG_HIERARCHY_SYNTHESIS = """You are a corporate communications analyst answering a question about organizational hierarchy at Enron.

You have been given pre-fetched data from the knowledge graph: REPORTS_TO relationships, MANAGES relationships, an entity summary, and email evidence. Use ALL of this data to provide a comprehensive, well-cited answer.

Guidelines:
- List ALL people found in the data, with their roles/titles when available.
- Pay attention to edge direction: in REPORTS_TO, the source reports to the target. In MANAGES, the source manages the target.
- If the data is sparse (few relationships found), state this explicitly and note the coverage limitation.
- Cite specific relationship descriptions as evidence.
- Cite email evidence inline using [YYYY-MM-DD, From: sender, Subject: topic] format when available.
- Show organizational paths with → notation (e.g., "Delainey → Skilling → Lay").
- Include explicit relationship type labels (REPORTS_TO, MANAGES) in your answer.
- Do NOT fabricate relationships not present in the data."""


COMMUNICATION_SYNTHESIS = """You are a corporate communications analyst answering a question about communication patterns at Enron.

You have been given pre-fetched data: a ranked contact list with sent/received counts, an entity profile, and sample emails between key contacts. Use ALL of this data to provide a comprehensive answer.

Guidelines:
- Present the ranked contacts with their communication volumes.
- Note directional patterns (who initiated more).
- If the question asks about specific people, highlight them.
- Cite email counts as evidence and include specific email citations [YYYY-MM-DD, From: sender, Subject: topic] when available.
- Include relationship type labels (SENT_TO, COLLABORATES_WITH) where relevant.
- Do NOT fabricate communication patterns not present in the data."""


PATH_SYNTHESIS = """You are a corporate communications analyst answering a question about how entities are connected at Enron.

You have been given pre-fetched path data showing the shortest connection between two entities, plus email evidence between the endpoints. Use ALL of this data to explain the connection.

Guidelines:
- Walk through each hop in the path, explaining the relationship at each step using → notation.
- Note the relationship types and directions (e.g., "Delainey REPORTS_TO Skilling").
- If direct relationships also exist, mention those.
- Cite email evidence [YYYY-MM-DD, From: sender, Subject: topic] when available to support the connection.
- Do NOT fabricate connections not present in the data."""


TEMPORAL_SYNTHESIS = """You are a corporate communications analyst answering a question about events and timelines at Enron.

You have been given pre-fetched data: investigation timeline events and emails from the relevant period. Use ALL of this data to provide a chronological, evidence-backed answer.

Guidelines:
- Present events in chronological order.
- For each event, cite the source: timeline entry or email evidence (date, sender, subject).
- If the timeline data is sparse, supplement with email evidence and note the gap.
- Distinguish between curated timeline facts and email-derived evidence.
- Do NOT fabricate dates or events not present in the data."""


TOPIC_SYNTHESIS = """You are a corporate communications analyst answering a question about discussion topics at Enron.

You have been given pre-fetched data: an entity profile with all relationships, and emails mentioning the relevant entity/topic. Use ALL of this data to identify and explain the topics discussed.

Guidelines:
- Identify distinct discussion themes from the email subjects and body previews.
- Group related emails by topic where possible.
- Cite specific email evidence: dates, senders, subjects.
- Note the volume of evidence for each topic (how many emails mention it).
- Do NOT fabricate discussion topics not supported by the data."""


PATTERN_REGISTRY: dict[str, Pattern] = {
    "org_hierarchy": Pattern(
        name="org_hierarchy",
        synthesis_prompt=ORG_HIERARCHY_SYNTHESIS,
        steps=[
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "REPORTS_TO",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "MANAGES",
            }),
            ExecutionStep("get_entity_summary", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.8,
    ),

    "communication": Pattern(
        name="communication",
        synthesis_prompt=COMMUNICATION_SYNTHESIS,
        steps=[
            ExecutionStep("find_top_contacts", {
                "entity_name": "$ENTITY",
                "direction": "both",
                "limit": 15,
            }),
            ExecutionStep("get_entity_summary", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.8,
    ),

    "path": Pattern(
        name="path",
        synthesis_prompt=PATH_SYNTHESIS,
        steps=[
            ExecutionStep("trace_path", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_emails_between", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
        ],
        min_confidence=0.85,
    ),

    "temporal": Pattern(
        name="temporal",
        synthesis_prompt=TEMPORAL_SYNTHESIS,
        steps=[
            ExecutionStep("query_timeline", {
                "person_name": "$ENTITY",
                "date_from": "$DATE_FROM",
                "date_to": "$DATE_TO",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.75,
    ),

    "topic": Pattern(
        name="topic",
        synthesis_prompt=TOPIC_SYNTHESIS,
        steps=[
            ExecutionStep("get_entity_summary", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "DISCUSSES",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.75,
    ),
}


def resolve_params(params: dict, entities: list[dict], *, metadata: dict | None = None) -> dict:
    """Replace $ENTITY / $ENTITY_B / $DATE_FROM / $DATE_TO placeholders.

    Args:
        params: Template params with $-prefixed placeholders.
        entities: Extracted entities from the classifier.
        metadata: Optional dict with 'date_from' and 'date_to' keys for temporal queries.
    """
    resolved = {}
    primary = entities[0]["name"] if entities else ""
    secondary = entities[1]["name"] if len(entities) > 1 else ""
    meta = metadata or {}

    for key, value in params.items():
        if isinstance(value, str):
            value = value.replace("$ENTITY_B", secondary)
            value = value.replace("$ENTITY", primary)
            value = value.replace("$DATE_FROM", meta.get("date_from", ""))
            value = value.replace("$DATE_TO", meta.get("date_to", ""))
        resolved[key] = value
    return resolved
