"""Pattern registry for the adaptive legal-audit agent.

Maps classified question patterns to pre-defined execution plans (MECE primitives).
The Query Planner decomposes user questions into sub-questions, each tagged with
one of 6 primitives. Each primitive has a fixed tool plan and a focused synthesis prompt.

The 6 primitives are derived from 2 orthogonal vectors across all eval questions:
  - Anchor type:  single entity | entity pair | temporal | keyword | open
  - Information need:  structure | explore | connection | events | content | synthesis
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExecutionStep:
    """A single tool invocation in a fast-path execution plan.

    Params may contain placeholders:
      $ENTITY   — replaced with the primary entity name from classifier output
      $ENTITY_B — replaced with the secondary entity (for dyad queries)
      $KEYWORDS — replaced with extracted search keywords
      $DATE_FROM / $DATE_TO — replaced with extracted date range
      $QUESTION — replaced with the original user question
    """
    tool_name: str
    params: dict = field(default_factory=dict)


@dataclass
class Pattern:
    name: str
    synthesis_prompt: str
    steps: list[ExecutionStep]
    min_confidence: float = 0.0


# ---------------------------------------------------------------------------
# Synthesis prompts for the 6 MECE primitives
# ---------------------------------------------------------------------------

ENTITY_STRUCTURE_SYNTHESIS = """You are a corporate communications analyst answering a question about organizational hierarchy at Enron.

You have curated org hierarchy data (from SEC filings/DOJ records), graph relationships, and an entity summary. The curated data is the PRIMARY source of truth — it has verified reporting lines with temporal validity.

Guidelines:
- PRIORITIZE curated org_hierarchy results over LLM-extracted relationships when they conflict.
- List ALL people found with their roles/titles and effective date ranges.
- Pay attention to edge direction: in REPORTS_TO, the source reports to the target.
- Show organizational paths with → notation (e.g., "Delainey → Skilling → Lay").
- Note temporal changes in reporting structure (e.g., "reported to X until Aug 2001, then to Y").
- Only cite emails that DIRECTLY support a specific claim. Do NOT cite news digests or mass emails as evidence for org structure.
- If the curated data is comprehensive, state its source (SEC filings, DOJ, congressional testimony).
- Do NOT fabricate relationships not present in the data."""


ENTITY_EXPLORE_SYNTHESIS = """You are a corporate communications analyst answering a question about an Enron employee's activities and connections.

You have a ranked contact list, discussion topics, an entity profile, and sample emails.

Guidelines:
- Present the person's role and key relationships.
- Rank their top contacts with communication volumes.
- Identify their main discussion topics from relationship and email data.
- Cite specific email evidence [YYYY-MM-DD, From: sender, Subject: topic] when available.
- Note directional patterns (who initiated more).
- Do NOT fabricate activities or contacts not in the data."""


ENTITY_PAIR_SYNTHESIS = """You are a corporate communications analyst answering a question about the relationship between two people at Enron.

You have path data, direct emails between them, shared discussion topics, and relationship data.

Guidelines:
- Walk through each hop in any connection path using → notation.
- Quantify their direct communication (email count, direction).
- List shared discussion topics with evidence.
- Note the relationship types (REPORTS_TO, COLLABORATES_WITH, SENT_TO).
- Cite specific emails [YYYY-MM-DD, From: sender, Subject: topic] that illuminate their relationship.
- Do NOT fabricate connections not present in the data."""


TIMELINE_SYNTHESIS = """You are a corporate communications analyst answering a question about events and timelines at Enron.

You have curated investigation timeline events, communication timeline data, and email evidence.

Guidelines:
- Present events in strict chronological order.
- For each event, cite the source: curated timeline (verified) or email evidence (derived).
- Distinguish clearly between curated facts and email-derived observations.
- If asking about communication patterns over time, include volume trends.
- Note any gaps in temporal coverage.
- Do NOT fabricate dates or events not present in the data."""


KEYWORD_SEARCH_SYNTHESIS = """You are a corporate communications analyst answering a question about a topic, project, or theme at Enron.

You have email search results, entity mentions, and entity context for the topic.

