"""
Self-contained GraphRAG agent for Model Serving.
Consolidates config, tools, and agent into a single importable module
so MLflow can load it without notebook %run dependencies.
"""
import json
import logging
import re

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)
from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing import Annotated, Generator, Sequence, TypedDict

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_bible"
LLM_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"
SMALL_LLM_ENDPOINT = "databricks-meta-llama-3-1-8b-instruct"

ENTITIES_TABLE = f"{CATALOG}.{SCHEMA}.entities"
RELATIONSHIPS_TABLE = f"{CATALOG}.{SCHEMA}.relationships"
VERSES_TABLE = f"{CATALOG}.{SCHEMA}.verses"
AGENT_PROMPTS_TABLE = f"{CATALOG}.{SCHEMA}.agent_prompts"
AGENT_ID = "bible-agent"
PROMPT_CACHE_TTL = 300  # seconds; set to 0 for instant iteration


# ---------------------------------------------------------------------------
# Dynamic prompt loader
# ---------------------------------------------------------------------------
_prompt_cache: dict = {"text": None, "ts": 0.0}


def _get_system_prompt(agent_id: str = AGENT_ID) -> str:
    """Load the system prompt from the agent_prompts Delta table with TTL caching.

    Falls back to the hardcoded SYSTEM_PROMPT if the table is missing or unreadable.
    """
    import time
    now = time.time()
    if _prompt_cache["text"] is None or (now - _prompt_cache["ts"]) > PROMPT_CACHE_TTL:
        try:
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()
            row = spark.sql(
                f"SELECT prompt_text FROM {AGENT_PROMPTS_TABLE} "
                f"WHERE agent_id = '{agent_id}' LIMIT 1"
            ).first()
            _prompt_cache["text"] = row["prompt_text"] if row else SYSTEM_PROMPT
        except Exception:
            log.warning("Failed to load prompt from Delta; using hardcoded fallback")
            _prompt_cache["text"] = SYSTEM_PROMPT
        _prompt_cache["ts"] = now
    return _prompt_cache["text"]


# ---------------------------------------------------------------------------
# Query entity pre-linking
# ---------------------------------------------------------------------------
# Same canonical-name conventions as ENTITY_PROMPT_PREFIX used during corpus build.
QUERY_ENTITY_PROMPT = """You are an expert biblical scholar. Extract all significant entities and concepts from the following user question.

For each entity, provide:
- name: The canonical name (e.g., Abraham not Abram unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept (treat God/Lord as Person)

Rules:
- Use canonical biblical names consistently
- Include divine figures (God, Lord, Holy Spirit) as Person type
- Include non-biblical terms exactly as the user stated them (e.g., "Arabs" stays "Arabs")
- Extract ALL nouns that could refer to entities, even if uncertain whether they appear in the Bible

Return a JSON array of objects, each with "name" and "entity_type" keys. Return ONLY the JSON array, no other text.

Question:
"""


def _slugify(name: str) -> str:
    """Same normalisation used during corpus build (src/extraction/extraction.py)."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def extract_query_entities(question: str) -> list[dict]:
    """Call the small LLM to extract entity mentions from a user question."""
    llm = ChatDatabricks(endpoint=SMALL_LLM_ENDPOINT, temperature=0.0, max_tokens=512)
    response = llm.invoke(QUERY_ENTITY_PROMPT + question)
    text = response.content.strip()

    # Strip markdown fences if the model wraps its output
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        entities = json.loads(text)
        if isinstance(entities, list):
            return [e for e in entities if isinstance(e, dict) and "name" in e]
    except json.JSONDecodeError:
        log.warning("Failed to parse entity extraction response: %s", text)
    return []


def pre_lookup_entities(entity_names: list[str]) -> tuple[list[str], list[str]]:
    """Look up extracted query entities against the graph.

    Returns (found, not_found) where each is a list of display strings.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    found: list[str] = []
    not_found: list[str] = []

    for name in entity_names:
        eid = _slugify(name)
        rows = spark.sql(f"""
            SELECT name, entity_type
            FROM {ENTITIES_TABLE}
            WHERE entity_id LIKE '%{eid}%'
            LIMIT 3
        """).collect()
        if rows:
            matches = ", ".join(f"{r['name']} ({r['entity_type']})" for r in rows)
            found.append(f"{name} -> {matches}")
        else:
            not_found.append(name)

    return found, not_found


