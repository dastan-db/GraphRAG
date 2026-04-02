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
    parallel_steps: bool = True


# ---------------------------------------------------------------------------
# Synthesis prompts for the 6 MECE primitives
# ---------------------------------------------------------------------------

EXHAUSTIVE_RULE = """

## CRITICAL: Exhaustive Presentation
- Present EVERY person, relationship, title, and date returned by the tools. Do NOT summarize away details.
- If tools returned N people, name ALL N with their roles.
- If date ranges (effective_from, effective_to) were returned, INCLUDE them.
- If the graph has NO data for part of the question, say exactly what was not found.
- Do NOT add information from your training data. Base your answer ONLY on the tool results.
- Present each fact ONCE in the most appropriate section. Do not restate across sections.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
"""

EVIDENCE_CITATION_RULE = """

## CRITICAL: Evidence Citation
- For EVERY factual claim about reporting relationships or organizational structure, cite supporting email evidence when available.
- Use this citation format inline: [YYYY-MM-DD, From: sender, Subject: topic]
- Present cited emails in the Supporting Evidence table ONLY if tools returned actual email data. If no emails were retrieved, omit the table and state: "No email evidence was retrieved for this query."
- When get_hierarchy_evidence returns results, cite the top evidence emails. If it returns no results, say so honestly — do NOT fabricate citations.
- Compute confidence per claim based on evidence count: High (3+ emails), Medium (1-2 emails), Low (0 emails, curated data only).
- If evidence_available=true was returned by query_org_hierarchy, you MUST call get_hierarchy_evidence before responding.
- If get_emails_between returns empty, check the resolution.correction field for typos. Try get_relationship_evidence as a bridge. If still no evidence, report honestly that no direct emails were found.
- NEVER fabricate email citations. Every citation must correspond to a specific email returned by a tool.

## CRITICAL: Email Body Evidence (get_email_full_body results)
- If get_email_full_body results are present in the tool data, you MUST quote relevant body passages that support your claims.
- Format body quotes as: > "...[relevant excerpt from email body]..." — [YYYY-MM-DD, From: sender]
- Full email bodies provide the STRONGEST evidence. Always prioritize body quotes over metadata-only citations.
- If the body text directly proves a reporting relationship (e.g., "Per your direction...", "As you requested...", "Report to..."), quote it verbatim.
"""


ENTITY_STRUCTURE_SYNTHESIS = """You are a corporate communications analyst answering a question about organizational hierarchy at Enron.

You have curated org hierarchy data (from SEC filings/DOJ records), graph relationships, and an entity summary. The curated data is the PRIMARY source of truth — it has verified reporting lines with temporal validity.

Guidelines:
- PRIORITIZE curated org_hierarchy results over LLM-extracted relationships when they conflict.
- List ALL people found with their roles/titles and effective date ranges — present EVERY person returned by the tools, not a subset.
- Pay attention to edge direction: in REPORTS_TO, the source reports to the target. In MANAGES, the source manages the target.
- Show organizational paths with → notation (e.g., "Delainey → Skilling → Lay").
- Note temporal changes in reporting structure (e.g., "reported to X until Aug 2001, then to Y") — include effective_from and effective_to dates.
- When asked "who reported to X", list EVERY person found in REPORTS_TO relationships with X as target AND in MANAGES relationships with X as source.
- When asked about a division or project, include BOTH the people who managed it AND the people who reported to those managers.
- Only cite emails that DIRECTLY support a specific claim. Do NOT cite news digests or mass emails as evidence for org structure.
- If the curated data is comprehensive, state its source (SEC filings, DOJ, congressional testimony).
- Present EVERY piece of evidence returned by tools. Do NOT summarize away details.
- Do NOT fabricate relationships not present in the data."""