Guidelines:
- Identify the key people involved with the topic from email evidence.
- Group related emails by sub-theme where possible.
- Cite specific email evidence: dates, senders, subjects, body previews.
- Note the volume of evidence (how many emails mention this topic).
- If an entity was found matching the keyword, include its profile.
- Do NOT fabricate discussion content not supported by the data."""


GENIE_ANALYTICS_SYNTHESIS = """You are a corporate communications analyst presenting Genie Space analytical results about Enron.

You have been given pre-fetched data from a Genie Space SQL query and optional data quality enrichment.

Guidelines:
- Present the analytical results with context.
- Note any data quality caveats from the enrichment.
- If the Genie query failed, explain the limitation.
- Do NOT fabricate analytical results not present in the data."""


# ---------------------------------------------------------------------------
# The 6 MECE computational primitives + genie_analytics
# ---------------------------------------------------------------------------

PATTERN_REGISTRY: dict[str, Pattern] = {

    "entity_structure": Pattern(
        name="entity_structure",
        synthesis_prompt=ENTITY_STRUCTURE_SYNTHESIS,
        steps=[
            ExecutionStep("query_org_hierarchy", {
                "entity_name": "$ENTITY",
            }),
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
        ],
        min_confidence=0.0,
    ),

    "entity_explore": Pattern(
        name="entity_explore",
        synthesis_prompt=ENTITY_EXPLORE_SYNTHESIS,
        steps=[
            ExecutionStep("find_top_contacts", {
                "entity_name": "$ENTITY",
                "direction": "both",
                "limit": 15,
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "DISCUSSES",
            }),
            ExecutionStep("get_entity_summary", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.0,
    ),

    "entity_pair": Pattern(
        name="entity_pair",
        synthesis_prompt=ENTITY_PAIR_SYNTHESIS,
        steps=[
            ExecutionStep("trace_path", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("get_emails_between", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("get_dyad_topics", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.0,
    ),

    "timeline": Pattern(
        name="timeline",
        synthesis_prompt=TIMELINE_SYNTHESIS,
        steps=[
            ExecutionStep("query_timeline", {
                "person_name": "$ENTITY",
                "date_from": "$DATE_FROM",
                "date_to": "$DATE_TO",
            }),
            ExecutionStep("get_communication_timeline", {
                "entity_name": "$ENTITY",
                "date_from": "$DATE_FROM",
                "date_to": "$DATE_TO",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
        ],
        min_confidence=0.0,
    ),

    "keyword_search": Pattern(
        name="keyword_search",
        synthesis_prompt=KEYWORD_SEARCH_SYNTHESIS,
        steps=[
            ExecutionStep("search_emails", {
                "keywords": "$KEYWORDS",
            }),
            ExecutionStep("get_context_verses", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("find_entity", {
                "name": "$ENTITY",
            }),
        ],
        min_confidence=0.0,
    ),

    "general": Pattern(
        name="general",
        synthesis_prompt="",
        steps=[],
        min_confidence=0.0,
    ),

    "genie_analytics": Pattern(
        name="genie_analytics",
        synthesis_prompt=GENIE_ANALYTICS_SYNTHESIS,
        steps=[
            ExecutionStep("query_and_enrich", {
                "question": "$QUESTION",
            }),
        ],
        min_confidence=0.0,
    ),
}


def resolve_params(
    params: dict,
    entities: list[dict],
    *,
    metadata: dict | None = None,
    question: str = "",
) -> dict:
    """Replace $-prefixed placeholders in tool params.

    Supported placeholders:
      $ENTITY, $ENTITY_B, $KEYWORDS, $DATE_FROM, $DATE_TO, $QUESTION

    Args:
        params: Template params with $-prefixed placeholders.
        entities: Extracted entities from the classifier.
        metadata: Optional dict with 'date_from', 'date_to', 'keywords' keys.
        question: Original user question for $QUESTION substitution.
    """
    resolved = {}
    primary = entities[0]["name"] if entities else ""
    secondary = entities[1]["name"] if len(entities) > 1 else ""
    meta = metadata or {}

    for key, value in params.items():
        if isinstance(value, str):
            value = value.replace("$ENTITY_B", secondary)
            value = value.replace("$KEYWORDS", meta.get("keywords", primary))
            value = value.replace("$ENTITY", primary)
            value = value.replace("$DATE_FROM", meta.get("date_from", ""))
            value = value.replace("$DATE_TO", meta.get("date_to", ""))
            value = value.replace("$QUESTION", question)
        resolved[key] = value
    return resolved