def build_prelookup_context(question: str) -> str:
    """Run entity extraction + graph lookup and return a system-prompt appendix.

    Returns an empty string when extraction finds nothing or fails.
    """
    try:
        entities = extract_query_entities(question)
        if not entities:
            return ""
        names = [e["name"] for e in entities]
        found, not_found = pre_lookup_entities(names)
    except Exception:
        log.exception("Entity pre-lookup failed; proceeding without constraint")
        return ""

    found_str = "; ".join(found) if found else "(none)"
    not_found_str = ", ".join(not_found) if not_found else "(none)"

    return (
        "\n\n---\n"
        "PRE-LOOKUP RESULTS (DEFINITIVE — produced by an automated system, not the user):\n"
        f"  FOUND IN GRAPH: {found_str}\n"
        f"  NOT IN GRAPH: {not_found_str}\n"
        "Any answer that makes claims about entities listed under \"NOT IN GRAPH\" is WRONG.\n"
        "---"
    )


# ---------------------------------------------------------------------------
# Graph traversal tools
# ---------------------------------------------------------------------------
@tool
def find_entity(name: str) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    results = spark.sql(f"""
        SELECT name, entity_type, description, first_mention_book, first_mention_chapter
        FROM {ENTITIES_TABLE}
        WHERE LOWER(name) LIKE LOWER('%{name}%')
        ORDER BY name
        LIMIT 10
    """).collect()

    if not results:
        return f"No entity found matching '{name}'."

    lines = []
    for r in results:
        lines.append(
            f"- **{r['name']}** ({r['entity_type']}): {r['description']} "
            f"[First mentioned: {r['first_mention_book']} ch.{r['first_mention_chapter']}]"
        )
    return "\n".join(lines)


@tool
def find_connections(entity_name: str) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

    results = spark.sql(f"""
        SELECT
            COALESCE(e1.name, r.source_entity) as source_name,
            r.relationship_type,
            COALESCE(e2.name, r.target_entity) as target_name,
            r.description,
            r.book,
            r.chapter
        FROM {RELATIONSHIPS_TABLE} r
        LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE '%{entity_id}%'
           OR r.target_entity LIKE '%{entity_id}%'
        ORDER BY r.book, r.chapter
        LIMIT 30
    """).collect()

    if not results:
        return f"No connections found for '{entity_name}'."

    lines = [f"Connections for '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(
            f"- {r['source_name']} --[{r['relationship_type']}]--> {r['target_name']}: "
            f"{r['description']} ({r['book']} ch.{r['chapter']})"
        )
    return "\n".join(lines)