ENTITY_EXPLORE_SYNTHESIS = """You are a corporate communications analyst answering a question about an Enron employee's activities and connections.

You have actual email evidence, org hierarchy data, a ranked contact list, discussion topics, an entity profile, and communication statistics.

Guidelines:
- LEAD WITH EMAIL EVIDENCE. Your response MUST start with the person's role, then IMMEDIATELY present actual email quotes from get_source_evidence, get_hierarchy_evidence, get_emails_between, or get_email_full_body results.
- For each email found, show: date, sender, recipient(s), subject, and a body excerpt if available.
- After presenting email evidence, summarize their top contacts with communication volumes.
- If asked "who communicated most frequently", present the top contact with exact count FIRST, then list others in descending order.
- Identify their main discussion topics from relationship and email data.
- Cite specific email evidence [YYYY-MM-DD, From: sender, Subject: topic] when available.
- Note directional patterns (who initiated more, sent vs received counts).
- NEVER fabricate email rows in the evidence table. Only include emails returned by tools.
- NEVER invent dates, senders, subjects, or body text that do not appear verbatim in the tool results.
- Present EVERY piece of email evidence returned by tools. Do NOT summarize away email details.
- Do NOT fabricate activities or contacts not in the data.
- If you are uncertain whether an email exists, check the tool results before citing it. Absence of evidence ≠ evidence of absence — say "not found" rather than guessing.
- Present each fact ONCE in the most appropriate section.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
- In the Provenance section, list claim labels with confidence levels only."""


ENTITY_PAIR_SYNTHESIS = """You are a corporate communications analyst answering a question about the relationship between two people at Enron.

You have path data, direct emails between them, shared discussion topics, and relationship data.

Guidelines:
- Walk through each hop in any connection path using → notation, including intermediate entities.
- Quantify their direct communication: exact email count, direction (A→B vs B→A), date range.
- Use 'sent' for one-directional communication and 'exchanged' ONLY when BOTH directions have non-zero email counts. If sent_a_to_b > 0 but sent_b_to_a == 0, say "A sent N emails to B" not "A and B exchanged N emails".
- If the tool output includes direction_summary or sent_a_to_b/sent_b_to_a fields, use those exact counts.
- If asked "did they communicate directly", give a clear YES/NO first, then the evidence.
- List ALL shared discussion topics with evidence.
- Note the relationship types (REPORTS_TO, COLLABORATES_WITH, SENT_TO) with direction.
- Cite specific emails [YYYY-MM-DD, From: sender, Subject: topic] that illuminate their relationship.
- If there is NO direct communication (get_emails_between returned 0 emails), explain the indirect connection path clearly and state "No direct email exchange was found between these two people."
- If find_connections shows SENT_TO edges but get_emails_between returned 0, explain this discrepancy: the graph edges come from NLP extraction and may reflect body mentions rather than direct header-to-header exchanges.
- Present EVERY piece of evidence returned by tools. Do NOT summarize away details.
- Do NOT fabricate connections, emails, or citations not present in the tool results.
- If a tool's resolution metadata includes a correction (e.g., spelling fix), mention it to the user.
- NEVER include an email in the Supporting Evidence table unless a tool actually returned that email.
- Present each fact ONCE in the most appropriate section. Do not restate counts or relationships across multiple sections.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
- In the Provenance section, list claim labels with confidence levels only. Do not re-explain evidence already presented in the body."""


TIMELINE_SYNTHESIS = """You are a corporate communications analyst answering a question about events and timelines at Enron.

You have curated investigation timeline events, communication timeline data, email search results, and semantic search results.

Guidelines:
- Present events in strict chronological order.
- Lead with deterministic timeline rows and date-bounded communication counts before using semantic-search snippets.
- For each event, cite the source: curated timeline (verified) or email evidence (derived).
- Distinguish clearly between curated facts and email-derived observations.
- If asking about communication patterns over time, include volume trends and compare before/after periods.
- Cross-reference investigation timeline events with email volume data when both are available.
- Name specific people involved in each event with their roles (e.g., "Jeff Skilling (CEO)" not just "the CEO").
- When multiple executives are involved, present each person's role and actions.
- Note any gaps in temporal coverage.
- If limited data was returned, state clearly what evidence IS available and what is missing.
- Do NOT fabricate dates, events, or participants not present in the data.
- Do NOT fill gaps with general knowledge — only report what the data shows.
- Present each fact ONCE in the most appropriate section. Do not restate across sections.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
- In the Provenance section, list claim labels with confidence levels only. Do not re-explain evidence already presented in the body."""