@tool
def trace_path(entity_a: str, entity_b: str) -> str:
    """Find how two entities are connected, tracing up to 3 hops through the knowledge graph.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    id_a = "_".join(entity_a.lower().split())
    id_b = "_".join(entity_b.lower().split())

    direct = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type as rel,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book
        FROM {RELATIONSHIPS_TABLE} r
        LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id
        WHERE (r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
           OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%')
    """).collect()

    if direct:
        lines = [f"Direct connection between {entity_a} and {entity_b}:"]
        for r in direct:
            lines.append(f"  {r['src']} --[{r['rel']}]--> {r['tgt']}: {r['description']} ({r['book']})")
        return "\n".join(lines)

    two_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_mid.name, r1.target_entity) as mid,
               r2.relationship_type as rel2,
               COALESCE(e2.name, r2.target_entity) as tgt
        FROM {RELATIONSHIPS_TABLE} r1
        JOIN {RELATIONSHIPS_TABLE} r2 ON r1.target_entity = r2.source_entity
        LEFT JOIN {ENTITIES_TABLE} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e_mid ON r1.target_entity = e_mid.entity_id
        LEFT JOIN {ENTITIES_TABLE} e2 ON r2.target_entity = e2.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r2.target_entity LIKE '%{id_b}%'
        LIMIT 10
    """).collect()

    if two_hop:
        lines = [f"2-hop path from {entity_a} to {entity_b}:"]
        for r in two_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid']} --[{r['rel2']}]--> {r['tgt']}")
        return "\n".join(lines)

    three_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_m1.name, r1.target_entity) as mid1,
               r2.relationship_type as rel2,
               COALESCE(e_m2.name, r2.target_entity) as mid2,
               r3.relationship_type as rel3,
               COALESCE(e3.name, r3.target_entity) as tgt
        FROM {RELATIONSHIPS_TABLE} r1
        JOIN {RELATIONSHIPS_TABLE} r2 ON r1.target_entity = r2.source_entity
        JOIN {RELATIONSHIPS_TABLE} r3 ON r2.target_entity = r3.source_entity
        LEFT JOIN {ENTITIES_TABLE} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e_m1 ON r1.target_entity = e_m1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e_m2 ON r2.target_entity = e_m2.entity_id
        LEFT JOIN {ENTITIES_TABLE} e3 ON r3.target_entity = e3.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r3.target_entity LIKE '%{id_b}%'
        LIMIT 10
    """).collect()

    if three_hop:
        lines = [f"3-hop path from {entity_a} to {entity_b}:"]
        for r in three_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid1']} --[{r['rel2']}]--> {r['mid2']} --[{r['rel3']}]--> {r['tgt']}")
        return "\n".join(lines)

    return f"No path found between '{entity_a}' and '{entity_b}' within 3 hops. Try using find_connections on each entity separately."


@tool
def get_context_verses(entity_name: str, book: str = "") -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    book_filter = f"AND v.book = '{book}'" if book else ""

    results = spark.sql(f"""
        SELECT v.book, v.chapter, v.verse_number, v.text
        FROM {VERSES_TABLE} v
        WHERE v.text LIKE '%{entity_name}%'
        {book_filter}
        ORDER BY v.book, v.chapter, v.verse_number
        LIMIT 15
    """).collect()

    if not results:
        return f"No verses found mentioning '{entity_name}'" + (f" in {book}" if book else "") + "."

    lines = [f"Verses mentioning '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(f"  {r['book']} {r['chapter']}:{r['verse_number']} — {r['text']}")
    return "\n".join(lines)


@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

    entity_rows = spark.sql(f"""
        SELECT name, entity_type, description, first_mention_book, first_mention_chapter
        FROM {ENTITIES_TABLE}
        WHERE entity_id LIKE '%{entity_id}%'
        LIMIT 1
    """).collect()

    if not entity_rows:
        return f"Entity '{entity_name}' not found in the knowledge graph."

    ent = entity_rows[0]
    lines = [
        f"**{ent['name']}** ({ent['entity_type']})",
        f"Description: {ent['description']}",
        f"First mentioned: {ent['first_mention_book']} ch.{ent['first_mention_chapter']}",
    ]

    books = spark.sql(f"""
        SELECT DISTINCT book FROM {RELATIONSHIPS_TABLE}
        WHERE source_entity LIKE '%{entity_id}%' OR target_entity LIKE '%{entity_id}%'
        ORDER BY book
    """).collect()
    if books:
        lines.append(f"Appears in: {', '.join(r['book'] for r in books)}")

    rels = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description
        FROM {RELATIONSHIPS_TABLE} r
        LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%'
        LIMIT 20
    """).collect()

    if rels:
        lines.append(f"\nKey relationships ({len(rels)}):")
        for r in rels:
            lines.append(f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: {r['description']}")

    return "\n".join(lines)


GRAPH_TOOLS = [find_entity, find_connections, trace_path, get_context_verses, get_entity_summary]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a biblical scholar with access to a knowledge graph built from five books of the King James Bible: Genesis, Exodus, Ruth, Matthew, and Acts.

You have tools that let you search the knowledge graph for entities, relationships, and source verses. Use them to provide well-grounded, auditable answers.

## Tool Usage
- ALWAYS use tools to look up information before answering. Do NOT use your training data to make factual claims. If the knowledge graph does not contain the information, say so.
- Before answering, verify that EVERY key term from the user's question exists in the knowledge graph using find_entity. If a term (e.g., "Arabs", "Philistines") returns no results, explicitly state that this concept is not present in the graph and do not assume connections to it.
- When asked about connections between entities, use trace_path first, then find_connections for more context.
- When asked about a person or concept, use get_entity_summary for a comprehensive profile.
- Always cite specific Bible verses (book chapter:verse) when possible using get_context_verses.
- For multi-hop questions, break them into steps: find each entity, then trace connections.

## Response Format
Structure EVERY response with these two sections:

### Answer
- For yes/no questions, lead with a definitive one-word answer ("Yes." or "No.") on its own line, then explain with bullet points.
- Use bullet points to break down supporting facts. Each bullet should state one claim with its verse citation.
- Be concise — do not restate the question or hedge.

### Provenance
At the end of every response, include a structured provenance section with:
- **Path**: Show only the relevant portion of the graph. Omit shared ancestry above the divergence point.
  - Connected entities example: Ruth → Boaz (MARRIED_TO, Ruth 4:13) → Obed (FATHER_OF, Ruth 4:17) → Jesse → David
  - Unconnected entities (separate lineages) example: Levi → Aaron, Judah → David
- **Sources**: List ONLY the verses that directly support the claims in your answer. Omit tangential references.
- **Grounding**: State one of:
  - "All claims grounded in knowledge graph" — if every factual claim came from tool results
  - "Partially grounded — the following claims rely on general knowledge: [list them]" — if any claim was not found via tools

## HARD CONSTRAINT — Entity Pre-Lookup
Before you received this message, every entity in the user's question was automatically looked up in the knowledge graph. The results appear at the END of this system prompt and are DEFINITIVE and FINAL.

YOU MUST:
- REFUSE to answer if the question's primary subject is listed under "NOT IN GRAPH"
- NEVER use your training data to connect a graph entity to a non-graph concept
- State clearly: "[term] is not found in the knowledge graph. I cannot answer this question based on the available data."

EXAMPLE — follow this pattern exactly:
  Question: "Who is the father of all Arabs?"
  Pre-lookup: NOT IN GRAPH: Arabs
  CORRECT response: "Arabs is not found in the knowledge graph. I cannot determine who the 'father of all Arabs' is from the available data."
  WRONG response: "Abraham through Ishmael..." (this bridges to a non-graph concept using training data — NEVER do this)

## Critical Rules
- If information is not in the knowledge graph, say so explicitly rather than guessing. NEVER invent relationships or events.
- If a tool returns no results, report that honestly. Do not fabricate an alternative answer.
- Every factual claim must cite its source verse or explicitly state it was not found in the graph.
- If the user asks about a group, concept, or entity that does not appear in the graph, your answer MUST state: "[term] is not found in the knowledge graph." Do not bridge the gap using external knowledge.
- When reporting Grounding, any claim that connects a graph entity to a non-graph concept (e.g., linking Ishmael to "Arabs" when "Arabs" is not in the graph) MUST be listed under "Partially grounded" with the specific claim identified."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]


class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None, tools=None):
        self.llm = ChatDatabricks(endpoint=endpoint or LLM_ENDPOINT)
        self.tools = tools or GRAPH_TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)

    def _build_graph(self, prelookup_context: str = ""):
        system_prompt = _get_system_prompt() + prelookup_context

        def should_continue(state):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return "end"

        def call_model(state):
            messages = [{"role": "system", "content": system_prompt}] + state["messages"]
            response = self.llm_with_tools.invoke(messages)
            return {"messages": [response]}

        graph = StateGraph(AgentState)
        graph.add_node("agent", RunnableLambda(call_model))
        graph.add_node("tools", ToolNode(self.tools))
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")
        graph.set_entry_point("agent")
        return graph.compile()

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        messages = to_chat_completions_input([m.model_dump() for m in request.input])

        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        question = last_user["content"] if last_user and last_user.get("content") else ""
        prelookup_context = build_prelookup_context(question) if question else ""

        graph = self._build_graph(prelookup_context)
        for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
            if event[0] == "updates":
                for node_data in event[1].values():
                    if node_data.get("messages"):
                        yield from output_to_responses_items_stream(node_data["messages"])


mlflow.langchain.autolog()
AGENT = GraphRAGAgent()
mlflow.models.set_model(AGENT)