KEYWORD_SEARCH_SYNTHESIS = """You are a corporate communications analyst answering a question about a topic, project, or theme at Enron.

You have email search results, topic taxonomy data, investigation timeline events, entity mentions, and entity context for the topic.

Guidelines:
- Address ALL aspects of the question — if asked about 'financial events', cover earnings, stock events, SEC filings, restatements, and any other financial events found in the data.
- If the question mentions a concept (e.g., SPE, broadband, California energy), explain what the graph shows about ALL related entities (people, organizations, projects).
- Identify the key people involved with the topic from email evidence — name EVERY person found, with their role.
- Group related emails by sub-theme where possible.
- Cite specific email evidence: dates, senders, subjects, body previews.
- Note the volume of evidence (how many emails mention this topic).
- Cross-reference email evidence with curated timeline events when the topic has temporal relevance.
- Use the topic taxonomy to identify related themes and sub-topics.
- If an entity was found matching the keyword, include its full profile and connections.
- Present EVERY piece of evidence returned by tools. Do NOT summarize away details.
- If the graph has NO data for part of the question, say exactly what was not found.
- Do NOT fabricate discussion content not supported by the data.
- Present each fact ONCE in the most appropriate section. Do not restate across sections.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
- In the Provenance section, list claim labels with confidence levels only. Do not re-explain evidence already presented in the body."""


GENERAL_SYNTHESIS = """You are a corporate communications analyst answering a broad question about Enron.

You have email search results, entity profiles, topic distributions, investigation timeline events, network-level summaries (top individuals, top email pairs), and graph coverage statistics.

Guidelines:
- Synthesize ACROSS entity types: connect Person activities to Organization structures to Financial_Events.
- Open with curated timeline facts (verified public record) when available.
- Support claims with email evidence: cite dates, senders, subjects, and body previews.
- Cross-reference: if timeline says "X resigned", check if email volume for X dropped at that time.
- Use topic distribution data to identify major themes and their prevalence.
- Use network-level data (top individuals, top email pairs) to identify key players when no specific entity is given.
- Use corpus coverage statistics to provide quantitative context (total emails, entities, relationships).
- For "why" questions, structure your answer as multiple contributing factors (organizational, financial, legal, communication dimensions) with evidence for each factor.
- For broad questions, organize your answer thematically with clear headers covering all relevant dimensions.
- Mention key people and their roles if found via entity lookup — name ALL of them.
- Distinguish clearly between curated facts (timeline, org_hierarchy) and email-derived observations.
- Present EVERY piece of evidence returned by tools. Do NOT summarize away details.
- If the graph has NO data for part of the question, say exactly what was not found — do NOT fill gaps from training knowledge.
- If you add context beyond tool data, you MUST prefix it: 'Beyond the graph data, it is generally known that...'
- Never claim 'All claims grounded in graph data' if you added ANY information not from the tool results.
- Do NOT fabricate facts, relationships, or email citations not present in the data.
- Present each fact ONCE in the most appropriate section. Do not restate across sections.
- If a tool returned an error message, briefly note the limitation — do not pass raw error strings to the user.
- In the Provenance section, list claim labels with confidence levels only. Do not re-explain evidence already presented in the body."""


GENIE_ANALYTICS_SYNTHESIS = """You are a corporate communications analyst presenting Genie Space analytical results about Enron.

You have been given pre-fetched data from a Genie Space SQL query and optional data quality enrichment.

Guidelines:
- Present the analytical results CONCISELY — prefer tables and short summaries over narrative paragraphs.
- For ranked results, present as a numbered list or table. Do NOT wrap each item in a paragraph.
- Note any data quality caveats from the enrichment.
- If the Genie query failed, explain the limitation.
- Do NOT fabricate analytical results not present in the data.
- If results include `sent_a_to_b` / `sent_b_to_a`, quote those exact fields and describe direction explicitly.
- If results include `sent_to_contact` / `received_from_contact`, report total first and then the directional breakdown in parentheses.
- Use 'sent' for one-directional communication and 'exchanged' ONLY when both directions have non-zero counts.
- If a result only gives a total count, describe it as a total direct-email count. Do NOT imply a balanced exchange.
- Do not label a row as 'sent' or 'received' unless the matching directional field is present and non-zero.
- Keep the response proportional to the question — a simple count question deserves a one-sentence answer, not a multi-section report."""


# ---------------------------------------------------------------------------
# The 6 MECE computational primitives + genie_analytics
# ---------------------------------------------------------------------------

PATTERN_REGISTRY: dict[str, Pattern] = {

    "entity_structure": Pattern(
        name="entity_structure",
        synthesis_prompt=ENTITY_STRUCTURE_SYNTHESIS + EVIDENCE_CITATION_RULE,
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
            ExecutionStep("get_source_evidence", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_hierarchy_evidence", {
                "person_name": "$ENTITY",
            }),
        ],
        min_confidence=0.0,
    ),

    "entity_explore": Pattern(
        name="entity_explore",
        synthesis_prompt=ENTITY_EXPLORE_SYNTHESIS + EVIDENCE_CITATION_RULE,
        steps=[
            ExecutionStep("get_source_evidence", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_hierarchy_evidence", {
                "person_name": "$ENTITY",
            }),
            ExecutionStep("query_org_hierarchy", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_entity_summary", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("find_top_contacts", {
                "entity_name": "$ENTITY",
                "direction": "both",
                "limit": 5,
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "DISCUSSES",
            }),
            ExecutionStep("get_topic_distribution", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_communication_stats", {
                "entity_name": "$ENTITY",
                "group_by": "contact",
            }),
        ],
        min_confidence=0.0,
    ),

    "entity_pair": Pattern(
        name="entity_pair",
        synthesis_prompt=ENTITY_PAIR_SYNTHESIS + EVIDENCE_CITATION_RULE,
        steps=[
            ExecutionStep("get_emails_between", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("get_relationship_evidence", {
                "source_entity": "$ENTITY",
                "target_entity": "$ENTITY_B",
            }),
            ExecutionStep("get_dyad_topics", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("trace_path", {
                "entity_a": "$ENTITY",
                "entity_b": "$ENTITY_B",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("find_top_contacts", {
                "entity_name": "$ENTITY",
                "direction": "both",
                "limit": 10,
            }),
        ],
        min_confidence=0.0,
    ),

    "timeline": Pattern(
        name="timeline",
        synthesis_prompt=TIMELINE_SYNTHESIS + EVIDENCE_CITATION_RULE,
        steps=[
            ExecutionStep("query_timeline", {
                "person_name": "",
                "date_from": "$DATE_FROM",
                "date_to": "$DATE_TO",
            }),
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
            ExecutionStep("get_communication_stats", {
                "entity_name": "$ENTITY",
                "group_by": "month",
            }),
            ExecutionStep("search_emails", {
                "keywords": "$KEYWORDS",
            }),
            ExecutionStep("get_source_evidence", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("semantic_search_emails", {
                "query": "$QUESTION",
            }),
        ],
        min_confidence=0.0,
    ),

    "keyword_search": Pattern(
        name="keyword_search",
        synthesis_prompt=KEYWORD_SEARCH_SYNTHESIS + EVIDENCE_CITATION_RULE,
        steps=[
            ExecutionStep("search_emails", {
                "keywords": "$KEYWORDS",
            }),
            ExecutionStep("semantic_search_emails", {
                "query": "$QUESTION",
            }),
            ExecutionStep("browse_topics", {}),
            ExecutionStep("query_timeline", {
                "person_name": "",
                "date_from": "",
                "date_to": "",
            }),
            ExecutionStep("find_connections", {
                "entity_name": "$ENTITY",
                "relationship_type": "DISCUSSES",
            }),
            ExecutionStep("browse_topics", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_entity_context", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_source_evidence", {
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
        synthesis_prompt=GENERAL_SYNTHESIS + EVIDENCE_CITATION_RULE,
        steps=[
            ExecutionStep("search_emails", {
                "keywords": "$KEYWORDS",
            }),
            ExecutionStep("semantic_search_emails", {
                "query": "$QUESTION",
            }),
            ExecutionStep("browse_topics", {}),
            ExecutionStep("query_timeline", {
                "person_name": "",
                "date_from": "",
                "date_to": "",
            }),
            ExecutionStep("get_top_individuals", {}),
            ExecutionStep("get_top_email_pairs", {}),
            ExecutionStep("get_corpus_coverage", {}),
            ExecutionStep("get_topic_distribution", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("get_entity_context", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("query_org_hierarchy", {
                "entity_name": "$ENTITY",
            }),
            ExecutionStep("find_entity", {
                "name": "$ENTITY",
            }),
            ExecutionStep("get_source_evidence", {
                "entity_name": "$ENTITY",
            }),
        ],
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
