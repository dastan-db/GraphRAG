"""
Self-contained GraphRAG agent for Model Serving.
Consolidates config, tools, and agent into a single importable module
so MLflow can load it without notebook %run dependencies.
"""
import json
import logging
import os
import re
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_function_call_item,
    create_function_call_output_item,
    output_to_responses_items_stream,
    to_chat_completions_input,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing import Annotated, Generator, Protocol, Sequence, TypedDict

try:
    import litellm
    litellm.suppress_debug_info = True
except ImportError:
    pass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
SCHEMA = os.environ.get("GRAPHRAG_SCHEMA", "graphrag_bible")
LLM_ENDPOINT = os.environ.get("GRAPHRAG_LLM_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")
SMALL_LLM_ENDPOINT = os.environ.get("GRAPHRAG_SMALL_LLM_ENDPOINT", "databricks-meta-llama-3-1-8b-instruct")

ENTITIES_TABLE = f"{CATALOG}.{SCHEMA}.entities"
RELATIONSHIPS_TABLE = f"{CATALOG}.{SCHEMA}.relationships"
VERSES_TABLE = f"{CATALOG}.{SCHEMA}.verses"
AGENT_PROMPTS_TABLE = f"{CATALOG}.{SCHEMA}.agent_prompts"
ENTITY_ANALYTICS_TABLE = f"{CATALOG}.{SCHEMA}.entity_analytics"
AGENT_ID = "bible-agent"
PROMPT_CACHE_TTL = 300  # seconds; set to 0 for instant iteration
_PARALLEL_TOOLS = os.environ.get("GRAPHRAG_PARALLEL_TOOLS", "true").lower() == "true"
_CLASSIFY_PIPELINE = os.environ.get("GRAPHRAG_CLASSIFY_PIPELINE", "true").lower() == "true"

CORPUS = os.environ.get("GRAPHRAG_CORPUS", "bible")

VS_ENDPOINT = os.environ.get("GRAPHRAG_VS_ENDPOINT", "")
VS_INDEX_NAME = os.environ.get("GRAPHRAG_VS_INDEX_NAME", "")
EMBEDDING_ENDPOINT = os.environ.get("GRAPHRAG_EMBEDDING_ENDPOINT", "databricks-gte-large-en")

ENRON_SCHEMA = os.environ.get("GRAPHRAG_ENRON_SCHEMA", "graphrag_enron")
ENRON_ENTITIES_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entities"
ENRON_RELATIONSHIPS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.relationships"
ENRON_EMAILS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.emails"
ENRON_ENTITY_ANALYTICS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics"
ENRON_ENTITY_PATHS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_paths"
ENRON_ENTITY_MENTIONS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"
ENRON_COMMUNICATION_DYADS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.communication_dyads"
ENRON_PARTICIPANTS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.participants"
ENRON_THREADS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.threads"
ENRON_PERSON_ACTIVITY_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.person_activity"
ENRON_ORG_HIERARCHY_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.org_hierarchy"

ACCESS_TIER = os.environ.get("GRAPHRAG_ACCESS_TIER", "")

ENRON_ABAC_ENTITIES_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entities_abac"
ENRON_ABAC_RELATIONSHIPS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.relationships_abac"
ENRON_ABAC_EMAILS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.emails_abac"
ENRON_ABAC_ENTITY_ANALYTICS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics_abac"
ENRON_ABAC_ENTITY_PATHS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_paths_abac"
ENRON_ABAC_ENTITY_MENTIONS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions_abac"

BACKEND_TYPE = os.environ.get("GRAPHRAG_BACKEND", "databricks")
LLM_PROVIDER = os.environ.get("GRAPHRAG_LLM_PROVIDER", "databricks")


# ---------------------------------------------------------------------------
# DataBackend protocol — pluggable SQL execution layer
# ---------------------------------------------------------------------------
class DataBackend(Protocol):
    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]: ...


class DatabricksBackend:
    """Statement Execution API — production path for Model Serving."""

    def __init__(self):
        self._ws_client = None
        self._warehouse_id = None

    def _get_ws_client(self):
        if self._ws_client is None:
            from databricks.sdk import WorkspaceClient
            self._ws_client = WorkspaceClient()
        return self._ws_client

    def _get_warehouse_id(self) -> str:
        if self._warehouse_id is None:
            wid = os.environ.get("DATABRICKS_WAREHOUSE_ID")
            if wid:
                self._warehouse_id = wid
            else:
                w = self._get_ws_client()
                warehouses = list(w.warehouses.list())
                running = [wh for wh in warehouses if str(wh.state) == "RUNNING"]
                target = running[0] if running else warehouses[0] if warehouses else None
                if target is None:
                    raise RuntimeError("No SQL warehouse found in workspace")
                self._warehouse_id = target.id
                log.info("Auto-selected SQL warehouse: %s (%s)", target.name, target.id)
        return self._warehouse_id

    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]:
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState

        w = self._get_ws_client()
        wh = self._get_warehouse_id()
        parameters = (
            [StatementParameterListItem(name=k, value=v) for k, v in params.items()]
            if params else None
        )
        result = w.statement_execution.execute_statement(
            warehouse_id=wh,
            statement=query,
            catalog=CATALOG,
            schema=SCHEMA,
            parameters=parameters,
        )
        if result.status.state != StatementState.SUCCEEDED:
            msg = result.status.error.message if result.status.error else "Unknown error"
            raise RuntimeError(f"SQL execution failed: {msg}")
        if not result.manifest or not result.result:
            return []
        columns = [col.name for col in result.manifest.schema.columns]
        rows = []
        for row_data in result.result.data_array or []:
            rows.append(dict(zip(columns, row_data)))
        return rows


class LocalBackend:
    """DuckDB — local development with exported graph data."""

    _FQN_PREFIX = f"{CATALOG}.{SCHEMA}."

    def __init__(self, db_path: str | None = None):
        import duckdb
        path = db_path or os.environ.get("GRAPHRAG_LOCAL_DB", "data/graphrag.duckdb")
        self._conn = duckdb.connect(path, read_only=True)
        log.info("LocalBackend connected to %s", path)

    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]:
        query = query.replace(self._FQN_PREFIX, "")
        query = re.sub(r":(\w+)", r"$\1", query)
        result = self._conn.execute(query, params or {})
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


class LakebaseBackend:
    """Lakebase Autoscaling (Postgres) — low-latency OLTP with RLS.

    Connects via psycopg with Databricks OAuth tokens.  Uses a connection pool
    to avoid per-query TCP/TLS handshake and credential generation overhead.
    Supports session-level RLS context: call
    ``set_rls_context({"permitted_books": "Genesis,Exodus"})``
    to scope all subsequent queries via Postgres RLS policies.
    """

    _FQN_BIBLE = f"{CATALOG}.{SCHEMA}."
    _FQN_ENRON = f"{CATALOG}.{ENRON_SCHEMA}."

    def __init__(self):
        self._endpoint = os.environ.get(
            "LAKEBASE_ENDPOINT",
            "projects/graphrag/branches/production/endpoints/primary",
        )
        self._host: str | None = os.environ.get("LAKEBASE_HOST") or None
        self._dbname = os.environ.get("LAKEBASE_DBNAME", "databricks_postgres")
        self._rls_context: dict[str, str] = {}
        self._pool = None

    def set_rls_context(self, context: dict[str, str]):
        """Set session-level RLS variables applied on every new connection."""
        self._rls_context = dict(context)

    def _build_conninfo(self) -> str:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        host = self._host
        if not host:
            ep = w.postgres.get_endpoint(name=self._endpoint)
            host = ep.status.hosts.host
            self._host = host

        cred = w.postgres.generate_database_credential(endpoint=self._endpoint)
        username = w.current_user.me().user_name

        return (
            f"host={host} dbname={self._dbname} "
            f"user={username} password={cred.token} sslmode=require"
        )

    def _get_pool(self):
        if self._pool is None:
            from psycopg_pool import ConnectionPool
            conninfo = self._build_conninfo()
            self._pool = ConnectionPool(
                conninfo, min_size=2, max_size=10, open=True,
            )
        return self._pool

    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]:
        query = query.replace(self._FQN_BIBLE, "")
        query = query.replace(self._FQN_ENRON, "enron.")

        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for key, value in self._rls_context.items():
                    if value:
                        cur.execute(
                            "SELECT set_config(%s, %s, true)",
                            (f"app.{key}", str(value)),
                        )

                if params:
                    pg_query = re.sub(r":(\w+)", r"%(\1)s", query)
                    cur.execute(pg_query, params)
                else:
                    cur.execute(query)

                if cur.description is None:
                    return []
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]


def _get_backend() -> DataBackend:
    if BACKEND_TYPE == "local":
        return LocalBackend()
    if BACKEND_TYPE == "lakebase":
        return LakebaseBackend()
    return DatabricksBackend()


_backend: DataBackend = _get_backend()


# ---------------------------------------------------------------------------
# LLM factory — pluggable LLM provider
# ---------------------------------------------------------------------------
_DEFAULT_SEED = int(os.environ.get("GRAPHRAG_LLM_SEED", "42"))

_SEED_SUPPORTED_PROVIDERS = {"openai", "gateway"}


def _get_llm(endpoint: str = LLM_ENDPOINT, **kwargs):
    """Return a LangChain chat model for the configured provider."""
    if _DEFAULT_SEED and "seed" not in kwargs and LLM_PROVIDER in _SEED_SUPPORTED_PROVIDERS:
        kwargs["seed"] = _DEFAULT_SEED
    if LLM_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        return ChatOpenAI(model=model, **kwargs)
    if LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return ChatOllama(model=model, **kwargs)
    if LLM_PROVIDER == "gateway":
        from langchain_openai import ChatOpenAI
        base_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
        model = os.environ.get("GATEWAY_MODEL", "gpt-4o-mini")
        return ChatOpenAI(base_url=base_url, model=model, **kwargs)
    from databricks_langchain import ChatDatabricks
    return ChatDatabricks(endpoint=endpoint, **kwargs)


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
            rows = _backend.execute_sql(
                f"SELECT prompt_text FROM {AGENT_PROMPTS_TABLE} "
                "WHERE agent_id = :agent_id LIMIT 1",
                params={"agent_id": agent_id},
            )
            _prompt_cache["text"] = rows[0]["prompt_text"] if rows else SYSTEM_PROMPT
        except Exception:
            log.warning("Failed to load prompt from Delta; using hardcoded fallback")
            _prompt_cache["text"] = SYSTEM_PROMPT
        _prompt_cache["ts"] = now
    return _prompt_cache["text"]


_entity_cache: dict = {"data": {}, "ts": 0.0}
ENTITY_CACHE_TTL = 600  # 10 minutes


def _get_cached_entity(entity_id: str) -> dict | None:
    import time

    now = time.time()
    if (now - _entity_cache["ts"]) > ENTITY_CACHE_TTL:
        _entity_cache["data"].clear()
        _entity_cache["ts"] = now
    return _entity_cache["data"].get(entity_id)


def _set_cached_entity(entity_id: str, data: dict):
    _entity_cache["data"][entity_id] = data


_query_log: list[dict] = []


def _log_agent_query(
    user_query: str,
    classified_intent: str,
    tools_invoked: list[str],
    latency_ms: int,
    execution_path: str = "slow",
):
    """Log agent query for audit compliance. Appends to in-memory list, periodically flushed."""
    _query_log.append({
        "query_id": str(_uuid.uuid4()),
        "user_query": user_query[:1000],
        "classified_intent": classified_intent,
        "tools_invoked": tools_invoked,
        "execution_path": execution_path,
        "latency_ms": latency_ms,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
    })


# ---------------------------------------------------------------------------
# Query entity pre-linking
# ---------------------------------------------------------------------------
# Same canonical-name conventions as ENTITY_PROMPT_PREFIX used during corpus build.
_BIBLE_QUERY_ENTITY_PROMPT = """You are an expert biblical scholar. Extract all significant entities and concepts from the following user question.

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

_CORPORATE_QUERY_ENTITY_PROMPT = """You are a corporate communications analyst. Extract all significant entities and concepts from the following user question about the Enron email corpus.

For each entity, provide:
- name: The canonical name (e.g., "Kenneth Lay" not "Ken"; "Enron Broadband Services" not "broadband")
- entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

Rules:
- Use full canonical names for people when possible
- NEVER use title prefixes (Dr., Mr., Mrs.) — just the bare name
- Include company and division names as stated by the user
- Extract ALL nouns that could refer to entities in a corporate context
- Terms like "executives", "leadership", "management" should be extracted as Group-type concepts

Return a JSON array of objects, each with "name" and "entity_type" keys. Return ONLY the JSON array, no other text.

Question:
"""

_CORPORATE_CLASSIFY_AND_EXTRACT_PROMPT = """You are a corporate communications analyst. Given a user question about the Enron email corpus, do TWO things:

1. CLASSIFY the question into one of these patterns:
   - entity_structure: questions about reporting lines, org chart, roles, titles, who managed whom, who reported to whom, department structure, authority — "who reported to Jeff Skilling?", "what was Andrew Fastow's title?", "who is Kenneth Lay?"
   - entity_explore: questions about one person's activities, contacts, discussions, involvement, communications — "who did Jeff Skilling email most?", "what did Kenneth Lay discuss?", "what projects was Andrew Fastow involved in?"
   - entity_pair: questions about the relationship between TWO specific people — "how are Kenneth Lay and Tim Belden connected?", "what did Jeff Skilling and Andrew Fastow discuss?", "did they email each other?"
   - timeline: questions about events over time, what happened when, sequences, before/after, anomalies — "what happened in August 2001?", "timeline of the investigation", "when did Jeff Skilling resign?"
   - keyword_search: questions about a topic, project, deal, concept without a specific person anchor — "what was Project Raptor?", "California energy crisis", "special purpose entities", "document destruction"
   - genie_analytics: questions requiring SQL aggregation, statistical analysis, rankings, percentage calculations — "who sent the most emails?", "top email pairs", "what percentage of emails were internal?", "busiest communicators"
   - general: anything that doesn't clearly fit the above categories

2. EXTRACT all significant entities mentioned in the question.
   For each entity provide:
   - name: The canonical name (e.g., "Kenneth Lay" not "Ken")
   - entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

IMPORTANT: Only extract REAL named entities. Generic phrases like "two individuals", "someone", "the person", "each other" are NOT entities — return an empty entities list for those.

Return a JSON object with exactly this structure:
{"pattern": "<one of the 7 pattern names>", "confidence": <0.0 to 1.0>, "entities": [{"name": "...", "entity_type": "..."}]}

Return ONLY the JSON object, no other text.

Question:
"""

QUERY_ENTITY_PROMPT = _CORPORATE_QUERY_ENTITY_PROMPT if CORPUS == "enron" else _BIBLE_QUERY_ENTITY_PROMPT
CLASSIFY_AND_EXTRACT_PROMPT = _CORPORATE_CLASSIFY_AND_EXTRACT_PROMPT


def _slugify(name: str) -> str:
    """Same normalisation used during corpus build (src/extraction/extraction.py)."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def extract_query_entities(question: str) -> list[dict]:
    """Call the small LLM to extract entity mentions from a user question."""
    llm = _get_llm(endpoint=SMALL_LLM_ENDPOINT, temperature=0.0, max_tokens=512)
    response = llm.invoke(QUERY_ENTITY_PROMPT + question)
    text = response.content.strip()

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


def classify_and_extract(question: str) -> dict:
    """Extract entities AND classify question pattern in a single 8B LLM call.

    Returns {"pattern": str, "confidence": float, "entities": list[dict]}.
    Falls back to {"pattern": "general", "confidence": 0.0, "entities": [...]}
    if classification fails but entity extraction succeeds.
    """
    if CORPUS != "enron":
        entities = extract_query_entities(question)
        return {"pattern": "general", "confidence": 0.0, "entities": entities}

    llm = _get_llm(endpoint=SMALL_LLM_ENDPOINT, temperature=0.0, max_tokens=512)
    response = llm.invoke(CLASSIFY_AND_EXTRACT_PROMPT + question)
    text = response.content.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
        if isinstance(result, dict) and "pattern" in result:
            entities = result.get("entities", [])
            if isinstance(entities, list):
                entities = [e for e in entities if isinstance(e, dict) and "name" in e]
            return {
                "pattern": result.get("pattern", "general"),
                "confidence": float(result.get("confidence", 0.0)),
                "entities": entities,
            }
    except (json.JSONDecodeError, ValueError, TypeError):
        log.warning("Failed to parse classify_and_extract response: %s", text)

    entities = extract_query_entities(question)
    return {"pattern": "general", "confidence": 0.0, "entities": entities}


_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

_TEMPORAL_DATE_RE = re.compile(
    r"(?:(?:in|during|around|after|before|since)\s+)?"
    r"(?:(?:early|mid|late)\s+)?"
    r"(?:(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+)?(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)


def _truncate_json_aware(raw: str, limit: int = 8000) -> str:
    """Truncate a JSON string at a clean boundary (end of a JSON object or array element)."""
    if len(raw) <= limit:
        return raw
    cut = raw[:limit]
    for end_char in ('},', '"]', '}', ']'):
        pos = cut.rfind(end_char)
        if pos > limit // 2:
            cut = cut[:pos + len(end_char)]
            break
    if cut.count('[') > cut.count(']'):
        cut += ' ... ]'
    if cut.count('{') > cut.count('}'):
        cut += ' ... }'
    return cut


def _extract_temporal_metadata(question: str) -> dict:
    """Parse date references from a question to build date_from/date_to filters.

    Supports two modes:
    1. Literal dates: "August 2001", "in 2001", "between January 2001 and March 2001"
    2. Event references: "after Skilling resigned", "between SEC inquiry and bankruptcy"
       Uses ENRON_EVENT_DATES for event-to-date resolution.
    """
    matches = list(_TEMPORAL_DATE_RE.finditer(question))
    if matches:
        dates = []
        for m in matches:
            year = m.group("year")
            month_name = m.group("month")
            if month_name:
                mm = _MONTH_MAP[month_name.lower()]
                dates.append((year, mm))
            else:
                dates.append((year, None))

        if len(dates) == 1:
            year, mm = dates[0]
            if mm:
                date_from = f"{year}-{mm}-01"
                month_int = int(mm)
                if month_int == 12:
                    date_to = f"{int(year) + 1}-01-01"
                else:
                    date_to = f"{year}-{month_int + 1:02d}-01"
            else:
                date_from = f"{year}-01-01"
                date_to = f"{year}-12-31"
            return {"date_from": date_from, "date_to": date_to}

        years_months = sorted(dates)
        first_y, first_m = years_months[0]
        last_y, last_m = years_months[-1]
        date_from = f"{first_y}-{first_m or '01'}-01"
        if last_m:
            m_int = int(last_m)
            date_to = f"{last_y}-{m_int + 1:02d}-01" if m_int < 12 else f"{int(last_y) + 1}-01-01"
        else:
            date_to = f"{last_y}-12-31"
        return {"date_from": date_from, "date_to": date_to}

    return _resolve_event_dates(question)


def _resolve_event_dates(question: str) -> dict:
    """Resolve event references in a question to date_from/date_to using ENRON_EVENT_DATES."""
    q_lower = question.lower()

    between_match = re.search(r"between\s+(?:the\s+)?(.+?)\s+and\s+(?:the\s+)?(.+?)(?:\?|$|,)", q_lower)
    if between_match:
        event_a = between_match.group(1).strip()
        event_b = between_match.group(2).strip()
        date_a = _find_event_date(event_a)
        date_b = _find_event_date(event_b)
        if date_a and date_b:
            return {"date_from": date_a[0], "date_to": date_b[1]}

    after_match = re.search(r"after\s+(?:the\s+)?(.+?)(?:\?|$|,|\s+(?:how|what|who|when|did))", q_lower)
    if after_match:
        event = after_match.group(1).strip()
        dates = _find_event_date(event)
        if dates:
            return {"date_from": dates[1], "date_to": "2002-06-30"}

    before_match = re.search(r"before\s+(?:the\s+)?(.+?)(?:\?|$|,|\s+(?:how|what|who|when|did))", q_lower)
    if before_match:
        event = before_match.group(1).strip()
        dates = _find_event_date(event)
        if dates:
            return {"date_from": "1999-01-01", "date_to": dates[0]}

    since_match = re.search(r"since\s+(?:the\s+)?(.+?)(?:\?|$|,)", q_lower)
    if since_match:
        event = since_match.group(1).strip()
        dates = _find_event_date(event)
        if dates:
            return {"date_from": dates[0], "date_to": "2002-06-30"}

    for event_key, (d_from, d_to) in ENRON_EVENT_DATES.items():
        if event_key in q_lower:
            return {"date_from": d_from, "date_to": d_to}

    return {}


def _find_event_date(event_text: str) -> tuple[str, str] | None:
    """Look up an event phrase in ENRON_EVENT_DATES, using substring matching."""
    event_lower = event_text.lower().strip()
    for event_key, dates in ENRON_EVENT_DATES.items():
        if event_key in event_lower or event_lower in event_key:
            return dates
    for event_key, dates in ENRON_EVENT_DATES.items():
        key_words = set(event_key.split())
        text_words = set(event_lower.split())
        if len(key_words & text_words) >= max(1, len(key_words) - 1):
            return dates
    return None


def _heuristic_entity_names(question: str) -> list[str]:
    """Fast regex extraction of probable entity names from question text."""
    capitalized = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", question)
    return list(dict.fromkeys(capitalized))[:5]


_DATE_LIKE_RE = re.compile(
    r"^(?:\d{4}[-/]\d{2}[-/]\d{2}|"                  # 2001-08-14
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}|"
    r"(?:early|mid|late|spring|summer|fall|winter)\s+\d{4}|"
    r"\d{4})$",                                        # bare year
    re.IGNORECASE,
)


def pre_lookup_entities(entity_names: list[str]) -> tuple[list[str], list[str]]:
    """Look up extracted query entities against the graph.

    Returns (found, not_found) where each is a list of display strings.
    Batches all entity slug + alias patterns into a single SQL query.
    Filters out date-like strings that aren't entity names.
    """
    entity_names = [n for n in entity_names if not _DATE_LIKE_RE.match(n.strip())]

    slug_to_original: dict[str, str] = {}
    for name in entity_names:
        eid = _slugify(name)
        slug_to_original.setdefault(eid, name)
        for alias in _get_alias_names(name):
            slug_to_original.setdefault(_slugify(alias), name)

    if not slug_to_original:
        return [], list(entity_names)

    conditions = " OR ".join(
        f"entity_id LIKE :p{i}" for i in range(len(slug_to_original))
    )
    params = {f"p{i}": f"%{eid}%" for i, eid in enumerate(slug_to_original)}
    rows = _backend.execute_sql(
        f"SELECT entity_id, name, entity_type FROM {ENTITIES_TABLE}"
        f" WHERE {conditions}",
        params=params,
    )

    matched_originals: dict[str, list[str]] = {}
    for row in rows:
        rid = row["entity_id"]
        for slug, orig in slug_to_original.items():
            if slug in rid and orig not in matched_originals:
                matched_originals[orig] = []
            if slug in rid:
                display = f"{row['name']} ({row['entity_type']})"
                if display not in matched_originals.get(orig, []):
                    matched_originals.setdefault(orig, []).append(display)

    found = [f"{name} -> {', '.join(matches)}" for name, matches in matched_originals.items()]
    not_found = [name for name in entity_names if name not in matched_originals]
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
# KJV name alias resolution
# ---------------------------------------------------------------------------
KJV_ALIASES: dict[str, list[str]] = {
    "Elijah": ["Elias"],
    "Elisha": ["Eliseus"],
    "Isaiah": ["Esaias"],
    "Jeremiah": ["Jeremias", "Jeremy"],
    "Joshua": ["Jesus (Nave)", "Josue"],
    "Hosea": ["Osee"],
    "Noah": ["Noe"],
    "Jonah": ["Jonas"],
    "Hezekiah": ["Ezekias"],
    "Judas Iscariot": ["Judas"],
    "Paul": ["Saul"],
    "Saul": ["Paul"],
    "Abraham": ["Abram"],
    "Abram": ["Abraham"],
    "Sarah": ["Sarai", "Sara"],
    "Sarai": ["Sarah", "Sara"],
    "Jacob": ["Israel"],
    "Israel": ["Jacob"],
    "Rebekah": ["Rebecca"],
    "Timothy": ["Timotheus"],
    "Zephaniah": ["Sophonias"],
}

_KJV_REVERSE: dict[str, str] = {}
for _modern, _variants in KJV_ALIASES.items():
    for _v in _variants:
        _KJV_REVERSE.setdefault(_v.lower(), _modern)
        _KJV_REVERSE.setdefault(_modern.lower(), _v)


def _get_alias_names(name: str) -> list[str]:
    """Return alternative spellings/names for a given name."""
    aliases = KJV_ALIASES.get(name, [])
    lower = name.lower()
    for _v, _modern in _KJV_REVERSE.items():
        if _v == lower and _modern not in aliases:
            aliases.append(_modern)
    return aliases


# ---------------------------------------------------------------------------
# Enron entity-ID humanization
# ---------------------------------------------------------------------------
_ENRON_EMAIL_SUFFIXES = ("_ect_enron_com", "_enronxgate_com", "_enron_net", "_enron_com")


def _humanize_enron_id(eid: str) -> str:
    """Convert email-derived entity IDs like 'andrew_fastow_enron_com' to 'Andrew Fastow'."""
    for suffix in _ENRON_EMAIL_SUFFIXES:
        if eid.endswith(suffix):
            eid = eid[: -len(suffix)]
            break
    return eid.replace("_", " ").title()


def _maybe_humanize(name: str) -> str:
    """Humanize entity IDs to readable names.

    Handles three cases:
    1. Email-derived IDs with known domain suffixes (andrew_fastow_enron_com -> Andrew Fastow)
    2. Slug-style IDs with only lowercase/digits/underscores (karen_denne -> Karen Denne)
    3. Already human-readable names pass through unchanged
    """
    if any(name.endswith(s) for s in _ENRON_EMAIL_SUFFIXES):
        return _humanize_enron_id(name)
    if re.fullmatch(r"[a-z0-9][a-z0-9_]*[a-z0-9]", name) and "_" in name:
        return name.replace("_", " ").title()
    return name


# ---------------------------------------------------------------------------
# Structured output helpers
# ---------------------------------------------------------------------------
def _group_connections(entity_name: str, results: list[dict], corpus: str = "bible") -> dict:
    """Group raw connection rows by relationship_type for structured JSON output."""
    from collections import defaultdict
    humanize = _maybe_humanize if corpus == "enron" else lambda x: x
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        entry = {
            "source": humanize(r["source_name"]),
            "target": humanize(r["target_name"]),
            "description": r["description"],
        }
        freq = r.get("frequency")
        if freq is not None:
            try:
                entry["frequency"] = int(freq)
            except (ValueError, TypeError):
                pass
        ev_count = r.get("evidence_count")
        if ev_count is not None:
            try:
                entry["evidence_count"] = int(ev_count)
            except (ValueError, TypeError):
                pass
        for temporal_key in ("first_observed", "last_observed"):
            val = r.get(temporal_key)
            if val is not None:
                entry[temporal_key] = str(val)
        conf = r.get("confidence")
        if conf is not None:
            try:
                entry["confidence"] = round(float(conf), 3)
            except (ValueError, TypeError):
                pass
        if corpus != "enron" and "book" in r:
            entry["book"] = r["book"]
            entry["chapter"] = r.get("chapter")
        groups[r["relationship_type"]].append(entry)

    for rel_type in groups:
        groups[rel_type].sort(key=lambda e: e.get("frequency", 0), reverse=True)

    return {
        "entity": entity_name,
        "total": len(results),
        "by_type": dict(groups),
    }


def _group_entities_by_type(results: list[dict], book: str = "") -> dict:
    """Group entity list rows by entity_type for structured JSON output."""
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        groups[r["entity_type"]].append({
            "name": r["name"],
            "description": (r.get("description") or "")[:100],
        })
    out: dict = {"total": len(results), "by_type": dict(groups)}
    if book:
        out["book"] = book
    return out


# ---------------------------------------------------------------------------
# Graph traversal tools
# ---------------------------------------------------------------------------
@tool
def find_entity(name: str) -> str:
    """Search for an entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Kenneth Lay", "Enron")
    """
    if CORPUS == "enron":
        cols = "name, entity_type, description, first_mention_thread, first_mention_subject"
    else:
        cols = "name, entity_type, description, first_mention_book, first_mention_chapter"

    results = _backend.execute_sql(
        f"SELECT {cols} FROM {ENTITIES_TABLE}"
        " WHERE LOWER(name) LIKE LOWER(:name_pattern)"
        " ORDER BY name LIMIT 10",
        params={"name_pattern": f"%{name}%"},
    )

    if not results:
        for alias in _get_alias_names(name):
            results = _backend.execute_sql(
                f"SELECT {cols} FROM {ENTITIES_TABLE}"
                " WHERE LOWER(name) LIKE LOWER(:name_pattern)"
                " ORDER BY name LIMIT 10",
                params={"name_pattern": f"%{alias}%"},
            )
            if results:
                break

    if not results:
        alias_note = ""
        aliases = _get_alias_names(name)
        if aliases:
            alias_note = f" (also tried KJV variants: {', '.join(aliases)})"
        return f"No entity found matching '{name}'{alias_note}."

    entities = []
    for r in results:
        if CORPUS == "enron":
            mention = f"Thread: {r.get('first_mention_subject', 'N/A')}"
        else:
            mention = f"{r['first_mention_book']} ch.{r['first_mention_chapter']}"
        entry = {
            "name": r["name"],
            "type": r["entity_type"],
            "description": r["description"],
            "first_mention": mention,
        }
        if CORPUS == "enron" and r.get("entity_type", "").lower() == "person":
            try:
                addrs = [e for e in _resolve_name_to_email(r["name"]) if "@" in e]
                if addrs:
                    entry["email_addresses"] = addrs
            except Exception:
                pass
        entities.append(entry)
    return json.dumps(entities, ensure_ascii=False)


def _resolve_enron_entity_id(entity_name: str) -> list[str]:
    """Resolve an entity name to all possible entity_id patterns for Enron.

    Returns a list of LIKE patterns to try. Checks the entity_aliases table
    and also tries common name transformations. Falls back to stem matching
    when exact patterns yield no results.
    """
    primary_id = "_".join(entity_name.lower().split())
    patterns = [f"%{primary_id}%"]

    parts = entity_name.lower().split()
    if len(parts) == 2:
        last_name = parts[1]
        first_name = parts[0]
        patterns.append(f"%{last_name}_{first_name}%")
        patterns.append(f"%{first_name[0]}_{last_name}%")
        stem = last_name[:-2] if len(last_name) > 4 else last_name[:-1] if len(last_name) > 2 else last_name
        if stem != last_name:
            patterns.append(f"%{first_name}%{stem}%")
            patterns.append(f"%{stem}%{first_name}%")

    try:
        alias_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_aliases"
        alias_rows = _backend.execute_sql(
            f"SELECT canonical_id FROM {alias_table}"
            " WHERE LOWER(alias_id) LIKE :pattern"
            " LIMIT 5",
            params={"pattern": f"%{primary_id}%"},
        )
        for row in alias_rows:
            canonical = row.get("canonical_id", "")
            if canonical:
                patterns.append(f"%{canonical}%")
    except Exception:
        pass

    return list(dict.fromkeys(patterns))


@tool
def find_connections(entity_name: str, book: str = "", relationship_type: str = "") -> str:
    """Find relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Kenneth Lay")
        book: Optional — filter to a specific book (Bible) or leave empty.
        relationship_type: Optional — filter to a specific type (e.g., "REPORTS_TO", "MANAGES", "SENT_TO"). Leave empty for all types.
    """
    entity_id = "_".join(entity_name.lower().split())

    eid_pattern = f"%{entity_id}%"
    sql_params = {"eid_pattern": eid_pattern}

    if CORPUS == "enron":
        rel_filter = ""
        if relationship_type:
            rel_filter = " AND r.relationship_type = :rel_type"
            sql_params["rel_type"] = relationship_type.upper()

        all_patterns = _resolve_enron_entity_id(entity_name)
        results = []
        for i, pattern in enumerate(all_patterns):
            param_key = f"eid_pattern_{i}" if i > 0 else "eid_pattern"
            sql_params[param_key] = pattern
            batch = _backend.execute_sql(
                f"SELECT source_name, relationship_type, target_name,"
                f" MAX(description) as description,"
                f" SUM(COALESCE(edge_count, 1)) as frequency,"
                f" SUM(COALESCE(thread_cnt, 0)) as evidence_count"
                f" FROM ("
                f"   SELECT COALESCE(e1.name, r.source_entity) as source_name,"
                f"   r.relationship_type, COALESCE(e2.name, r.target_entity) as target_name,"
                f"   r.description, r.edge_count,"
                f"   COALESCE(SIZE(r.source_threads), 0) as thread_cnt"
                f"   FROM {RELATIONSHIPS_TABLE} r"
                f"   LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
                f"   LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
                f"   WHERE (r.source_entity LIKE :{param_key} OR r.target_entity LIKE :{param_key}){rel_filter}"
                f" ) sub"
                f" GROUP BY source_name, relationship_type, target_name"
                f" ORDER BY frequency DESC"
                " LIMIT 50",
                params=sql_params,
            )
            if batch:
                results.extend(batch)
                break

        if not results:
            suffix = f" of type {relationship_type}" if relationship_type else ""
            return f"No connections found for '{entity_name}'{suffix}."

        grouped = _group_connections(entity_name, results, corpus="enron")

        if not relationship_type:
            for rel_type in grouped.get("by_type", {}):
                grouped["by_type"][rel_type] = grouped["by_type"][rel_type][:10]
            grouped["note"] = "Results capped at 10 per type. Use relationship_type param to get full results for a specific type."

        return json.dumps(grouped, ensure_ascii=False)

    book_filter = ""
    if book:
        book_filter = " AND r.book = :book"
        sql_params["book"] = book

    rel_filter = ""
    if relationship_type:
        rel_filter = " AND r.relationship_type = :rel_type"
        sql_params["rel_type"] = relationship_type.upper()

    results = _backend.execute_sql(
        f"SELECT COALESCE(e1.name, r.source_entity) as source_name,"
        f" r.relationship_type, COALESCE(e2.name, r.target_entity) as target_name,"
        f" r.description, r.book, r.chapter"
        f" FROM {RELATIONSHIPS_TABLE} r"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        f" WHERE (r.source_entity LIKE :eid_pattern OR r.target_entity LIKE :eid_pattern){book_filter}{rel_filter}"
        " ORDER BY r.book, r.chapter LIMIT 100",
        params=sql_params,
    )

    if not results:
        suffix = f" in {book}" if book else ""
        return f"No connections found for '{entity_name}'{suffix}."

    return json.dumps(
        _group_connections(entity_name, results, corpus="bible"),
        ensure_ascii=False,
    )


def _resolve_name_to_email(person_name: str) -> list[str]:
    """Resolve a person name to email address patterns for querying communication tables.

    Tries multiple strategies:
    1. Direct name->email pattern (e.g., "Jeff Dasovich" -> "%jeff.dasovich%")
    2. Participants table lookup by display name (exact, then fuzzy stem)
    3. Entity aliases canonical_id -> email derivation

    Returns LIKE patterns for matching against email addresses in communication_dyads.
    """
    name_lower = person_name.lower().strip()
    parts = name_lower.split()
    patterns: list[str] = []

    if "@" in name_lower:
        patterns.append(name_lower)
        return patterns

    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        patterns.append(f"%{first}.{last}%")
        patterns.append(f"%{last}.{first}%")
        patterns.append(f"%{first[0]}.{last}%")
        if len(parts) == 3:
            middle = parts[1]
            patterns.append(f"%{first}.{middle[0]}.{last}%")
    else:
        patterns.append(f"%{name_lower}%")

    found_from_participants = False
    try:
        rows = _backend.execute_sql(
            f"SELECT email_address FROM {ENRON_PARTICIPANTS_TABLE}"
            " WHERE LOWER(name_normalized) LIKE :name_pat"
            " OR LOWER(display_name) LIKE :name_pat"
            " LIMIT 5",
            params={"name_pat": f"%{name_lower}%"},
        )
        for row in rows:
            addr = row.get("email_address", "")
            if addr and addr not in patterns:
                patterns.append(addr)
                found_from_participants = True
    except Exception:
        pass

    if not found_from_participants and len(parts) >= 2:
        first, last = parts[0], parts[-1]
        stem = last[:-2] if len(last) > 4 else last[:-1] if len(last) > 2 else last
        try:
            rows = _backend.execute_sql(
                f"SELECT email_address FROM {ENRON_PARTICIPANTS_TABLE}"
                " WHERE (LOWER(name_normalized) LIKE :stem_pat"
                " OR LOWER(display_name) LIKE :stem_pat)"
                " AND (LOWER(name_normalized) LIKE :first_pat"
                " OR LOWER(display_name) LIKE :first_pat)"
                " LIMIT 5",
                params={"stem_pat": f"%{stem}%", "first_pat": f"%{first}%"},
            )
            for row in rows:
                addr = row.get("email_address", "")
                if addr and addr not in patterns:
                    patterns.append(addr)
        except Exception:
            pass

    return list(dict.fromkeys(patterns))


def _email_local_part(addr: str) -> str:
    """Extract and normalize the local part of an email address."""
    local = addr.split("@")[0] if "@" in addr else addr
    return local.replace(".", "").replace("_", "").replace("-", "").lower()


def _is_likely_same_person(email_a: str, email_b: str) -> bool:
    """Heuristic check whether two email addresses likely belong to the same person.

    Handles cross-domain aliases including first-initial+lastname patterns
    (e.g., vince.kaminski@enron.com vs vkaminski@aol.com).
    """
    if email_a == email_b:
        return True
    domain_a = email_a.split("@")[1] if "@" in email_a else ""
    domain_b = email_b.split("@")[1] if "@" in email_b else ""
    if domain_a == domain_b:
        return False
    local_a = _email_local_part(email_a)
    local_b = _email_local_part(email_b)
    if local_a == local_b:
        return True
    shorter, longer = sorted([local_a, local_b], key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    if len(shorter) >= 5 and longer.startswith(shorter[:5]):
        return True

    raw_a = email_a.split("@")[0] if "@" in email_a else email_a
    raw_b = email_b.split("@")[0] if "@" in email_b else email_b

    def _extract_lastname(raw: str) -> str:
        parts = re.split(r"[._\-]", raw.lower())
        return parts[-1] if len(parts) >= 2 and len(parts[-1]) >= 3 else ""

    def _is_initial_plus_name(raw: str, lastname: str) -> bool:
        """Check if raw is first-initial + lastname (e.g., 'vkaminski')."""
        norm = raw.lower().replace(".", "").replace("_", "").replace("-", "")
        return len(norm) >= 4 and norm[0].isalpha() and norm[1:].startswith(lastname)

    last_a = _extract_lastname(raw_a)
    last_b = _extract_lastname(raw_b)

    if last_a and last_b and last_a == last_b:
        return True
    if last_a and _is_initial_plus_name(raw_b, last_a):
        return True
    if last_b and _is_initial_plus_name(raw_a, last_b):
        return True

    return False


def _parse_search_terms(entity_name: str) -> list[str]:
    """Split 'A AND B' into multiple search terms; returns single-element list otherwise."""
    if " AND " in entity_name:
        return [t.strip() for t in entity_name.split(" AND ") if t.strip()]
    return [entity_name]


def _dedup_contacts(contacts: list[dict]) -> list[dict]:
    """Merge contacts that resolve to the same canonical entity.

    Uses two strategies:
    1. Entity alias table lookup (knowledge graph entity_id dedup)
    2. Email-based dedup: contacts whose email addresses resolve to the same
       person in the participants table are merged.

    Sums sent/received/total and keeps the most human-readable name.
    """
    if not contacts:
        return contacts

    slug_map: dict[str, str] = {}
    raw_slugs = []
    for c in contacts:
        slug = "_".join(c["name"].lower().split())
        raw_slugs.append(slug)

    try:
        alias_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_aliases"
        all_slugs = list(set(raw_slugs))
        conditions = " OR ".join(
            f"LOWER(alias_id) = :s{i}" for i in range(len(all_slugs))
        )
        params = {f"s{i}": s for i, s in enumerate(all_slugs)}
        rows = _backend.execute_sql(
            f"SELECT LOWER(alias_id) AS alias_id, canonical_id"
            f" FROM {alias_table}"
            f" WHERE {conditions}",
            params=params,
        )
        for row in rows:
            slug_map[row["alias_id"]] = row["canonical_id"]
    except Exception:
        pass

    email_canonical: dict[str, str] = {}
    emails_to_check = [c.get("email", "") for c in contacts if c.get("email")]
    if emails_to_check:
        try:
            all_emails = list(set(emails_to_check))
            conditions = " OR ".join(
                f"email_address = :e{i}" for i in range(len(all_emails))
            )
            params = {f"e{i}": e for i, e in enumerate(all_emails)}
            name_rows = _backend.execute_sql(
                f"SELECT email_address,"
                f" COALESCE(name_normalized, display_name) AS display"
                f" FROM {ENRON_PARTICIPANTS_TABLE}"
                f" WHERE {conditions}",
                params=params,
            )
            display_to_emails: dict[str, list[str]] = {}
            for nr in name_rows:
                display = (nr.get("display") or "").strip().lower()
                addr = nr["email_address"]
                if display and len(display) > 3:
                    display_to_emails.setdefault(display, []).append(addr)
            for display, addrs in display_to_emails.items():
                if len(addrs) > 1:
                    canonical_email = addrs[0]
                    for addr in addrs[1:]:
                        email_canonical[addr] = canonical_email
        except Exception:
            pass

    grouped: dict[str, dict] = {}
    for c, slug in zip(contacts, raw_slugs):
        canonical = slug_map.get(slug, slug)
        c_email = c.get("email", "")
        if c_email in email_canonical:
            canonical = email_canonical[c_email]

        if canonical in grouped:
            grouped[canonical]["sent"] += c["sent"]
            grouped[canonical]["received"] += c["received"]
            grouped[canonical]["total"] += c["total"]
            if len(c["name"]) > len(grouped[canonical]["name"]) and not c["name"].endswith(".com"):
                grouped[canonical]["name"] = c["name"]
        else:
            grouped[canonical] = dict(c)

    result = sorted(grouped.values(), key=lambda x: x["total"], reverse=True)
    return result


@tool
def find_top_contacts(entity_name: str, direction: str = "both", limit: int = 10) -> str:
    """Find the people who communicated most frequently with an entity, ranked by total email count.
    Queries pre-aggregated communication_dyads built from actual email sender/recipient headers.
    Use for "who emailed X the most?", "who did X communicate with?", or "how many emails between X and Y?".

    Args:
        entity_name: The person to find top contacts for (e.g., "Kenneth Lay")
        direction: Filter direction — "both" (default), "inbound" (emails TO entity), or "outbound" (emails FROM entity).
        limit: Max number of contacts to return (default 10).
    """
    if CORPUS != "enron":
        return "find_top_contacts is only available for the Enron corpus. Use find_connections instead."

    email_patterns = _resolve_name_to_email(entity_name)
    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    results = None
    for ep in email_patterns:
        if direction == "outbound":
            sql = (
                f"SELECT d.person_b AS contact_email,"
                f" SUM(d.total_count) AS sent,"
                f" 0 AS received,"
                f" SUM(d.total_count) AS total"
                f" FROM {dyads_table} d"
                f" WHERE LOWER(d.person_a) LIKE :email_pat"
                f" GROUP BY d.person_b ORDER BY total DESC LIMIT {int(limit)}"
            )
        elif direction == "inbound":
            sql = (
                f"SELECT d.person_a AS contact_email,"
                f" 0 AS sent,"
                f" SUM(d.total_count) AS received,"
                f" SUM(d.total_count) AS total"
                f" FROM {dyads_table} d"
                f" WHERE LOWER(d.person_b) LIKE :email_pat"
                f" GROUP BY d.person_a ORDER BY total DESC LIMIT {int(limit)}"
            )
        else:
            sql = (
                f"SELECT contact_email,"
                f" SUM(CASE WHEN dir = 'out' THEN cnt ELSE 0 END) AS sent,"
                f" SUM(CASE WHEN dir = 'in' THEN cnt ELSE 0 END) AS received,"
                f" SUM(cnt) AS total"
                f" FROM ("
                f"   SELECT d.person_b AS contact_email, 'out' AS dir,"
                f"   SUM(d.total_count) AS cnt"
                f"   FROM {dyads_table} d"
                f"   WHERE LOWER(d.person_a) LIKE :email_pat"
                f"   GROUP BY d.person_b"
                f"   UNION ALL"
                f"   SELECT d.person_a AS contact_email, 'in' AS dir,"
                f"   SUM(d.total_count) AS cnt"
                f"   FROM {dyads_table} d"
                f"   WHERE LOWER(d.person_b) LIKE :email_pat"
                f"   GROUP BY d.person_a"
                f" ) combined"
                f" GROUP BY contact_email ORDER BY total DESC LIMIT {int(limit)}"
            )

        results = _backend.execute_sql(sql, params={"email_pat": ep})
        if results:
            break

    if not results:
        return f"No email contacts found for '{entity_name}'."

    contact_emails = [r["contact_email"] for r in results if r.get("contact_email")]
    display_map: dict[str, str] = {}
    if contact_emails:
        try:
            chunks = [contact_emails[i:i + 20] for i in range(0, len(contact_emails), 20)]
            for chunk in chunks:
                conditions = " OR ".join(f"email_address = :e{i}" for i in range(len(chunk)))
                params = {f"e{i}": e for i, e in enumerate(chunk)}
                name_rows = _backend.execute_sql(
                    f"SELECT email_address,"
                    f" COALESCE(name_normalized, display_name, email_address) AS display"
                    f" FROM {participants_table}"
                    f" WHERE {conditions}",
                    params=params,
                )
                for nr in name_rows:
                    display_map[nr["email_address"]] = nr["display"]
        except Exception:
            pass

    def _display_name(email_addr: str) -> str:
        raw = display_map.get(email_addr, "")
        if raw and "@" not in raw and "<" not in raw:
            return raw
        local = email_addr.split("@")[0] if "@" in email_addr else email_addr
        return local.replace(".", " ").replace("_", " ").title()

    contacts = []
    for r in results:
        addr = r["contact_email"]
        contacts.append({
            "name": _display_name(addr),
            "email": addr,
            "sent": int(r.get("sent") or 0),
            "received": int(r.get("received") or 0),
            "total": int(r.get("total") or 0),
        })
    contacts = _dedup_contacts(contacts)
    return json.dumps({
        "entity": entity_name,
        "direction": direction,
        "source": "communication_dyads",
        "top_contacts": contacts,
    }, ensure_ascii=False)


@tool
def get_top_email_pairs(limit: int = 20) -> str:
    """Find the pairs of people who exchanged the most emails with each other across the entire corpus.
    Queries pre-aggregated communication_dyads for the highest-volume bidirectional pairs.
    Use for "which two people emailed each other the most?", "top communication pairs", or global corpus ranking questions.

    Args:
        limit: Max number of pairs to return (default 20).
    """
    if CORPUS != "enron":
        return "get_top_email_pairs is only available for the Enron corpus."

    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    sql = (
        f"SELECT"
        f" CASE WHEN person_a < person_b THEN person_a ELSE person_b END AS email_a,"
        f" CASE WHEN person_a < person_b THEN person_b ELSE person_a END AS email_b,"
        f" SUM(total_count) AS total"
        f" FROM {dyads_table}"
        f" GROUP BY 1, 2"
        f" ORDER BY total DESC"
        f" LIMIT {int(limit)}"
    )
    results = _backend.execute_sql(sql)
    if not results:
        return "No communication pairs found in the corpus."

    all_emails = set()
    for r in results:
        all_emails.add(r["email_a"])
        all_emails.add(r["email_b"])

    display_map: dict[str, str] = {}
    if all_emails:
        try:
            email_list = list(all_emails)
            chunks = [email_list[i:i + 20] for i in range(0, len(email_list), 20)]
            for chunk in chunks:
                conditions = " OR ".join(f"email_address = :e{i}" for i in range(len(chunk)))
                params = {f"e{i}": e for i, e in enumerate(chunk)}
                name_rows = _backend.execute_sql(
                    f"SELECT email_address,"
                    f" COALESCE(name_normalized, display_name, email_address) AS display"
                    f" FROM {participants_table}"
                    f" WHERE {conditions}",
                    params=params,
                )
                for nr in name_rows:
                    display_map[nr["email_address"]] = nr["display"]
        except Exception:
            pass

    def _display(addr: str) -> str:
        raw = display_map.get(addr, "")
        if raw and "@" not in raw and "<" not in raw:
            return raw
        local = addr.split("@")[0] if "@" in addr else addr
        return local.replace(".", " ").replace("_", " ").title()

    pairs = []
    self_email_count = 0
    for r in results:
        is_self = _is_likely_same_person(r["email_a"], r["email_b"])
        if is_self:
            self_email_count += 1
        pairs.append({
            "person_a": _display(r["email_a"]),
            "person_a_email": r["email_a"],
            "person_b": _display(r["email_b"]),
            "person_b_email": r["email_b"],
            "total_emails": int(r["total"]),
            "is_self_email": is_self,
        })

    return json.dumps({
        "source": "communication_dyads",
        "self_email_pairs_found": self_email_count,
        "top_pairs": pairs,
    }, ensure_ascii=False)


@tool
def get_top_individuals(limit: int = 20, sort_by: str = "total") -> str:
    """Find the individuals who sent or received the most emails across the entire corpus.
    Queries pre-aggregated person_activity for corpus-wide individual volume ranking.
    Use for "who sent the most emails?", "top senders", "most active people", or individual ranking.

    Args:
        limit: Max number of individuals to return (default 20).
        sort_by: Column to rank by — "total" (sent+received), "sent", or "received" (default "total").
    """
    if CORPUS != "enron":
        return "get_top_individuals is only available for the Enron corpus."

    activity_table = ENRON_PERSON_ACTIVITY_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    order_col = {
        "sent": "total_sent",
        "received": "total_received",
    }.get(sort_by, "total")

    sql = (
        f"SELECT person_id,"
        f" SUM(emails_sent) AS total_sent,"
        f" SUM(emails_received) AS total_received,"
        f" SUM(emails_sent) + SUM(emails_received) AS total"
        f" FROM {activity_table}"
        f" GROUP BY person_id"
        f" ORDER BY {order_col} DESC"
        f" LIMIT {int(limit)}"
    )
    results = _backend.execute_sql(sql)
    if not results:
        return "No individual activity data found in the corpus."

    all_emails = {r["person_id"] for r in results}
    display_map: dict[str, str] = {}
    if all_emails:
        try:
            email_list = list(all_emails)
            chunks = [email_list[i:i + 20] for i in range(0, len(email_list), 20)]
            for chunk in chunks:
                conditions = " OR ".join(f"email_address = :e{i}" for i in range(len(chunk)))
                params = {f"e{i}": e for i, e in enumerate(chunk)}
                name_rows = _backend.execute_sql(
                    f"SELECT email_address,"
                    f" COALESCE(name_normalized, display_name, email_address) AS display"
                    f" FROM {participants_table}"
                    f" WHERE {conditions}",
                    params=params,
                )
                for nr in name_rows:
                    display_map[nr["email_address"]] = nr["display"]
        except Exception:
            pass

    def _display(addr: str) -> str:
        raw = display_map.get(addr, "")
        if raw and "@" not in raw and "<" not in raw:
            return raw
        local = addr.split("@")[0] if "@" in addr else addr
        return local.replace(".", " ").replace("_", " ").title()

    individuals = []
    for r in results:
        individuals.append({
            "name": _display(r["person_id"]),
            "email": r["person_id"],
            "emails_sent": int(r["total_sent"]),
            "emails_received": int(r["total_received"]),
            "total": int(r["total"]),
        })

    return json.dumps({
        "source": "person_activity",
        "sort_by": sort_by,
        "individuals": individuals,
    }, ensure_ascii=False)


@tool
def get_emails_between(entity_a: str, entity_b: str, limit: int = 15) -> str:
    """Retrieve emails between two people. First searches sender/recipient headers;
    if no direct emails exist, falls back to emails that mention both people in the body.
    Use this to find what topics two people discussed, or to ground claims with email evidence.

    Args:
        entity_a: First person (e.g., "Karen Denne" or "karen.denne@enron.com")
        entity_b: Second person (e.g., "Kenneth Lay" or "kenneth.lay@enron.com")
        limit: Max emails to return (default 15).
    """
    if CORPUS != "enron":
        return "get_emails_between is only available for the Enron corpus."

    cfg = _get_corpus_config()
    src_table = cfg["source_table"]

    a_patterns_hdr = _resolve_name_to_email(entity_a)
    b_patterns_hdr = _resolve_name_to_email(entity_b)

    results = []
    match_type = "header"
    for a_pat in a_patterns_hdr:
        for b_pat in b_patterns_hdr:
            results = _backend.execute_sql(
                f"SELECT sender, subject, date,"
                f" SUBSTRING(body, 1, 500) AS body_preview"
                f" FROM {src_table}"
                f" WHERE (LOWER(sender) LIKE :a_pat"
                f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :b_pat"
                f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :b_pat))"
                f"    OR (LOWER(sender) LIKE :b_pat"
                f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :a_pat"
                f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :a_pat))"
                f" ORDER BY date DESC LIMIT {int(limit)}",
                params={"a_pat": a_pat, "b_pat": b_pat},
            )
            if results:
                break
        if results:
            break

    if not results:
        mentions_table = cfg["entity_mentions"]
        a_patterns = _resolve_enron_entity_id(entity_a)
        b_patterns = _resolve_enron_entity_id(entity_b)

        for a_pat in a_patterns:
            for b_pat in b_patterns:
                results = _backend.execute_sql(
                    f"SELECT e.sender, e.subject, e.date,"
                    f" SUBSTRING(e.body, 1, 500) AS body_preview"
                    f" FROM {src_table} e"
                    f" INNER JOIN {mentions_table} ma ON e.message_id = ma.message_id"
                    f" INNER JOIN {mentions_table} mb ON e.message_id = mb.message_id"
                    f" WHERE ma.entity_id LIKE :a_id AND mb.entity_id LIKE :b_id"
                    f"   AND ma.entity_id != mb.entity_id"
                    f" ORDER BY e.date DESC LIMIT {int(limit)}",
                    params={"a_id": a_pat, "b_id": b_pat},
                )
                if results:
                    break
            if results:
                break

        match_type = "body_mention"

    if not results:
        rel_table = cfg["relationships"]
        a_eid_patterns = _resolve_enron_entity_id(entity_a)
        b_eid_patterns = _resolve_enron_entity_id(entity_b)

        all_threads: list[str] = []
        for si, sp in enumerate(a_eid_patterns):
            for ti, tp in enumerate(b_eid_patterns):
                for rels_batch in [
                    _backend.execute_sql(
                        f"SELECT source_threads FROM {rel_table}"
                        f" WHERE source_entity LIKE :sp AND target_entity LIKE :tp"
                        f" LIMIT 20",
                        params={"sp": sp, "tp": tp},
                    ),
                    _backend.execute_sql(
                        f"SELECT source_threads FROM {rel_table}"
                        f" WHERE source_entity LIKE :tp AND target_entity LIKE :sp"
                        f" LIMIT 20",
                        params={"sp": sp, "tp": tp},
                    ),
                ]:
                    for row in (rels_batch or []):
                        threads = row.get("source_threads") or []
                        if isinstance(threads, str):
                            threads = [t.strip() for t in threads.strip("[]").split(",") if t.strip()]
                        all_threads.extend(threads)
                if all_threads:
                    break
            if all_threads:
                break

        all_threads = list(dict.fromkeys(all_threads))[:30]
        if all_threads:
            thread_params = {f"t{i}": tid for i, tid in enumerate(all_threads)}
            placeholders = ", ".join(f":t{i}" for i in range(len(all_threads)))
            results = _backend.execute_sql(
                f"SELECT sender, subject, date,"
                f" SUBSTRING(body, 1, 500) AS body_preview"
                f" FROM {src_table}"
                f" WHERE thread_id IN ({placeholders})"
                f" ORDER BY date DESC LIMIT {int(limit)}",
                params=thread_params,
            )
            match_type = "relationship_threads"

    if not results:
        return f"No emails found between '{entity_a}' and '{entity_b}'."

    total_count = len(results)
    if match_type == "header" and total_count >= int(limit):
        try:
            a_email_pats = _resolve_name_to_email(entity_a)
            b_email_pats = _resolve_name_to_email(entity_b)
            dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
            for ap in a_email_pats:
                for bp in b_email_pats:
                    count_rows = _backend.execute_sql(
                        f"SELECT SUM(d.total_count) AS cnt"
                        f" FROM {dyads_table} d"
                        f" WHERE (LOWER(d.person_a) LIKE :a AND LOWER(d.person_b) LIKE :b)"
                        f"    OR (LOWER(d.person_a) LIKE :b AND LOWER(d.person_b) LIKE :a)",
                        params={"a": ap, "b": bp},
                    )
                    if count_rows and count_rows[0].get("cnt"):
                        total_count = int(count_rows[0]["cnt"])
                        break
                if total_count > len(results):
                    break
        except Exception:
            pass

    emails = []
    for r in results:
        emails.append({
            "date": str(r.get("date", ""))[:10],
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": (r.get("body_preview", "") or "")[:300],
        })
    return json.dumps({
        "between": [entity_a, entity_b],
        "total_emails": total_count,
        "showing": len(emails),
        "match_type": match_type,
        "emails": emails,
    }, ensure_ascii=False)


@tool
def get_dyad_topics(entity_a: str, entity_b: str, limit: int = 20) -> str:
    """Get the discussion topics between two people using AI-generated thread summaries.
    Joins emails exchanged between the pair to thread-level key_topics and summaries.
    Returns a frequency-ranked list of topics and sample thread summaries.

    Args:
        entity_a: First person (e.g., "Jeff Dasovich" or "jeff.dasovich@enron.com")
        entity_b: Second person (e.g., "Susan Mara" or "susan.mara@enron.com")
        limit: Max threads to scan (default 20).
    """
    if CORPUS != "enron":
        return "get_dyad_topics is only available for the Enron corpus."

    cfg = _get_corpus_config()
    src_table = cfg["source_table"]
    threads_table = ENRON_THREADS_TABLE

    a_patterns = _resolve_name_to_email(entity_a)
    b_patterns = _resolve_name_to_email(entity_b)

    thread_rows = []
    for a_pat in a_patterns:
        for b_pat in b_patterns:
            thread_rows = _backend.execute_sql(
                f"SELECT DISTINCT e.thread_id"
                f" FROM {src_table} e"
                f" WHERE (LOWER(sender) LIKE :a_pat"
                f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :b_pat"
                f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :b_pat))"
                f"    OR (LOWER(sender) LIKE :b_pat"
                f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :a_pat"
                f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :a_pat))"
                f" LIMIT {int(limit) * 2}",
                params={"a_pat": a_pat, "b_pat": b_pat},
            )
            if thread_rows:
                break
        if thread_rows:
            break

    if not thread_rows:
        return json.dumps({
            "between": [entity_a, entity_b],
            "top_topics": [],
            "threads": [],
            "note": "No emails found between these people.",
        }, ensure_ascii=False)

    thread_ids = [r["thread_id"] for r in thread_rows if r.get("thread_id")]
    if not thread_ids:
        return json.dumps({
            "between": [entity_a, entity_b],
            "top_topics": [],
            "threads": [],
            "note": "No thread IDs found for emails between these people.",
        }, ensure_ascii=False)

    thread_params = {f"t{i}": tid for i, tid in enumerate(thread_ids)}
    placeholders = ", ".join(f":t{i}" for i in range(len(thread_ids)))
    topic_rows = _backend.execute_sql(
        f"SELECT thread_id, subject, summary, key_topics"
        f" FROM {threads_table}"
        f" WHERE thread_id IN ({placeholders})"
        f"   AND key_topics IS NOT NULL"
        f" LIMIT {int(limit)}",
        params=thread_params,
    )

    if not topic_rows:
        return json.dumps({
            "between": [entity_a, entity_b],
            "top_topics": [],
            "threads": [],
            "note": "Threads exist but have no AI-generated topic tags yet.",
        }, ensure_ascii=False)

    from collections import Counter
    topic_counts: Counter = Counter()
    threads_out = []
    for row in topic_rows:
        topics = row.get("key_topics") or []
        if isinstance(topics, str):
            try:
                topics = json.loads(topics)
            except (json.JSONDecodeError, ValueError):
                topics = [t.strip() for t in topics.strip("[]").split(",") if t.strip()]
        for tag in topics:
            topic_counts[tag.strip().lower()] += 1
        threads_out.append({
            "thread_id": row.get("thread_id", ""),
            "subject": row.get("subject", ""),
            "summary": (row.get("summary", "") or "")[:300],
            "key_topics": topics,
        })

    ranked_topics = [
        {"topic": tag, "count": cnt}
        for tag, cnt in topic_counts.most_common(30)
    ]

    return json.dumps({
        "between": [entity_a, entity_b],
        "threads_scanned": len(topic_rows),
        "top_topics": ranked_topics,
        "threads": threads_out[:10],
    }, ensure_ascii=False)


@tool
def get_relationship_evidence(
    source_entity: str, target_entity: str,
    relationship_type: str = "", limit: int = 5,
) -> str:
    """Retrieve the original emails where a graph relationship was extracted from.
    Use this to validate or ground a relationship claim with source email evidence.

    Args:
        source_entity: Source entity name (e.g., "Sanjay Bhatnagar")
        target_entity: Target entity name (e.g., "Jeff Skilling")
        relationship_type: Optional type filter (e.g., "REPORTS_TO", "MANAGES")
        limit: Max emails to return (default 5).
    """
    if CORPUS != "enron":
        return "get_relationship_evidence is only available for the Enron corpus."

    cfg = _get_corpus_config()
    rel_table = cfg["relationships"]
    src_table = cfg["source_table"]

    src_patterns = _resolve_enron_entity_id(source_entity)
    tgt_patterns = _resolve_enron_entity_id(target_entity)

    rel_filter = ""
    sql_params: dict[str, str] = {}
    if relationship_type:
        rel_filter = " AND r.relationship_type = :rel_type"
        sql_params["rel_type"] = relationship_type.upper()

    rels: list[dict] = []
    for si, sp in enumerate(src_patterns):
        for ti, tp in enumerate(tgt_patterns):
            sk = f"src_{si}_{ti}"
            tk = f"tgt_{si}_{ti}"
            sql_params[sk] = sp
            sql_params[tk] = tp
            batch = _backend.execute_sql(
                f"SELECT source_threads, description, relationship_type,"
                f" COALESCE(e1.name, r.source_entity) AS source_name,"
                f" COALESCE(e2.name, r.target_entity) AS target_name"
                f" FROM {rel_table} r"
                f" LEFT JOIN {cfg['entities']} e1 ON r.source_entity = e1.entity_id"
                f" LEFT JOIN {cfg['entities']} e2 ON r.target_entity = e2.entity_id"
                f" WHERE (r.source_entity LIKE :{sk} AND r.target_entity LIKE :{tk})"
                f" {rel_filter} LIMIT 10",
                params=sql_params,
            )
            if batch:
                rels.extend(batch)
                break
        if rels:
            break

    if not rels:
        for si, sp in enumerate(src_patterns):
            for ti, tp in enumerate(tgt_patterns):
                sk = f"rev_src_{si}_{ti}"
                tk = f"rev_tgt_{si}_{ti}"
                sql_params[sk] = tp
                sql_params[tk] = sp
                batch = _backend.execute_sql(
                    f"SELECT source_threads, description, relationship_type,"
                    f" COALESCE(e1.name, r.source_entity) AS source_name,"
                    f" COALESCE(e2.name, r.target_entity) AS target_name"
                    f" FROM {rel_table} r"
                    f" LEFT JOIN {cfg['entities']} e1 ON r.source_entity = e1.entity_id"
                    f" LEFT JOIN {cfg['entities']} e2 ON r.target_entity = e2.entity_id"
                    f" WHERE (r.source_entity LIKE :{sk} AND r.target_entity LIKE :{tk})"
                    f" {rel_filter} LIMIT 10",
                    params=sql_params,
                )
                if batch:
                    rels.extend(batch)
                    break
            if rels:
                break

    if not rels:
        return json.dumps({
            "source": source_entity,
            "target": target_entity,
            "error": f"No relationship found between '{source_entity}' and '{target_entity}'."
                + (" Try get_source_evidence with 'A AND B' syntax to find emails mentioning both." if not relationship_type else ""),
        })

    all_threads: list[str] = []
    rel_descriptions: list[dict] = []
    humanize = _maybe_humanize
    for r in rels:
        threads = r.get("source_threads") or []
        if isinstance(threads, str):
            threads = [t.strip() for t in threads.strip("[]").split(",") if t.strip()]
        all_threads.extend(threads)
        rel_descriptions.append({
            "type": r.get("relationship_type", ""),
            "source": humanize(r.get("source_name", "")),
            "target": humanize(r.get("target_name", "")),
            "description": r.get("description", ""),
        })

    all_threads = list(dict.fromkeys(all_threads))[:20]

    if not all_threads:
        return json.dumps({
            "source": source_entity,
            "target": target_entity,
            "relationships": rel_descriptions,
            "evidence": "No source thread IDs recorded for this relationship. "
                "Try get_source_evidence with 'A AND B' syntax to find emails mentioning both.",
        }, ensure_ascii=False)

    thread_params = {f"t{i}": tid for i, tid in enumerate(all_threads)}
    placeholders = ", ".join(f":t{i}" for i in range(len(all_threads)))

    emails_rows = _backend.execute_sql(
        f"SELECT sender, subject, date, thread_id,"
        f" SUBSTRING(body, 1, 800) AS body_preview"
        f" FROM {src_table}"
        f" WHERE thread_id IN ({placeholders})"
        f" ORDER BY date LIMIT {int(limit)}",
        params=thread_params,
    )

    evidence_emails = []
    for e in emails_rows:
        evidence_emails.append({
            "date": str(e.get("date", ""))[:10],
            "sender": e.get("sender", ""),
            "subject": e.get("subject", ""),
            "thread_id": e.get("thread_id", ""),
            "body_preview": (e.get("body_preview", "") or "")[:500],
        })

    return json.dumps({
        "source": source_entity,
        "target": target_entity,
        "relationships": rel_descriptions,
        "evidence_emails": evidence_emails,
        "thread_count": len(all_threads),
    }, ensure_ascii=False)


@tool
def get_source_evidence(entity_name: str, book: str = "") -> str:
    """Get source text that mentions a specific entity. For Bible: returns verses. For Enron: returns emails.

    Args:
        entity_name: The entity name to find source text for. Supports 'A AND B' syntax
            to find emails mentioning both entities (e.g., "Kathy Dodgen AND Kenneth Lay").
        book: For Bible: filter to a specific book (e.g., "Genesis").
            For Enron: filter to a second entity name — returns emails mentioning BOTH
            entity_name and book (e.g., entity_name="Kathy Dodgen", book="Kenneth Lay").
    """
    if CORPUS == "enron":
        terms = _parse_search_terms(entity_name)
        if book:
            terms.append(book)

        sql_params = {}
        conditions = []
        for i, term in enumerate(terms):
            param_name = f"p{i}"
            sql_params[param_name] = f"%{term}%"
            conditions.append(f"body LIKE :{param_name}")

        where_clause = " AND ".join(conditions)
        src_table = _get_corpus_config()["source_table"]

        results = _backend.execute_sql(
            f"SELECT message_id, sender, date, subject,"
            f" ARRAY_JOIN(SLICE(to_recipients, 1, 3), ', ') AS to_list,"
            f" SUBSTRING(body, 1, 500) AS body_preview,"
            f" COALESCE(SIZE(to_recipients), 0) + COALESCE(SIZE(cc_recipients), 0) AS recipient_count"
            f" FROM {src_table}"
            f" WHERE {where_clause}"
            " ORDER BY date DESC LIMIT 20",
            params=sql_params,
        )
        if not results:
            search_desc = " AND ".join(terms)
            return f"No emails found mentioning '{search_desc}'."
        emails = []
        for r in results:
            entry = {
                "id": r.get("message_id", ""),
                "date": str(r.get("date", ""))[:10],
                "from": r.get("sender", ""),
                "to": r.get("to_list", ""),
                "subject": r.get("subject", ""),
                "snippet": (r.get("body_preview", "") or "")[:200],
            }
            rc = r.get("recipient_count")
            if rc is not None:
                try:
                    rc_int = int(rc)
                    entry["recipient_count"] = rc_int
                    entry["email_type"] = "direct" if rc_int <= 5 else "group" if rc_int <= 20 else "mass"
                except (ValueError, TypeError):
                    pass
            emails.append(entry)
        search_desc = " AND ".join(terms)
        return json.dumps({"entity": search_desc, "total": len(emails), "emails": emails}, ensure_ascii=False)

    sql_params = {"name_pattern": f"%{entity_name}%"}

    book_filter = ""
    if book:
        book_filter = " AND v.book = :book"
        sql_params["book"] = book

    results = _backend.execute_sql(
        f"SELECT v.book, v.chapter, v.verse_number, v.text FROM {VERSES_TABLE} v"
        f" WHERE v.text LIKE :name_pattern{book_filter}"
        " ORDER BY v.book, v.chapter, v.verse_number LIMIT 30",
        params=sql_params,
    )

    if not results:
        return f"No verses found mentioning '{entity_name}'" + (f" in {book}" if book else "") + "."

    verses = []
    for r in results:
        verses.append({
            "reference": f"{r['book']} {r['chapter']}:{r['verse_number']}",
            "text": r["text"],
        })
    return json.dumps({"entity": entity_name, "total": len(verses), "verses": verses}, ensure_ascii=False)


@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of an entity: type, description, all relationships, and context.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Kenneth Lay")
    """
    if CORPUS == "enron":
        all_patterns = _resolve_enron_entity_id(entity_name)
    else:
        all_patterns = [f"%{'_'.join(entity_name.lower().split())}%"]

    if CORPUS == "enron":
        entity_cols = "entity_id, name, entity_type, description, first_mention_subject AS mention_a, first_mention_thread AS mention_b"
    else:
        entity_cols = "entity_id, name, entity_type, description, first_mention_book AS mention_a, first_mention_chapter AS mention_b"

    combined = None
    for pattern in all_patterns:
        combined = _backend.execute_sql(
            f"WITH target AS ("
            f" SELECT {entity_cols} FROM {ENTITIES_TABLE}"
            f" WHERE entity_id LIKE :eid_pattern LIMIT 1"
            f") SELECT t.name, t.entity_type, t.description,"
            f" t.mention_a, t.mention_b,"
            f" COALESCE(e1.name, r.source_entity) AS src,"
            f" r.relationship_type,"
            f" COALESCE(e2.name, r.target_entity) AS tgt,"
            f" r.description AS rel_desc"
            f" FROM target t"
            f" LEFT JOIN {RELATIONSHIPS_TABLE} r"
            f"   ON r.source_entity = t.entity_id OR r.target_entity = t.entity_id"
            f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            " LIMIT 51",
            params={"eid_pattern": pattern},
        )
        if combined:
            break

    if not combined:
        return f"Entity '{entity_name}' not found in the knowledge graph."

    first = combined[0]
    if CORPUS == "enron":
        mention = f"Thread: {first.get('mention_a', 'N/A')}"
    else:
        mention = f"{first['mention_a']} ch.{first['mention_b']}"

    summary = {
        "name": first["name"],
        "type": first["entity_type"],
        "description": first["description"],
        "first_mention": mention,
    }

    if CORPUS == "enron" and first.get("entity_type", "").lower() == "person":
        try:
            email_addrs = [e for e in _resolve_name_to_email(first["name"]) if "@" in e]
            if email_addrs:
                summary["email_addresses"] = email_addrs
        except Exception:
            pass

        try:
            role_rows = _backend.execute_sql(
                f"SELECT title, department, reports_to, effective_from, effective_to, source"
                f" FROM {CATALOG}.{ENRON_SCHEMA}.person_role_timeline"
                f" WHERE LOWER(entity_id) LIKE :pattern"
                f" ORDER BY effective_from"
                f" LIMIT 5",
                params={"pattern": f"%{'_'.join(first['name'].lower().split())}%"},
            )
            if role_rows:
                summary["roles"] = [
                    {k: str(v) if v is not None else None for k, v in r.items()}
                    for r in role_rows
                ]
        except Exception:
            pass

        try:
            entity_id_pat = f"%{'_'.join(first['name'].lower().split())}%"
            analytics_rows = _backend.execute_sql(
                f"SELECT pagerank, in_degree, out_degree, total_degree"
                f" FROM {ENRON_ENTITY_ANALYTICS_TABLE}"
                f" WHERE entity_id LIKE :eid LIMIT 1",
                params={"eid": entity_id_pat},
            )
            if analytics_rows:
                a = analytics_rows[0]
                summary["centrality"] = {
                    "pagerank": round(float(a.get("pagerank", 0)), 6),
                    "in_degree": int(a.get("in_degree", 0)),
                    "out_degree": int(a.get("out_degree", 0)),
                    "total_degree": int(a.get("total_degree", 0)),
                }
        except Exception:
            pass

        try:
            dept_rows = _backend.execute_sql(
                f"SELECT department FROM {ENRON_PARTICIPANTS_TABLE}"
                f" WHERE LOWER(name_normalized) LIKE :name_pat"
                f" AND department IS NOT NULL AND department != ''"
                f" LIMIT 1",
                params={"name_pat": f"%{first['name'].lower()}%"},
            )
            if dept_rows and dept_rows[0].get("department"):
                summary["department"] = dept_rows[0]["department"]
        except Exception:
            pass

    rels = [r for r in combined if r.get("relationship_type")]
    if rels:
        from collections import defaultdict
        humanize = _maybe_humanize if CORPUS == "enron" else lambda x: x
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rels:
            groups[r["relationship_type"]].append({
                "source": humanize(r["src"]), "target": humanize(r["tgt"]),
                "description": r["rel_desc"],
            })
        summary["relationships"] = {"total": len(rels), "by_type": dict(groups)}

    return json.dumps(summary, ensure_ascii=False)


@tool
def list_entities_by_book(book: str, entity_type: str = "") -> str:
    """List all named entities that appear in a specific book or by type.
    For Bible: filter by book name. For Enron: use entity_type to filter.

    Args:
        book: The book name (Bible: "Ruth", "Genesis"). For Enron, leave empty.
        entity_type: Optional — filter by type (e.g., "Person", "Place", "Group", "Organization").
    """
    if CORPUS == "enron":
        sql_params = {}
        type_filter = ""
        if entity_type:
            type_filter = " WHERE e.entity_type = :entity_type"
            sql_params["entity_type"] = entity_type
        results = _backend.execute_sql(
            f"SELECT DISTINCT e.name, e.entity_type, e.description"
            f" FROM {ENTITIES_TABLE} e{type_filter}"
            " ORDER BY e.entity_type, e.name LIMIT 200",
            params=sql_params,
        )
        if not results:
            return f"No entities found" + (f" of type '{entity_type}'" if entity_type else "") + "."
        return json.dumps(
            _group_entities_by_type(results), ensure_ascii=False,
        )

    sql_params = {"book": book}
    type_filter = ""
    if entity_type:
        type_filter = " AND e.entity_type = :entity_type"
        sql_params["entity_type"] = entity_type

    results = _backend.execute_sql(
        f"SELECT DISTINCT e.name, e.entity_type, e.description"
        f" FROM {ENTITIES_TABLE} e"
        f" JOIN {RELATIONSHIPS_TABLE} r"
        f"   ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
        f" WHERE r.book = :book{type_filter}"
        " ORDER BY e.entity_type, e.name",
        params=sql_params,
    )

    if not results:
        return f"No entities found in '{book}'."

    return json.dumps(
        _group_entities_by_type(results, book=book), ensure_ascii=False,
    )


@tool
def find_cross_book_entities(min_books: int = 2, entity_type: str = "") -> str:
    """Find entities that appear across multiple books/threads — useful for cross-context analysis.
    For Bible: entities appearing in multiple books. For Enron: entities in multiple threads.

    Args:
        min_books: Minimum number of distinct books/threads (default: 2)
        entity_type: Optional — filter by type (e.g., "Person", "Place", "Organization").
    """
    sql_params: dict[str, str] = {"min_count": str(int(min_books))}
    type_filter = ""
    if entity_type:
        type_filter = " AND e.entity_type = :entity_type"
        sql_params["entity_type"] = entity_type

    if CORPUS == "enron":
        rows = _backend.execute_sql(
            f"SELECT e.name, e.entity_type,"
            f" COUNT(DISTINCT r.thread_id) AS thread_count"
            f" FROM {ENTITIES_TABLE} e"
            f" JOIN {RELATIONSHIPS_TABLE} r"
            f"   ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
            f" WHERE 1=1{type_filter}"
            " GROUP BY e.name, e.entity_type"
            " HAVING COUNT(DISTINCT r.thread_id) >= :min_count"
            " ORDER BY thread_count DESC, e.name"
            " LIMIT 100",
            params=sql_params,
        )
        if not rows:
            return f"No entities found appearing in {min_books}+ email threads."
        return json.dumps({
            "min_threads": int(min_books), "total": len(rows),
            "entities": [
                {"name": r["name"], "type": r["entity_type"],
                 "thread_count": int(r["thread_count"])}
                for r in rows
            ],
        }, ensure_ascii=False)

    rows = _backend.execute_sql(
        f"SELECT e.name, e.entity_type,"
        f" COUNT(DISTINCT r.book) AS book_count"
        f" FROM {ENTITIES_TABLE} e"
        f" JOIN {RELATIONSHIPS_TABLE} r"
        f"   ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
        f" WHERE 1=1{type_filter}"
        " GROUP BY e.name, e.entity_type"
        " HAVING COUNT(DISTINCT r.book) >= :min_count"
        " ORDER BY book_count DESC, e.name"
        " LIMIT 100",
        params=sql_params,
    )

    if not rows:
        type_hint = f" of type '{entity_type}'" if entity_type else ""
        return f"No entities{type_hint} found appearing in {min_books}+ books."

    return json.dumps({
        "min_books": int(min_books), "total": len(rows),
        "entities": [
            {"name": r["name"], "type": r["entity_type"],
             "book_count": int(r["book_count"])}
            for r in rows
        ],
    }, ensure_ascii=False)


_org_hierarchy_cache: dict = {"rows": None, "ts": 0.0}
_ORG_HIERARCHY_CACHE_TTL = 600  # 10 minutes


def _get_org_hierarchy_rows() -> list[dict] | None:
    """Cached loader for the org_hierarchy table (24 rows, static curated data)."""
    import time
    now = time.time()
    if _org_hierarchy_cache["rows"] is None or (now - _org_hierarchy_cache["ts"]) > _ORG_HIERARCHY_CACHE_TTL:
        oh_table = f"{CATALOG}.{ENRON_SCHEMA}.org_hierarchy"
        try:
            rows = _backend.execute_sql(
                f"SELECT DISTINCT person_id, name, title, reports_to_id FROM {oh_table}"
            )
        except Exception:
            return None
        _org_hierarchy_cache["rows"] = rows if rows else None
        _org_hierarchy_cache["ts"] = now
    return _org_hierarchy_cache["rows"]


def _trace_path_via_org_hierarchy(entity_a: str, entity_b: str) -> str | None:
    """Try to find a reporting-chain path using the org_hierarchy table.

    Returns a JSON result string if a path is found, or None to fall back to CTE.
    This is fast (24-row table) and reliable for Enron person-to-person paths.
    """
    rows = _get_org_hierarchy_rows()
    if not rows:
        return None

    by_id: dict[str, dict] = {}
    for r in rows:
        pid = r["person_id"]
        if pid not in by_id:
            by_id[pid] = r

    def _find_id(name: str) -> str | None:
        slug = "_".join(name.lower().split())
        for pid in by_id:
            if slug in pid or pid in slug:
                return pid
        for pid, info in by_id.items():
            if name.lower() in info["name"].lower() or info["name"].lower() in name.lower():
                return pid
        return None

    id_a = _find_id(entity_a)
    id_b = _find_id(entity_b)
    if not id_a or not id_b:
        return None

    def _chain_to_root(pid: str) -> list[str]:
        chain = [pid]
        visited = {pid}
        current = pid
        while current:
            parent = by_id.get(current, {}).get("reports_to_id")
            if not parent or parent in visited:
                break
            chain.append(parent)
            visited.add(parent)
            current = parent
        return chain

    chain_a = _chain_to_root(id_a)
    chain_b = _chain_to_root(id_b)

    set_a = set(chain_a)
    set_b = set(chain_b)
    common = set_a & set_b

    if not common:
        return None

    best_ancestor = None
    for node in chain_a:
        if node in common:
            best_ancestor = node
            break

    if best_ancestor is None:
        return None

    path_up = chain_a[: chain_a.index(best_ancestor) + 1]
    path_down = chain_b[: chain_b.index(best_ancestor) + 1]
    path_down.reverse()

    full_path = path_up + path_down[1:]

    steps = []
    for i in range(len(full_path) - 1):
        src = full_path[i]
        tgt = full_path[i + 1]
        src_info = by_id.get(src, {})
        tgt_info = by_id.get(tgt, {})
        if tgt == by_id.get(src, {}).get("reports_to_id"):
            rel = "REPORTS_TO"
        elif src == by_id.get(tgt, {}).get("reports_to_id"):
            rel = "MANAGES"
        else:
            rel = "REPORTS_TO"
        steps.append({
            "source": src_info.get("name", src),
            "relationship": rel,
            "target": tgt_info.get("name", tgt),
        })

    return json.dumps({
        "from": by_id.get(id_a, {}).get("name", entity_a),
        "to": by_id.get(id_b, {}).get("name", entity_b),
        "hops": len(steps),
        "path": steps,
        "source": "curated_org_hierarchy (SEC filings, DOJ records)",
    }, ensure_ascii=False)


@tool
def trace_path(entity_a: str, entity_b: str, max_hops: int = 5) -> str:
    """Find the shortest path between two entities by traversing relationships.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?' or genealogy chains.

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
        max_hops: Maximum number of hops to search (default: 5)
    """
    if CORPUS == "enron":
        oh_result = _trace_path_via_org_hierarchy(entity_a, entity_b)
        if oh_result is not None:
            return oh_result
        a_patterns = _resolve_enron_entity_id(entity_a)
        b_patterns = _resolve_enron_entity_id(entity_b)
    else:
        eid_a = "_".join(entity_a.lower().split())
        eid_b = "_".join(entity_b.lower().split())
        a_patterns = [f"%{eid_a}%"]
        b_patterns = [f"%{eid_b}%"]

    _person_filter = " AND entity_type = 'Person'" if CORPUS == "enron" else ""
    start_rows = []
    for pat in a_patterns:
        start_rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            f" WHERE entity_id LIKE :pattern{_person_filter} LIMIT 1",
            params={"pattern": pat},
        )
        if start_rows:
            break
    if not start_rows:
        for pat in a_patterns:
            start_rows = _backend.execute_sql(
                f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
                " WHERE entity_id LIKE :pattern LIMIT 1",
                params={"pattern": pat},
            )
            if start_rows:
                break

    end_rows = []
    for pat in b_patterns:
        end_rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            f" WHERE entity_id LIKE :pattern{_person_filter} LIMIT 1",
            params={"pattern": pat},
        )
        if end_rows:
            break
    if not end_rows:
        for pat in b_patterns:
            end_rows = _backend.execute_sql(
                f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
                " WHERE entity_id LIKE :pattern LIMIT 1",
                params={"pattern": pat},
            )
            if end_rows:
                break

    if not start_rows:
        return f"Entity '{entity_a}' not found in the knowledge graph."
    if not end_rows:
        return f"Entity '{entity_b}' not found in the knowledge graph."

    start_ids = {r["entity_id"] for r in start_rows}
    end_ids = {r["entity_id"] for r in end_rows}
    eid_a = next(iter(start_ids))
    eid_b = next(iter(end_ids))
    max_h = min(int(max_hops), 4 if CORPUS == "enron" else 6)

    # Slugified IDs contain only [a-z0-9_], safe for inline SQL values
    start_list = ", ".join(f"'{s}'" for s in start_ids)
    end_list = ", ".join(f"'{e}'" for e in end_ids)

    cte_query = (
        f"WITH RECURSIVE bfs(current_id, depth, visited, path_names, path_rels) AS ("
        f" SELECT e.entity_id, 0,"
        f"  CAST('|' || e.entity_id || '|' AS VARCHAR(4000)),"
        f"  CAST(e.name AS VARCHAR(4000)),"
        f"  CAST('' AS VARCHAR(4000))"
        f" FROM {ENTITIES_TABLE} e"
        f" WHERE e.entity_id IN ({start_list})"
        f" UNION ALL"
        f" SELECT"
        f"  CASE WHEN r.source_entity = b.current_id"
        f"   THEN r.target_entity ELSE r.source_entity END,"
        f"  b.depth + 1,"
        f"  b.visited"
        f"   || CASE WHEN r.source_entity = b.current_id"
        f"       THEN r.target_entity ELSE r.source_entity END || '|',"
        f"  b.path_names || '|' || COALESCE("
        f"   CASE WHEN r.source_entity = b.current_id THEN e2.name ELSE e1.name END,"
        f"   CASE WHEN r.source_entity = b.current_id"
        f"    THEN r.target_entity ELSE r.source_entity END),"
        f"  CASE WHEN b.path_rels = '' THEN r.relationship_type"
        f"   ELSE b.path_rels || '|' || r.relationship_type END"
        f" FROM bfs b"
        f" JOIN {RELATIONSHIPS_TABLE} r"
        f"  ON (r.source_entity = b.current_id OR r.target_entity = b.current_id)"
        + (f"  AND r.relationship_type IN ('REPORTS_TO','MANAGES')" if CORPUS == "enron" else "")
        + f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        f" WHERE b.depth < {max_h}"
        f"  AND b.visited NOT LIKE"
        f"   '%|' || CASE WHEN r.source_entity = b.current_id"
        f"    THEN r.target_entity ELSE r.source_entity END || '|%'"
        f") SELECT path_names, path_rels, depth FROM bfs"
        f" WHERE current_id IN ({end_list})"
        f" ORDER BY depth LIMIT 1"
    )

    path_rows = _backend.execute_sql(cte_query)

    if not path_rows:
        return (
            f"No path found between '{entity_a}' and '{entity_b}' "
            f"within {max_h} hops in the knowledge graph."
        )

    row = path_rows[0]
    names = row["path_names"].split("|")
    rels = row["path_rels"].split("|") if row["path_rels"] else []

    if CORPUS == "enron":
        names = [_maybe_humanize(n) for n in names]

    path_steps = [
        {"source": names[i], "relationship": rels[i], "target": names[i + 1]}
        for i in range(len(rels))
    ]

    if CORPUS == "enron":
        detail_cols = "COALESCE(e1.name, r.source_entity) AS src, r.relationship_type, COALESCE(e2.name, r.target_entity) AS tgt, r.description"
    else:
        detail_cols = "COALESCE(e1.name, r.source_entity) AS src, r.relationship_type, COALESCE(e2.name, r.target_entity) AS tgt, r.description, r.book, r.chapter"

    direct_rels_rows = _backend.execute_sql(
        f"SELECT {detail_cols}"
        f" FROM {RELATIONSHIPS_TABLE} r"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        " WHERE (r.source_entity LIKE :eid_a AND r.target_entity LIKE :eid_b)"
        "    OR (r.source_entity LIKE :eid_b AND r.target_entity LIKE :eid_a)"
        " LIMIT 10",
        params={"eid_a": f"%{eid_a}%", "eid_b": f"%{eid_b}%"},
    )
    direct_rels = []
    for r in direct_rels_rows:
        entry = {
            "source": r["src"], "relationship": r["relationship_type"],
            "target": r["tgt"], "description": r["description"],
        }
        if CORPUS != "enron" and "book" in r:
            entry["book"] = r["book"]
            entry["chapter"] = r.get("chapter")
        direct_rels.append(entry)

    result = {
        "from": entity_a, "to": entity_b,
        "hops": len(rels), "path": path_steps,
    }
    if direct_rels:
        result["direct_relationships"] = direct_rels
    return json.dumps(result, ensure_ascii=False)


@tool
def compare_entity_sets(
    entity_name: str = "",
    book_a: str = "",
    book_b: str = "",
    rel_type_a: str = "",
    rel_type_b: str = "",
    operation: str = "difference",
) -> str:
    """Compare two sets of entities using set operations (difference, intersection, union).
    Use this for constraint questions like 'who did Moses COMMAND but not SPOKE_TO'
    or 'entities in Exodus but not Genesis'.

    Set A is defined by (entity_name + rel_type_a + book_a).
    Set B is defined by (entity_name + rel_type_b + book_b).
    If entity_name is empty, sets are all entities in that book (optionally filtered by rel_type).

    Args:
        entity_name: Optional central entity (e.g., "Moses"). If empty, compares all entities in the two books.
        book_a: Optional book filter for set A (e.g., "Exodus").
        book_b: Optional book filter for set B (e.g., "Genesis").
        rel_type_a: Optional relationship type filter for set A (e.g., "COMMANDED").
        rel_type_b: Optional relationship type filter for set B (e.g., "SPOKE_TO").
        operation: One of "difference" (A-B), "intersection" (A&B), or "union" (A|B). Default: "difference".
    """
    def _build_set_conditions(rel_type: str, alias: str, book: str = "") -> tuple[str, dict]:
        conditions = []
        params = {}
        if entity_name:
            eid = "_".join(entity_name.lower().split())
            eid_key = f"eid_{alias}"
            conditions.append(
                f"(r.source_entity LIKE :{eid_key} OR r.target_entity LIKE :{eid_key})"
            )
            params[eid_key] = f"%{eid}%"
        if book and CORPUS != "enron":
            bk_key = f"book_{alias}"
            conditions.append(f"r.book = :{bk_key}")
            params[bk_key] = book
        if rel_type:
            rt_key = f"rt_{alias}"
            conditions.append(f"r.relationship_type = :{rt_key}")
            params[rt_key] = rel_type
        where = " AND ".join(conditions) if conditions else "1=1"
        return where, params

    if not entity_name and not book_a and not book_b and not rel_type_a and not rel_type_b:
        return "Please provide at least entity_name, or relationship type filters to compare."

    where_a, params_a = _build_set_conditions(rel_type_a, "a", book_a)
    set_a_rows = _backend.execute_sql(
        f"SELECT DISTINCT COALESCE(e2.name, r.target_entity) AS neighbor,"
        f" r.relationship_type"
        f" FROM {RELATIONSHIPS_TABLE} r"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" WHERE {where_a}",
        params=params_a,
    )

    where_b, params_b = _build_set_conditions(rel_type_b, "b", book_b)
    set_b_rows = _backend.execute_sql(
        f"SELECT DISTINCT COALESCE(e2.name, r.target_entity) AS neighbor,"
        f" r.relationship_type"
        f" FROM {RELATIONSHIPS_TABLE} r"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" WHERE {where_b}",
        params=params_b,
    )

    names_a = {r["neighbor"] for r in set_a_rows if r["neighbor"]}
    names_b = {r["neighbor"] for r in set_b_rows if r["neighbor"]}

    if entity_name:
        names_a.discard(entity_name)
        names_b.discard(entity_name)

    op = operation.lower()
    if op == "difference":
        result_set = sorted(names_a - names_b)
        op_label = "A \\ B (in A but not B)"
    elif op == "intersection":
        result_set = sorted(names_a & names_b)
        op_label = "A ∩ B (in both)"
    elif op == "union":
        result_set = sorted(names_a | names_b)
        op_label = "A ∪ B (in either)"
    else:
        return f"Unknown operation '{operation}'. Use 'difference', 'intersection', or 'union'."

    desc_a = " + ".join(filter(None, [entity_name, rel_type_a, book_a])) or "(all)"
    desc_b = " + ".join(filter(None, [entity_name, rel_type_b, book_b])) or "(all)"

    return json.dumps({
        "set_a": {"description": desc_a, "count": len(names_a)},
        "set_b": {"description": desc_b, "count": len(names_b)},
        "operation": op_label,
        "result_count": len(result_set),
        "result": result_set,
    }, ensure_ascii=False)


@tool
def query_timeline(person_name: str = "", date_from: str = "", date_to: str = "", category: str = "") -> str:
    """Query the Enron investigation timeline for key events.
    Use this for temporal questions about what happened at specific times, sequences of events, or milestones.

    Args:
        person_name: Optional — filter events involving a specific person (e.g., "Jeff Skilling")
        date_from: Optional — start date filter (YYYY-MM-DD format, e.g., "2001-08-01")
        date_to: Optional — end date filter (YYYY-MM-DD format, e.g., "2001-12-31")
        category: Optional — event category filter (resignation, whistleblower, financial_event, regulatory, criminal_investigation, leadership_change, bankruptcy, conviction, death, public_statement, document_destruction, congressional_testimony)
    """
    if CORPUS != "enron":
        return "Timeline is only available for the Enron corpus."

    timeline_table = f"{CATALOG}.{ENRON_SCHEMA}.investigation_timeline"

    conditions = []
    params = {}

    if person_name:
        conditions.append(
            "EXISTS(SELECT 1 FROM EXPLODE(key_persons) AS t(p) "
            "WHERE LOWER(t.p) LIKE :person_pattern)"
        )
        params["person_pattern"] = f"%{person_name.lower()}%"
    if date_from:
        conditions.append("event_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("event_date <= :date_to")
        params["date_to"] = date_to
    if category:
        conditions.append("category = :category")
        params["category"] = category.lower()

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    try:
        results = _backend.execute_sql(
            f"SELECT event_date, description, category, key_persons"
            f" FROM {timeline_table}{where}"
            f" ORDER BY event_date"
            " LIMIT 30",
            params=params,
        )
    except Exception as exc:
        log.warning("Timeline query failed (table may not exist): %s", exc)
        return "Investigation timeline table is not available."

    if not results:
        filters = []
        if person_name:
            filters.append(f"person={person_name}")
        if date_from or date_to:
            filters.append(f"dates={date_from or '...'} to {date_to or '...'}")
        if category:
            filters.append(f"category={category}")
        return f"No timeline events found matching: {', '.join(filters) or 'no filters'}."

    events = []
    for r in results:
        events.append({
            "date": str(r.get("event_date", "")),
            "description": r.get("description", ""),
            "category": r.get("category", ""),
            "key_persons": r.get("key_persons", []),
        })

    return json.dumps({"events": events, "count": len(events)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Investigative analysis tools (Enron-only)
# ---------------------------------------------------------------------------

@tool
def detect_self_emails(limit: int = 20) -> str:
    """Find people who sent emails from their corporate Enron account to their own personal account.
    Detects cross-domain same-person email pairs — a key data exfiltration signal.
    Use for "who forwarded to personal email?", "self-emailing patterns", or data exfiltration analysis.

    Args:
        limit: Max number of self-email pairs to return (default 20).
    """
    if CORPUS != "enron":
        return "detect_self_emails is only available for the Enron corpus."

    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    sql = (
        f"SELECT d.person_a, d.person_b,"
        f" SUM(d.total_count) AS total,"
        f" MAX(d.total_count) AS peak_week,"
        f" MIN(d.period) AS first_seen,"
        f" MAX(d.period) AS last_seen,"
        f" COUNT(DISTINCT d.period) AS active_weeks"
        f" FROM {dyads_table} d"
        f" WHERE ("
        f"   (d.person_a LIKE '%@enron.com' AND d.person_b NOT LIKE '%@enron.com')"
        f"   OR (d.person_b LIKE '%@enron.com' AND d.person_a NOT LIKE '%@enron.com')"
        f" )"
        f" GROUP BY d.person_a, d.person_b"
        f" HAVING SUM(d.total_count) >= 3"
        f" ORDER BY total DESC"
        f" LIMIT 200"
    )
    results = _backend.execute_sql(sql)
    if not results:
        return "No cross-domain email pairs found."

    self_pairs = []
    for r in results:
        a, b = r["person_a"], r["person_b"]
        if _is_likely_same_person(a, b):
            corp = a if "@enron.com" in a else b
            personal = b if "@enron.com" in b != "@enron.com" in a else a
            if "@enron.com" not in personal:
                pass
            else:
                corp, personal = personal, corp
            corp_local = _email_local_part(corp)
            display = corp_local.replace(".", " ").title() if corp_local else corp
            try:
                name_rows = _backend.execute_sql(
                    f"SELECT COALESCE(name_normalized, display_name) AS name"
                    f" FROM {participants_table}"
                    f" WHERE email_address = :email LIMIT 1",
                    params={"email": corp},
                )
                if name_rows and name_rows[0].get("name"):
                    display = name_rows[0]["name"]
            except Exception:
                pass

            self_pairs.append({
                "person": display,
                "corporate_email": corp,
                "personal_email": personal,
                "total_emails": int(r["total"]),
                "peak_week_volume": int(r["peak_week"]),
                "first_seen": str(r.get("first_seen", "")),
                "last_seen": str(r.get("last_seen", "")),
                "active_weeks": int(r.get("active_weeks", 0)),
            })
        if len(self_pairs) >= limit:
            break

    return json.dumps({
        "source": "communication_dyads",
        "description": "Corporate-to-personal self-email detection",
        "total_found": len(self_pairs),
        "self_email_pairs": self_pairs,
    }, ensure_ascii=False)


@tool
def get_external_contacts(entity_name: str = "", direction: str = "both", limit: int = 20) -> str:
    """Find who communicated most with non-Enron (external) email addresses.
    If entity_name is given, shows that person's external contacts.
    If empty, shows a corpus-wide ranking of people with the most external communication.
    Use for "who emailed outside the company?", "external contacts", or investigating information flow outside Enron.

    Args:
        entity_name: Optional person name. If empty, returns corpus-wide external communication ranking.
        direction: "both" (default), "outbound" (sent to external), or "inbound" (received from external).
        limit: Max number of results (default 20).
    """
    if CORPUS != "enron":
        return "get_external_contacts is only available for the Enron corpus."

    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    if entity_name:
        email_patterns = _resolve_name_to_email(entity_name)
        results = None
        for ep in email_patterns:
            if direction == "outbound":
                sql = (
                    f"SELECT d.person_b AS external_email, SUM(d.total_count) AS total"
                    f" FROM {dyads_table} d"
                    f" WHERE LOWER(d.person_a) LIKE :email_pat"
                    f" AND d.person_b NOT LIKE '%@enron.com'"
                    f" GROUP BY d.person_b ORDER BY total DESC LIMIT {int(limit)}"
                )
            elif direction == "inbound":
                sql = (
                    f"SELECT d.person_a AS external_email, SUM(d.total_count) AS total"
                    f" FROM {dyads_table} d"
                    f" WHERE LOWER(d.person_b) LIKE :email_pat"
                    f" AND d.person_a NOT LIKE '%@enron.com'"
                    f" GROUP BY d.person_a ORDER BY total DESC LIMIT {int(limit)}"
                )
            else:
                sql = (
                    f"SELECT external_email, SUM(cnt) AS total FROM ("
                    f"  SELECT d.person_b AS external_email, SUM(d.total_count) AS cnt"
                    f"  FROM {dyads_table} d"
                    f"  WHERE LOWER(d.person_a) LIKE :email_pat"
                    f"  AND d.person_b NOT LIKE '%@enron.com'"
                    f"  GROUP BY d.person_b"
                    f"  UNION ALL"
                    f"  SELECT d.person_a AS external_email, SUM(d.total_count) AS cnt"
                    f"  FROM {dyads_table} d"
                    f"  WHERE LOWER(d.person_b) LIKE :email_pat"
                    f"  AND d.person_a NOT LIKE '%@enron.com'"
                    f"  GROUP BY d.person_a"
                    f" ) combined GROUP BY external_email ORDER BY total DESC LIMIT {int(limit)}"
                )
            results = _backend.execute_sql(sql, params={"email_pat": ep})
            if results:
                break

        if not results:
            return f"No external contacts found for '{entity_name}'."

        contacts = []
        for r in results:
            addr = r["external_email"]
            domain = addr.split("@")[1] if "@" in addr else "unknown"
            local = addr.split("@")[0] if "@" in addr else addr
            contacts.append({
                "email": addr,
                "name": local.replace(".", " ").replace("_", " ").title(),
                "domain": domain,
                "total_emails": int(r["total"]),
            })

        return json.dumps({
            "entity": entity_name,
            "direction": direction,
            "source": "communication_dyads",
            "external_contacts": contacts,
        }, ensure_ascii=False)

    else:
        sql = (
            f"SELECT enron_person, SUM(total) AS ext_total,"
            f" COUNT(DISTINCT external_email) AS unique_externals FROM ("
            f"  SELECT d.person_a AS enron_person, d.person_b AS external_email,"
            f"  SUM(d.total_count) AS total"
            f"  FROM {dyads_table} d"
            f"  WHERE d.person_a LIKE '%@enron.com'"
            f"  AND d.person_b NOT LIKE '%@enron.com'"
            f"  GROUP BY d.person_a, d.person_b"
            f"  UNION ALL"
            f"  SELECT d.person_b AS enron_person, d.person_a AS external_email,"
            f"  SUM(d.total_count) AS total"
            f"  FROM {dyads_table} d"
            f"  WHERE d.person_b LIKE '%@enron.com'"
            f"  AND d.person_a NOT LIKE '%@enron.com'"
            f"  GROUP BY d.person_b, d.person_a"
            f" ) combined"
            f" GROUP BY enron_person"
            f" ORDER BY ext_total DESC LIMIT {int(limit)}"
        )
        results = _backend.execute_sql(sql)
        if not results:
            return "No external communication found in the corpus."

        display_map: dict[str, str] = {}
        try:
            emails = [r["enron_person"] for r in results]
            chunks = [emails[i:i + 20] for i in range(0, len(emails), 20)]
            for chunk in chunks:
                conditions = " OR ".join(f"email_address = :e{i}" for i in range(len(chunk)))
                params = {f"e{i}": e for i, e in enumerate(chunk)}
                name_rows = _backend.execute_sql(
                    f"SELECT email_address,"
                    f" COALESCE(name_normalized, display_name, email_address) AS display"
                    f" FROM {participants_table} WHERE {conditions}",
                    params=params,
                )
                for nr in name_rows:
                    display_map[nr["email_address"]] = nr["display"]
        except Exception:
            pass

        ranking = []
        for r in results:
            addr = r["enron_person"]
            name = display_map.get(addr, "")
            if not name or "@" in name:
                local = addr.split("@")[0] if "@" in addr else addr
                name = local.replace(".", " ").replace("_", " ").title()
            ranking.append({
                "person": name,
                "email": addr,
                "total_external_emails": int(r["ext_total"]),
                "unique_external_contacts": int(r["unique_externals"]),
            })

        return json.dumps({
            "source": "communication_dyads",
            "description": "Corpus-wide external communication ranking",
            "ranking": ranking,
        }, ensure_ascii=False)


@tool
def get_communication_timeline(
    entity_name: str = "",
    entity_b: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Get time-series email volume data, using weekly periods from communication_dyads or person_activity.
    Use for "how did email volume change over time?", "communication spikes", or temporal pattern analysis.

    Args:
        entity_name: Optional person name. If empty, returns corpus-wide weekly volume.
        entity_b: Optional second person. If both given, shows pairwise weekly volume.
        date_from: Optional start date (YYYY-MM-DD) to filter the time range.
        date_to: Optional end date (YYYY-MM-DD) to filter the time range.
    """
    if CORPUS != "enron":
        return "get_communication_timeline is only available for the Enron corpus."

    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    activity_table = ENRON_PERSON_ACTIVITY_TABLE
    emails_table = ENRON_EMAILS_TABLE

    date_conditions = ""
    date_params: dict = {}
    if date_from:
        date_conditions += " AND period >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_conditions += " AND period <= :date_to"
        date_params["date_to"] = date_to

    if entity_name and entity_b:
        email_pats_a = _resolve_name_to_email(entity_name)
        email_pats_b = _resolve_name_to_email(entity_b)
        results = None
        for ep_a in email_pats_a:
            for ep_b in email_pats_b:
                sql = (
                    f"SELECT d.period, SUM(d.total_count) AS total"
                    f" FROM {dyads_table} d"
                    f" WHERE ("
                    f"   (LOWER(d.person_a) LIKE :pat_a AND LOWER(d.person_b) LIKE :pat_b)"
                    f"   OR (LOWER(d.person_a) LIKE :pat_b AND LOWER(d.person_b) LIKE :pat_a)"
                    f" ){date_conditions}"
                    f" GROUP BY d.period ORDER BY d.period"
                )
                params = {"pat_a": ep_a, "pat_b": ep_b, **date_params}
                results = _backend.execute_sql(sql, params=params)
                if results:
                    break
            if results:
                break
        if not results:
            return f"No communication timeline found between '{entity_name}' and '{entity_b}'."
        series = [{"period": str(r["period"]), "total": int(r["total"])} for r in results]
        return json.dumps({
            "between": [entity_name, entity_b],
            "source": "communication_dyads",
            "time_series": series,
        }, ensure_ascii=False)

    elif entity_name:
        email_patterns = _resolve_name_to_email(entity_name)
        results = None
        for ep in email_patterns:
            date_conds_activity = date_conditions.replace("period", "period")
            sql = (
                f"SELECT period,"
                f" COALESCE(emails_sent, 0) AS sent,"
                f" COALESCE(emails_received, 0) AS received,"
                f" COALESCE(emails_sent, 0) + COALESCE(emails_received, 0) AS total"
                f" FROM {activity_table}"
                f" WHERE LOWER(person_id) LIKE :email_pat{date_conds_activity}"
                f" ORDER BY period"
            )
            results = _backend.execute_sql(sql, params={"email_pat": ep, **date_params})
            if results:
                break
        if not results:
            return f"No activity timeline found for '{entity_name}'."
        series = [{
            "period": str(r["period"]),
            "sent": int(r["sent"]),
            "received": int(r["received"]),
            "total": int(r["total"]),
        } for r in results]
        return json.dumps({
            "entity": entity_name,
            "source": "person_activity",
            "time_series": series,
        }, ensure_ascii=False)

    else:
        date_conds_email = ""
        if date_from:
            date_conds_email += " AND date >= :date_from"
        if date_to:
            date_conds_email += " AND date <= :date_to"
        sql = (
            f"SELECT DATE_TRUNC('week', date) AS period, COUNT(*) AS total"
            f" FROM {emails_table}"
            f" WHERE date IS NOT NULL{date_conds_email}"
            f" GROUP BY 1 ORDER BY 1"
        )
        results = _backend.execute_sql(sql, params=date_params)
        if not results:
            return "No email volume data found for the corpus."
        series = [{"period": str(r["period"]), "total": int(r["total"])} for r in results]
        return json.dumps({
            "source": "emails",
            "description": "Corpus-wide weekly email volume",
            "time_series": series,
        }, ensure_ascii=False)


@tool
def get_activity_anomalies(entity_name: str = "", metric: str = "all", limit: int = 20) -> str:
    """Surface unusual behavioral patterns from person_activity: BCC-heavy usage, after-hours emailing,
    weekend activity, or volume spikes. Use for "who used BCC the most?", "after-hours email patterns",
    "suspicious activity", or behavioral anomaly detection.

    Args:
        entity_name: Optional person name. If given, shows that person's anomaly profile over time.
        metric: Which anomaly to surface — "bcc_heavy", "after_hours", "weekend", "volume_spike", or "all" (default).
        limit: Max results (default 20).
    """
    if CORPUS != "enron":
        return "get_activity_anomalies is only available for the Enron corpus."

    activity_table = ENRON_PERSON_ACTIVITY_TABLE
    participants_table = ENRON_PARTICIPANTS_TABLE

    if entity_name:
        email_patterns = _resolve_name_to_email(entity_name)
        results = None
        for ep in email_patterns:
            sql = (
                f"SELECT period, emails_sent, emails_received, unique_contacts_sent,"
                f" bcc_emails_sent, after_hours_count, weekend_count"
                f" FROM {activity_table}"
                f" WHERE LOWER(person_id) LIKE :email_pat"
                f" ORDER BY period"
            )
            results = _backend.execute_sql(sql, params={"email_pat": ep})
            if results:
                break
        if not results:
            return f"No activity data found for '{entity_name}'."
        profile = []
        for r in results:
            sent = int(r.get("emails_sent") or 0)
            entry = {
                "period": str(r["period"]),
                "emails_sent": sent,
                "emails_received": int(r.get("emails_received") or 0),
                "bcc_emails_sent": int(r.get("bcc_emails_sent") or 0),
                "bcc_ratio": round(int(r.get("bcc_emails_sent") or 0) / max(sent, 1), 3),
                "after_hours_count": int(r.get("after_hours_count") or 0),
                "weekend_count": int(r.get("weekend_count") or 0),
                "unique_contacts": int(r.get("unique_contacts_sent") or 0),
            }
            profile.append(entry)
        return json.dumps({
            "entity": entity_name,
            "source": "person_activity",
            "profile": profile,
        }, ensure_ascii=False)

    metric_queries = {
        "bcc_heavy": (
            f"SELECT person_id,"
            f" SUM(bcc_emails_sent) AS bcc_total,"
            f" SUM(emails_sent) AS sent_total,"
            f" ROUND(SUM(bcc_emails_sent) * 1.0 / NULLIF(SUM(emails_sent), 0), 3) AS bcc_ratio"
            f" FROM {activity_table}"
            f" GROUP BY person_id HAVING SUM(emails_sent) >= 10"
            f" ORDER BY bcc_ratio DESC LIMIT {int(limit)}"
        ),
        "after_hours": (
            f"SELECT person_id,"
            f" SUM(after_hours_count) AS after_hours_total,"
            f" SUM(emails_sent) AS sent_total,"
            f" ROUND(SUM(after_hours_count) * 1.0 / NULLIF(SUM(emails_sent), 0), 3) AS after_hours_ratio"
            f" FROM {activity_table}"
            f" GROUP BY person_id HAVING SUM(emails_sent) >= 10"
            f" ORDER BY after_hours_total DESC LIMIT {int(limit)}"
        ),
        "weekend": (
            f"SELECT person_id,"
            f" SUM(weekend_count) AS weekend_total,"
            f" SUM(emails_sent) AS sent_total,"
            f" ROUND(SUM(weekend_count) * 1.0 / NULLIF(SUM(emails_sent), 0), 3) AS weekend_ratio"
            f" FROM {activity_table}"
            f" GROUP BY person_id HAVING SUM(emails_sent) >= 10"
            f" ORDER BY weekend_total DESC LIMIT {int(limit)}"
        ),
        "volume_spike": (
            f"SELECT person_id, period, emails_sent,"
            f" LAG(emails_sent) OVER (PARTITION BY person_id ORDER BY period) AS prev_sent"
            f" FROM {activity_table}"
            f" WHERE emails_sent >= 10"
            f" ORDER BY emails_sent DESC"
            f" LIMIT {int(limit)}"
        ),
    }

    if metric == "all":
        metrics_to_run = ["bcc_heavy", "after_hours", "weekend"]
    else:
        if metric not in metric_queries:
            return f"Unknown metric '{metric}'. Choose from: bcc_heavy, after_hours, weekend, volume_spike, all."
        metrics_to_run = [metric]

    def _resolve_display(person_id: str) -> str:
        local = person_id.split("@")[0] if "@" in person_id else person_id
        return local.replace(".", " ").replace("_", " ").title()

    all_anomalies = {}
    for m in metrics_to_run:
        try:
            rows = _backend.execute_sql(metric_queries[m])
        except Exception:
            rows = []
        entries = []
        for r in rows:
            entry = {"person": _resolve_display(r["person_id"]), "email": r["person_id"]}
            for k, v in r.items():
                if k != "person_id":
                    if v is None:
                        entry[k] = 0
                    else:
                        try:
                            fv = float(v)
                            entry[k] = int(fv) if fv == int(fv) else round(fv, 3)
                        except (ValueError, TypeError):
                            entry[k] = str(v)
            entries.append(entry)
        all_anomalies[m] = entries

    return json.dumps({
        "source": "person_activity",
        "metrics_queried": metrics_to_run,
        "anomalies": all_anomalies,
    }, ensure_ascii=False)


@tool
def search_emails(
    keywords: str,
    date_from: str = "",
    date_to: str = "",
    sender: str = "",
    recipient: str = "",
    limit: int = 15,
) -> str:
    """Search email content by keywords with optional date range, sender, and recipient filters.
    Keywords are comma-separated and OR-matched against subject and body.
    Use for investigative keyword sweeps like "shred", "delete", "destroy", "off the record".

    Args:
        keywords: Comma-separated keywords to search for (e.g., "shred, delete, destroy").
        date_from: Optional start date (YYYY-MM-DD).
        date_to: Optional end date (YYYY-MM-DD).
        sender: Optional sender name or email to filter by.
        recipient: Optional recipient name or email to filter (searches to, cc, bcc fields).
        limit: Max results (default 15).
    """
    if CORPUS != "enron":
        return "search_emails is only available for the Enron corpus."

    cfg = _get_corpus_config()
    source_table = cfg["source_table"]

    kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        return "No keywords provided. Pass comma-separated keywords to search."

    kw_conditions = []
    params: dict = {}
    for i, kw in enumerate(kw_list):
        param_name = f"kw{i}"
        kw_conditions.append(f"(LOWER(subject) LIKE :{param_name} OR LOWER(body) LIKE :{param_name})")
        params[param_name] = f"%{kw}%"

    where_parts = [f"({' OR '.join(kw_conditions)})"]
    if date_from:
        where_parts.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where_parts.append("date <= :date_to")
        params["date_to"] = date_to
    if sender:
        sender_pats = _resolve_name_to_email(sender)
        if sender_pats:
            where_parts.append(f"LOWER(sender) LIKE :sender_pat")
            params["sender_pat"] = sender_pats[0]
    if recipient:
        recip_pats = _resolve_name_to_email(recipient)
        if recip_pats:
            where_parts.append(
                f"(LOWER(CAST(to_recipients AS STRING)) LIKE :recip_pat"
                f" OR LOWER(CAST(cc_recipients AS STRING)) LIKE :recip_pat"
                f" OR LOWER(CAST(bcc_recipients AS STRING)) LIKE :recip_pat)"
            )
            params["recip_pat"] = recip_pats[0]

    where_clause = " AND ".join(where_parts)

    sql = (
        f"SELECT date, sender, subject,"
        f" SUBSTR(body, 1, 300) AS body_preview"
        f" FROM {source_table}"
        f" WHERE {where_clause}"
        f" ORDER BY date DESC"
        f" LIMIT {int(limit)}"
    )

    try:
        results = _backend.execute_sql(sql, params=params)
    except Exception as exc:
        log.warning("search_emails query failed: %s", exc)
        return f"Search query failed: {exc}"

    if not results:
        filters = [f"keywords={keywords}"]
        if date_from:
            filters.append(f"from={date_from}")
        if date_to:
            filters.append(f"to={date_to}")
        if sender:
            filters.append(f"sender={sender}")
        if recipient:
            filters.append(f"recipient={recipient}")
        return f"No emails found matching: {', '.join(filters)}."

    emails = []
    for r in results:
        emails.append({
            "date": str(r.get("date", "")),
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": r.get("body_preview", ""),
        })

    return json.dumps({
        "keywords": kw_list,
        "filters": {
            "date_from": date_from or None,
            "date_to": date_to or None,
            "sender": sender or None,
            "recipient": recipient or None,
        },
        "total": len(emails),
        "emails": emails,
    }, ensure_ascii=False)


@tool
def semantic_search_emails(query: str, limit: int = 10) -> str:
    """Search emails by semantic meaning using vector similarity.
    Better than keyword search for conceptual queries like 'financial irregularities',
    'corporate governance failures', or 'accounting concerns'.

    Args:
        query: Natural language query describing what you're looking for.
        limit: Max results (default 10).
    """
    if CORPUS != "enron":
        return "semantic_search_emails is only available for the Enron corpus."

    if VS_ENDPOINT and VS_INDEX_NAME:
        try:
            from databricks.vector_search.client import VectorSearchClient
            vsc = VectorSearchClient()
            idx = vsc.get_index(VS_ENDPOINT, VS_INDEX_NAME)
            vs_results = idx.similarity_search(
                query_text=query,
                columns=["date", "sender", "subject", "body"],
                num_results=limit,
            )
            rows = vs_results.get("result", {}).get("data_array", [])
            cols = [c["name"] for c in vs_results.get("manifest", {}).get("columns", [])]
            emails = []
            for row in rows:
                item = dict(zip(cols, row))
                emails.append({
                    "date": str(item.get("date", "")),
                    "sender": item.get("sender", ""),
                    "subject": item.get("subject", ""),
                    "body_preview": str(item.get("body", ""))[:300],
                })
            return json.dumps({
                "query": query, "method": "vector_search",
                "total": len(emails), "emails": emails,
            }, ensure_ascii=False)
        except Exception as exc:
            log.warning("Vector search failed, falling back to SQL: %s", exc)

    cfg = _get_corpus_config()
    source_table = cfg["source_table"]
    words = [w.strip().lower() for w in query.split() if len(w.strip()) > 2]
    if not words:
        return "No meaningful terms in query."
    kw_conditions = []
    params: dict = {}
    for i, w in enumerate(words[:8]):
        p = f"sem{i}"
        kw_conditions.append(f"(LOWER(subject) LIKE :{p} OR LOWER(body) LIKE :{p})")
        params[p] = f"%{w}%"
    where = " OR ".join(kw_conditions)
    sql = (
        f"SELECT date, sender, subject, SUBSTR(body, 1, 300) AS body_preview"
        f" FROM {source_table}"
        f" WHERE ({where})"
        f" ORDER BY date DESC"
        f" LIMIT {int(limit)}"
    )
    try:
        results = _backend.execute_sql(sql, params=params)
    except Exception as exc:
        return f"Semantic search fallback failed: {exc}"
    if not results:
        return f"No emails found matching semantic query: {query}"
    emails = []
    for r in results:
        emails.append({
            "date": str(r.get("date", "")),
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": r.get("body_preview", ""),
        })
    return json.dumps({
        "query": query, "method": "sql_fallback",
        "total": len(emails), "emails": emails,
    }, ensure_ascii=False)


def _genie_sql_fallback(question: str, space_name: str) -> dict | None:
    """Direct SQL fallback when Genie is unavailable (e.g. Model Serving identity issues).

    Returns a genie_result-shaped dict on success, None if no fallback applies.
    """
    q_lower = question.lower()
    emails_table = f"{CATALOG}.{ENRON_SCHEMA}.emails"
    participants_table = f"{CATALOG}.{ENRON_SCHEMA}.participants"

    try:
        if any(kw in q_lower for kw in ("business hours", "after hours", "outside of",
                                         "weekend", "evening", "night", "before 9", "after 5", "after 6")):
            is_weekend = "weekend" in q_lower
            if is_weekend:
                time_filter = "DAYOFWEEK(e.date) IN (1, 7)"
                label = "weekend"
            else:
                time_filter = "(HOUR(e.date) < 9 OR HOUR(e.date) >= 17)"
                label = "outside business hours (before 9am or after 5pm)"

            if any(kw in q_lower for kw in ("pair", "between", "each other", "communicated", "exchanged")):
                sql = (
                    f"SELECT p_from.display_name AS person_a, p_from.email AS email_a,"
                    f" p_to.display_name AS person_b, p_to.email AS email_b,"
                    f" COUNT(*) AS total_emails"
                    f" FROM {emails_table} e"
                    f" JOIN {participants_table} p_from ON e.message_id = p_from.message_id AND p_from.role = 'from'"
                    f" JOIN {participants_table} p_to ON e.message_id = p_to.message_id AND p_to.role = 'to'"
                    f" WHERE e.date IS NOT NULL AND {time_filter}"
                    f" GROUP BY 1, 2, 3, 4"
                    f" ORDER BY total_emails DESC"
                    f" LIMIT 20"
                )
            else:
                sql = (
                    f"SELECT p.display_name, p.email, COUNT(*) AS total_emails"
                    f" FROM {emails_table} e"
                    f" JOIN {participants_table} p ON e.message_id = p.message_id AND p.role = 'from'"
                    f" WHERE e.date IS NOT NULL AND {time_filter}"
                    f" GROUP BY 1, 2"
                    f" ORDER BY total_emails DESC"
                    f" LIMIT 20"
                )

            rows = _backend.execute_sql(sql)
            return {
                "source": "direct_sql_fallback",
                "space": space_name,
                "query": question,
                "sql": sql,
                "filter_description": label,
                "results": rows,
                "row_count": len(rows),
            }
    except Exception as exc:
        log.warning("Genie SQL fallback failed: %s", exc)
    return None


@tool
def query_and_enrich(question: str, space_name: str = "auto") -> str:
    """Query a Genie Space for analytical answers, then enrich with graph context.
    Phase 1: Sends the question to the specified Genie Space for SQL-based analysis.
    Phase 2: Enriches the result with entity context and data quality caveats from the knowledge graph.

    Args:
        question: The analytical question to ask (e.g., "What percentage of emails were internal?").
        space_name: Which Genie Space to query — auto (recommended), communication_analytics, organizational_intelligence, or email_investigation.
                    When "auto", selects the best space based on question keywords.
    """
    if CORPUS != "enron":
        return "query_and_enrich is only available for the Enron corpus."

    genie_space_ids = {
        "communication_analytics": os.environ.get("GENIE_COMM_SPACE_ID", ""),
        "organizational_intelligence": os.environ.get("GENIE_ORG_SPACE_ID", ""),
        "email_investigation": os.environ.get("GENIE_INVEST_SPACE_ID", ""),
    }

    if space_name == "auto":
        q_lower = question.lower()
        _time_kw = ("business hours", "after hours", "weekend", "time of day",
                     "hour", "morning", "evening", "night", "sent_date",
                     "before 9", "after 5", "after 6", "outside of")
        _org_kw = ("report to", "department", "role", "title", "hierarchy",
                    "manager", "direct report", "c-suite", "executive")
        if any(kw in q_lower for kw in _time_kw):
            space_name = "email_investigation"
        elif any(kw in q_lower for kw in _org_kw):
            space_name = "organizational_intelligence"
        else:
            space_name = "communication_analytics"

    space_id = genie_space_ids.get(space_name, "")
    if not space_id:
        return json.dumps({
            "error": f"Genie Space '{space_name}' not configured. Set GENIE_*_SPACE_ID env vars.",
            "available_spaces": list(genie_space_ids.keys()),
        })

    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        host = w.config.host.rstrip("/")

        conv_resp = w.api_client.do(
            "POST",
            f"/api/2.0/genie/spaces/{space_id}/start-conversation",
            body={"content": question},
        )
        conversation_id = conv_resp.get("conversation_id", "")
        message_id = conv_resp.get("message_id", "")

        import time as _time
        genie_result = None
        for _attempt in range(30):
            _time.sleep(2)
            msg_resp = w.api_client.do(
                "GET",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}",
            )
            status = msg_resp.get("status", "")
            if status in ("COMPLETED", "COMPLETED_WITH_ERROR"):
                attachments = msg_resp.get("attachments", [])
                sql_query = ""
                result_data = []
                for att in attachments:
                    query_info = att.get("query", {})
                    if query_info.get("query"):
                        sql_query = query_info["query"]
                    att_desc = att.get("text", {}).get("content", "")
                    if att_desc:
                        result_data.append(att_desc)
                genie_result = {
                    "source": "genie",
                    "space": space_name,
                    "query": question,
                    "sql_generated": sql_query,
                    "response_text": "\n".join(result_data) if result_data else str(msg_resp),
                    "status": status,
                }
                break
            elif status == "FAILED":
                genie_result = {
                    "source": "genie",
                    "space": space_name,
                    "error": f"Genie query failed: {msg_resp.get('error', status)}",
                }
                break

        if genie_result is None:
            genie_result = {
                "source": "genie",
                "space": space_name,
                "error": "Genie query timed out after 60s",
            }
    except Exception as exc:
        genie_result = {
            "source": "genie",
            "space": space_name,
            "error": f"Genie query failed: {exc}",
        }

    if genie_result.get("error"):
        fallback = _genie_sql_fallback(question, space_name)
        if fallback:
            genie_result = fallback

    enrichment = {}
    try:
        quality_rows = _backend.execute_sql(
            f"SELECT table_name, SUM(null_count) as total_nulls, AVG(null_rate) as avg_null_rate"
            f" FROM {CATALOG}.{ENRON_SCHEMA}.data_quality_report"
            f" GROUP BY table_name"
            f" ORDER BY avg_null_rate DESC LIMIT 5"
        )
        if quality_rows:
            enrichment["data_quality_caveats"] = quality_rows
    except Exception:
        pass

    q_lower = question.lower()
    person_names = _heuristic_entity_names(question)

    for pname in person_names[:2]:
        try:
            role_rows = _backend.execute_sql(
                f"SELECT entity_id, title, department, reports_to, effective_from, effective_to, source"
                f" FROM {CATALOG}.{ENRON_SCHEMA}.person_role_timeline"
                f" WHERE LOWER(entity_id) LIKE :pattern"
                f" ORDER BY effective_from"
                f" LIMIT 5",
                params={"pattern": f"%{'_'.join(pname.lower().split())}%"},
            )
            if role_rows:
                enrichment.setdefault("role_context", {})[pname] = role_rows
        except Exception:
            pass

    try:
        cov_rows = _backend.execute_sql(
            f"SELECT metric_name, coverage_pct"
            f" FROM {CATALOG}.{ENRON_SCHEMA}.corpus_coverage"
            f" WHERE coverage_pct < 80"
        )
        if cov_rows:
            enrichment["coverage_warnings"] = [
                f"{r['metric_name']}: {r.get('coverage_pct', 0):.1f}%"
                for r in cov_rows
            ]
    except Exception:
        pass

    try:
        cls_rows = _backend.execute_sql(
            f"SELECT email_type, COUNT(*) AS cnt,"
            f" ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) AS pct"
            f" FROM {CATALOG}.{ENRON_SCHEMA}.email_classification"
            f" GROUP BY email_type"
            f" ORDER BY cnt DESC"
        )
        if cls_rows:
            enrichment["email_classification_summary"] = cls_rows
    except Exception:
        pass

    for pname in person_names[:2]:
        try:
            ent_rows = _backend.execute_sql(
                f"SELECT name, entity_type, description"
                f" FROM {ENTITIES_TABLE if CORPUS != 'enron' else f'{CATALOG}.{ENRON_SCHEMA}.entities'}"
                f" WHERE LOWER(name) LIKE :pattern"
                f" LIMIT 3",
                params={"pattern": f"%{pname.lower()}%"},
            )
            if ent_rows:
                enrichment.setdefault("entity_context", {})[pname] = ent_rows
        except Exception:
            pass

    return json.dumps({
        "genie_result": genie_result,
        "enrichment": enrichment,
    }, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Investigative-trust tools — surface lineage, provenance, coverage, topics
# ---------------------------------------------------------------------------

@tool
def get_extraction_provenance(thread_id: str = "", entity_name: str = "") -> str:
    """Get extraction quality metadata for a thread or entity: which LLM model
    performed the extraction, whether input was truncated, entity resolution
    method and confidence. Use this to assess data reliability.

    Args:
        thread_id: A specific thread ID to check extraction quality for.
        entity_name: An entity name to check resolution audit and identity confidence.
    """
    if CORPUS != "enron":
        return "get_extraction_provenance is only available for the Enron corpus."

    result: dict = {}

    if thread_id:
        prov_table = f"{CATALOG}.{ENRON_SCHEMA}.extraction_provenance"
        try:
            rows = _backend.execute_sql(
                f"SELECT step, model_endpoint, prompt_template_version,"
                f" input_char_count, input_truncated_at,"
                f" output_entity_count, output_rel_count, error_message"
                f" FROM {prov_table}"
                f" WHERE thread_id = :tid",
                params={"tid": thread_id},
            )
            result["extraction_steps"] = rows if rows else []
            truncated = [r for r in (rows or []) if r.get("input_truncated_at")]
            if truncated:
                result["truncation_warning"] = (
                    f"{len(truncated)} extraction step(s) had truncated input — "
                    "some entities/relationships may be missing."
                )
        except Exception as exc:
            result["extraction_error"] = str(exc)

    if entity_name:
        patterns = _resolve_enron_entity_id(entity_name) if CORPUS == "enron" else [f"%{entity_name}%"]
        primary_pattern = patterns[0] if patterns else f"%{entity_name.lower()}%"

        audit_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_resolution_audit"
        try:
            rows = _backend.execute_sql(
                f"SELECT alias_id, canonical_id, method, blocking_reason,"
                f" confidence, ai_raw_response"
                f" FROM {audit_table}"
                f" WHERE LOWER(canonical_id) LIKE :pattern"
                f"    OR LOWER(alias_id) LIKE :pattern"
                f" LIMIT 10",
                params={"pattern": primary_pattern},
            )
            result["resolution_audit"] = rows if rows else []
        except Exception as exc:
            result["resolution_audit_error"] = str(exc)

        identity_table = f"{CATALOG}.{ENRON_SCHEMA}.person_identity"
        try:
            rows = _backend.execute_sql(
                f"SELECT entity_id, canonical_name, email_addresses, aliases,"
                f" source, confidence"
                f" FROM {identity_table}"
                f" WHERE LOWER(canonical_name) LIKE :pattern"
                f" LIMIT 5",
                params={"pattern": f"%{entity_name.lower()}%"},
            )
            result["identity"] = rows if rows else []
        except Exception as exc:
            result["identity_error"] = str(exc)

    if not result:
        return "Provide either thread_id or entity_name to check extraction provenance."

    return json.dumps(result, ensure_ascii=False, default=str)


_KNOWN_TABLES = {
    "emails", "threads", "participants", "entities", "relationships",
    "entity_mentions", "entity_analytics", "entity_paths", "entity_aliases",
    "communication_dyads", "person_activity", "org_hierarchy",
    "investigation_timeline", "extraction_provenance", "pipeline_lineage",
    "topic_taxonomy", "entity_resolution_audit", "corpus_coverage",
    "person_role_timeline", "person_identity", "email_classification",
    "data_quality_report", "ontology_registry",
}


def _extract_table_name(text: str) -> str:
    """Extract a table name from freeform text by matching known table names."""
    text_lower = text.lower().replace("-", "_").replace(" ", "_")
    for t in sorted(_KNOWN_TABLES, key=len, reverse=True):
        if t in text_lower:
            return t
    slug = re.sub(r"[^a-z0-9_]", "_", text_lower).strip("_")
    return slug if slug else text


@tool
def trace_data_lineage(table_name: str) -> str:
    """Trace how a table was derived through the data pipeline. Shows the
    upstream transformation chain from raw data to the target table.

    Args:
        table_name: The short table name (e.g., "communication_dyads", "entities").
                    Can also be a natural language reference — the tool will extract the table name.
    """
    if CORPUS != "enron":
        return "trace_data_lineage is only available for the Enron corpus."

    resolved_name = _extract_table_name(table_name) if table_name not in _KNOWN_TABLES else table_name

    lineage_table = f"{CATALOG}.{ENRON_SCHEMA}.pipeline_lineage"
    try:
        all_rows = _backend.execute_sql(
            f"SELECT source_table, target_table, transformation_step, sql_description"
            f" FROM {lineage_table}"
        )
    except Exception as exc:
        return f"Failed to query pipeline_lineage: {exc}"

    if not all_rows:
        return "No pipeline lineage data found."

    edges = {(r["source_table"], r["target_table"]): r for r in all_rows}

    chain = []
    visited = set()
    queue = [resolved_name]
    while queue and len(visited) < 20:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for (src, tgt), row in edges.items():
            if tgt == current:
                chain.append({
                    "source": src,
                    "target": tgt,
                    "step": row.get("transformation_step", ""),
                    "description": row.get("sql_description", ""),
                })
                queue.append(src)

    if not chain:
        return json.dumps({
            "table": resolved_name,
            "lineage": [],
            "note": f"No upstream lineage found for '{resolved_name}'. It may be a raw source table.",
        })

    chain.reverse()
    return json.dumps({
        "table": resolved_name,
        "lineage_depth": len(chain),
        "lineage": chain,
    }, ensure_ascii=False, default=str)


@tool
def browse_topics(category: str = "", entity_name: str = "") -> str:
    """Browse the hierarchical topic taxonomy extracted from email threads.
    Without arguments, lists parent categories with aggregate counts.
    With category, drills into sub-topics. With entity_name, shows topics
    associated with that entity.

    Args:
        category: A parent category to drill into (e.g., "Energy", "Legal", "Finance").
        entity_name: An entity name to find associated topics for.
    """
    if CORPUS != "enron":
        return "browse_topics is only available for the Enron corpus."

    taxonomy_table = f"{CATALOG}.{ENRON_SCHEMA}.topic_taxonomy"

    if entity_name:
        mentions_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"
        threads_table = f"{CATALOG}.{ENRON_SCHEMA}.threads"
        try:
            rows = _backend.execute_sql(
                f"SELECT tt.parent_label, tt.topic_label, COUNT(DISTINCT em.thread_id) AS thread_count"
                f" FROM {mentions_table} em"
                f" JOIN {threads_table} t ON em.thread_id = t.thread_id"
                f" JOIN {taxonomy_table} tt ON tt.level = 1"
                f"   AND LOWER(tt.topic_label) IN ("
                f"     SELECT LOWER(topic) FROM (SELECT EXPLODE(t.key_topics) AS topic)"
                f"   )"
                f" WHERE LOWER(em.entity_id) LIKE :pattern"
                f" GROUP BY tt.parent_label, tt.topic_label"
                f" ORDER BY thread_count DESC"
                f" LIMIT 20",
                params={"pattern": f"%{'_'.join(entity_name.lower().split())}%"},
            )
        except Exception:
            try:
                rows = _backend.execute_sql(
                    f"SELECT parent_label, topic_label, thread_count, entity_count"
                    f" FROM {taxonomy_table}"
                    f" WHERE level = 1"
                    f" ORDER BY entity_count DESC"
                    f" LIMIT 20"
                )
            except Exception as exc:
                return f"Topic query failed: {exc}"

        return json.dumps({
            "entity": entity_name,
            "topics": rows if rows else [],
        }, ensure_ascii=False, default=str)

    if category:
        try:
            rows = _backend.execute_sql(
                f"SELECT topic_id, topic_label, thread_count, entity_count"
                f" FROM {taxonomy_table}"
                f" WHERE level = 1 AND LOWER(parent_label) = LOWER(:cat)"
                f" ORDER BY thread_count DESC"
                f" LIMIT 30",
                params={"cat": category},
            )
        except Exception as exc:
            return f"Topic query failed: {exc}"

        return json.dumps({
            "category": category,
            "sub_topics": rows if rows else [],
        }, ensure_ascii=False, default=str)

    try:
        rows = _backend.execute_sql(
            f"SELECT topic_id, topic_label AS category, thread_count, entity_count"
            f" FROM {taxonomy_table}"
            f" WHERE level = 0"
            f" ORDER BY thread_count DESC"
        )
    except Exception as exc:
        return f"Topic query failed: {exc}"

    return json.dumps({
        "parent_categories": rows if rows else [],
        "hint": "Pass a category name to see sub-topics, or entity_name to find topics for a person.",
    }, ensure_ascii=False, default=str)


@tool
def get_topic_distribution(entity_name: str = "", limit: int = 20) -> str:
    """Get ranked topic distribution from email threads, optionally filtered by entity.
    Returns topics ranked by thread count with sample subjects.

    Args:
        entity_name: Optional entity name to filter topics for (e.g., "Kenneth Lay").
        limit: Maximum number of topics to return (default 20).
    """
    if CORPUS != "enron":
        return "get_topic_distribution is only available for the Enron corpus."

    threads_table = f"{CATALOG}.{ENRON_SCHEMA}.threads"
    mentions_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"

    if entity_name:
        pattern = f"%{'_'.join(entity_name.lower().split())}%"
        try:
            rows = _backend.execute_sql(
                f"SELECT topic, COUNT(DISTINCT t.thread_id) AS thread_count,"
                f" COLLECT_LIST(t.subject)[0] AS sample_subject"
                f" FROM {mentions_table} em"
                f" JOIN {threads_table} t ON em.thread_id = t.thread_id"
                f" LATERAL VIEW EXPLODE(t.key_topics) kt AS topic"
                f" WHERE LOWER(em.entity_id) LIKE :pattern"
                f" GROUP BY topic"
                f" ORDER BY thread_count DESC"
                f" LIMIT :lim",
                params={"pattern": pattern, "lim": limit},
            )
        except Exception:
            try:
                rows = _backend.execute_sql(
                    f"WITH exploded AS ("
                    f"  SELECT t.thread_id, t.subject, EXPLODE(t.key_topics) AS topic"
                    f"  FROM {mentions_table} em"
                    f"  JOIN {threads_table} t ON em.thread_id = t.thread_id"
                    f"  WHERE LOWER(em.entity_id) LIKE :pattern"
                    f")"
                    f" SELECT topic, COUNT(DISTINCT thread_id) AS thread_count,"
                    f"   FIRST(subject) AS sample_subject"
                    f" FROM exploded"
                    f" GROUP BY topic"
                    f" ORDER BY thread_count DESC"
                    f" LIMIT :lim",
                    params={"pattern": pattern, "lim": limit},
                )
            except Exception as exc:
                return f"Topic distribution query failed: {exc}"
    else:
        try:
            rows = _backend.execute_sql(
                f"SELECT topic, COUNT(DISTINCT thread_id) AS thread_count,"
                f" COLLECT_LIST(subject)[0] AS sample_subject"
                f" FROM {threads_table}"
                f" LATERAL VIEW EXPLODE(key_topics) kt AS topic"
                f" GROUP BY topic"
                f" ORDER BY thread_count DESC"
                f" LIMIT :lim",
                params={"lim": limit},
            )
        except Exception:
            try:
                rows = _backend.execute_sql(
                    f"WITH exploded AS ("
                    f"  SELECT thread_id, subject, EXPLODE(key_topics) AS topic"
                    f"  FROM {threads_table}"
                    f")"
                    f" SELECT topic, COUNT(DISTINCT thread_id) AS thread_count,"
                    f"   FIRST(subject) AS sample_subject"
                    f" FROM exploded"
                    f" GROUP BY topic"
                    f" ORDER BY thread_count DESC"
                    f" LIMIT :lim",
                    params={"lim": limit},
                )
            except Exception as exc:
                return f"Topic distribution query failed: {exc}"

    return json.dumps({
        "entity": entity_name or "all",
        "topic_count": len(rows) if rows else 0,
        "topics": rows if rows else [],
    }, ensure_ascii=False, default=str)


@tool
def get_communication_stats(entity_name: str = "", group_by: str = "contact", limit: int = 20) -> str:
    """Get communication volume statistics: top contacts, sent/received ratio, monthly trends.

    Args:
        entity_name: Entity name to get stats for (e.g., "Kenneth Lay").
        group_by: How to aggregate — "contact" (top contacts), "month" (monthly trend), "direction" (sent vs received).
        limit: Maximum rows to return (default 20).
    """
    if CORPUS != "enron":
        return "get_communication_stats is only available for the Enron corpus."

    dyads_table = f"{CATALOG}.{ENRON_SCHEMA}.communication_dyads"
    activity_table = f"{CATALOG}.{ENRON_SCHEMA}.person_activity"

    if not entity_name:
        try:
            rows = _backend.execute_sql(
                f"SELECT display_name, total_sent, total_received,"
                f" total_sent + total_received AS total_volume,"
                f" ROUND(total_sent * 100.0 / NULLIF(total_sent + total_received, 0), 1) AS sent_pct"
                f" FROM {activity_table}"
                f" ORDER BY total_sent + total_received DESC"
                f" LIMIT :lim",
                params={"lim": limit},
            )
        except Exception as exc:
            return f"Communication stats query failed: {exc}"
        return json.dumps({
            "group_by": "top_senders",
            "stats": rows if rows else [],
        }, ensure_ascii=False, default=str)

    pattern = f"%{entity_name.lower()}%"

    if group_by == "contact":
        try:
            rows = _backend.execute_sql(
                f"SELECT contact_name, email_count, direction,"
                f" first_email_date, last_email_date"
                f" FROM {dyads_table}"
                f" WHERE LOWER(display_name) LIKE :pattern"
                f" ORDER BY email_count DESC"
                f" LIMIT :lim",
                params={"pattern": pattern, "lim": limit},
            )
        except Exception as exc:
            return f"Communication stats query failed: {exc}"
        return json.dumps({
            "entity": entity_name,
            "group_by": "contact",
            "contacts": rows if rows else [],
        }, ensure_ascii=False, default=str)

    elif group_by == "month":
        try:
            rows = _backend.execute_sql(
                f"SELECT DATE_FORMAT(first_email_date, 'yyyy-MM') AS month,"
                f" SUM(email_count) AS total_emails,"
                f" COUNT(DISTINCT contact_name) AS unique_contacts"
                f" FROM {dyads_table}"
                f" WHERE LOWER(display_name) LIKE :pattern"
                f" GROUP BY DATE_FORMAT(first_email_date, 'yyyy-MM')"
                f" ORDER BY month",
                params={"pattern": pattern},
            )
        except Exception:
            try:
                rows = _backend.execute_sql(
                    f"SELECT SUBSTR(CAST(first_email_date AS STRING), 1, 7) AS month,"
                    f" SUM(email_count) AS total_emails,"
                    f" COUNT(DISTINCT contact_name) AS unique_contacts"
                    f" FROM {dyads_table}"
                    f" WHERE LOWER(display_name) LIKE :pattern"
                    f" GROUP BY SUBSTR(CAST(first_email_date AS STRING), 1, 7)"
                    f" ORDER BY month",
                    params={"pattern": pattern},
                )
            except Exception as exc:
                return f"Communication stats query failed: {exc}"
        return json.dumps({
            "entity": entity_name,
            "group_by": "month",
            "monthly_trend": rows if rows else [],
        }, ensure_ascii=False, default=str)

    else:
        try:
            rows = _backend.execute_sql(
                f"SELECT display_name, total_sent, total_received,"
                f" total_sent + total_received AS total_volume,"
                f" ROUND(total_sent * 100.0 / NULLIF(total_sent + total_received, 0), 1) AS sent_pct"
                f" FROM {activity_table}"
                f" WHERE LOWER(display_name) LIKE :pattern",
                params={"pattern": pattern},
            )
        except Exception as exc:
            return f"Communication stats query failed: {exc}"
        return json.dumps({
            "entity": entity_name,
            "group_by": "direction",
            "activity": rows if rows else [],
        }, ensure_ascii=False, default=str)


@tool
def get_entity_context(entity_name: str) -> str:
    """Get comprehensive context for an entity: summary, org position, top contacts, and topics.
    Bundles 4 lookups into one call for richer context.

    Args:
        entity_name: The entity name to get full context for.
    """
    result = {}

    summary_raw = get_entity_summary.invoke({"entity_name": entity_name})
    try:
        result["summary"] = json.loads(summary_raw)
    except (json.JSONDecodeError, TypeError):
        result["summary"] = summary_raw

    if CORPUS == "enron":
        org_raw = query_org_hierarchy.invoke({"entity_name": entity_name})
        try:
            result["org_position"] = json.loads(org_raw)
        except (json.JSONDecodeError, TypeError):
            result["org_position"] = org_raw

        contacts_raw = find_top_contacts.invoke({"entity_name": entity_name, "direction": "both", "limit": 5})
        try:
            result["top_contacts"] = json.loads(contacts_raw)
        except (json.JSONDecodeError, TypeError):
            result["top_contacts"] = contacts_raw

        topics_raw = get_topic_distribution.invoke({"entity_name": entity_name, "limit": 10})
        try:
            result["topics"] = json.loads(topics_raw)
        except (json.JSONDecodeError, TypeError):
            result["topics"] = topics_raw

    return json.dumps(result, ensure_ascii=False, default=str)


@tool
def get_corpus_coverage(entity_name: str = "") -> str:
    """Get corpus coverage statistics and data quality context. Shows extraction
    rates, relationship density, and coverage gaps. When given an entity, shows
    coverage context specific to that entity's data.

    Args:
        entity_name: Optional entity name for entity-specific coverage context.
    """
    if CORPUS != "enron":
        return "get_corpus_coverage is only available for the Enron corpus."

    coverage_table = f"{CATALOG}.{ENRON_SCHEMA}.corpus_coverage"
    result: dict = {}

    try:
        rows = _backend.execute_sql(
            f"SELECT metric_name, metric_value, denominator, coverage_pct"
            f" FROM {coverage_table}"
        )
        result["corpus_metrics"] = rows if rows else []
        low_coverage = [r for r in (rows or []) if (r.get("coverage_pct") or 100) < 80]
        if low_coverage:
            result["coverage_warnings"] = [
                f"{r['metric_name']}: {r.get('coverage_pct', 0):.1f}% "
                f"({r.get('metric_value', 0)}/{r.get('denominator', 0)})"
                for r in low_coverage
            ]
    except Exception as exc:
        result["coverage_error"] = str(exc)

    if entity_name:
        activity_table = f"{CATALOG}.{ENRON_SCHEMA}.person_activity"
        try:
            rows = _backend.execute_sql(
                f"SELECT display_name, total_sent, total_received"
                f" FROM {activity_table}"
                f" WHERE LOWER(display_name) LIKE :pattern"
                f" LIMIT 3",
                params={"pattern": f"%{entity_name.lower()}%"},
            )
            result["entity_activity"] = rows if rows else []
        except Exception:
            pass

        classification_table = f"{CATALOG}.{ENRON_SCHEMA}.email_classification"
        emails_table = f"{CATALOG}.{ENRON_SCHEMA}.emails"
        try:
            rows = _backend.execute_sql(
                f"SELECT ec.email_type, COUNT(*) AS cnt,"
                f" ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) AS pct"
                f" FROM {classification_table} ec"
                f" JOIN {emails_table} e ON ec.message_id = e.message_id"
                f" WHERE LOWER(e.sender) LIKE :pattern"
                f" GROUP BY ec.email_type"
                f" ORDER BY cnt DESC",
                params={"pattern": f"%{entity_name.lower()}%"},
            )
            result["email_type_breakdown"] = rows if rows else []
        except Exception:
            pass

        prov_table = f"{CATALOG}.{ENRON_SCHEMA}.extraction_provenance"
        try:
            rows = _backend.execute_sql(
                f"SELECT COUNT(*) AS total_threads,"
                f" SUM(CASE WHEN ep.input_truncated_at IS NOT NULL THEN 1 ELSE 0 END) AS truncated_threads"
                f" FROM {prov_table} ep"
                f" JOIN {CATALOG}.{ENRON_SCHEMA}.entity_mentions em"
                f"   ON ep.thread_id = em.thread_id"
                f" WHERE ep.step = 'entity_extraction'"
                f"   AND LOWER(em.entity_id) LIKE :pattern",
                params={"pattern": f"%{'_'.join(entity_name.lower().split())}%"},
            )
            if rows and rows[0].get("total_threads"):
                total = rows[0]["total_threads"]
                trunc = rows[0].get("truncated_threads", 0)
                result["extraction_quality"] = {
                    "total_threads": total,
                    "truncated_threads": trunc,
                    "truncation_rate_pct": round(100.0 * trunc / total, 1) if total else 0,
                }
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False, default=str)


@tool
def find_emails(
    person_a: str = "",
    person_b: str = "",
    keywords: str = "",
    date_from: str = "",
    date_to: str = "",
    hour_from: int = -1,
    hour_to: int = -1,
    limit: int = 15,
) -> str:
    """Unified email search: find emails by people, keywords, date range, and/or time of day.
    Combines person-to-person lookup, keyword search, and body-mention search in one tool.

    Args:
        person_a: Optional first person name or email (sender or participant).
        person_b: Optional second person. When both given, finds emails between the two.
        keywords: Optional comma-separated keywords (OR-matched against subject and body).
        date_from: Optional start date (YYYY-MM-DD).
        date_to: Optional end date (YYYY-MM-DD).
        hour_from: Optional hour (0-23) — only include emails sent at or after this hour. Use 18 for "after 6pm".
        hour_to: Optional hour (0-23) — only include emails sent at or before this hour.
        limit: Max results (default 15).
    """
    if CORPUS != "enron":
        return "find_emails is only available for the Enron corpus."

    if person_a and person_b and not keywords:
        if hour_from < 0 and hour_to < 0 and not date_from and not date_to:
            return get_emails_between(entity_a=person_a, entity_b=person_b, limit=limit)

    cfg = _get_corpus_config()
    source_table = cfg["source_table"]

    where_parts: list[str] = []
    params: dict = {}

    if person_a:
        pats_a = _resolve_name_to_email(person_a)
        if pats_a:
            if person_b:
                pats_b = _resolve_name_to_email(person_b)
                if pats_b:
                    where_parts.append(
                        "("
                        "(LOWER(sender) LIKE :pa AND ("
                        "LOWER(CAST(to_recipients AS STRING)) LIKE :pb"
                        " OR LOWER(CAST(cc_recipients AS STRING)) LIKE :pb))"
                        " OR "
                        "(LOWER(sender) LIKE :pb AND ("
                        "LOWER(CAST(to_recipients AS STRING)) LIKE :pa"
                        " OR LOWER(CAST(cc_recipients AS STRING)) LIKE :pa))"
                        ")"
                    )
                    params["pa"] = pats_a[0]
                    params["pb"] = pats_b[0]
            else:
                where_parts.append(
                    "(LOWER(sender) LIKE :pa"
                    " OR LOWER(CAST(to_recipients AS STRING)) LIKE :pa"
                    " OR LOWER(body) LIKE :pa)"
                )
                params["pa"] = pats_a[0]

    if keywords:
        kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
        kw_conds = []
        for i, kw in enumerate(kw_list):
            pk = f"kw{i}"
            kw_conds.append(f"(LOWER(subject) LIKE :{pk} OR LOWER(body) LIKE :{pk})")
            params[pk] = f"%{kw}%"
        if kw_conds:
            where_parts.append(f"({' OR '.join(kw_conds)})")

    if date_from:
        where_parts.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where_parts.append("date <= :date_to")
        params["date_to"] = date_to
    if hour_from >= 0:
        where_parts.append("HOUR(date) >= :hour_from")
        params["hour_from"] = hour_from
    if hour_to >= 0:
        where_parts.append("HOUR(date) <= :hour_to")
        params["hour_to"] = hour_to

    if not where_parts:
        return "No search criteria provided. Specify at least person_a, keywords, or a date range."

    where_clause = " AND ".join(where_parts)
    sql = (
        f"SELECT date, sender, subject,"
        f" SUBSTR(body, 1, 400) AS body_preview"
        f" FROM {source_table}"
        f" WHERE {where_clause}"
        f" ORDER BY date DESC"
        f" LIMIT {int(limit)}"
    )

    try:
        results = _backend.execute_sql(sql, params=params)
    except Exception as exc:
        log.warning("find_emails query failed: %s", exc)
        return f"Email search failed: {exc}"

    if not results:
        return f"No emails found matching the given criteria."

    emails = []
    for r in results:
        emails.append({
            "date": str(r.get("date", "")),
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": r.get("body_preview", ""),
        })

    return json.dumps({
        "filters": {
            "person_a": person_a or None, "person_b": person_b or None,
            "keywords": keywords or None,
            "date_from": date_from or None, "date_to": date_to or None,
            "hour_from": hour_from if hour_from >= 0 else None,
            "hour_to": hour_to if hour_to >= 0 else None,
        },
        "total": len(emails),
        "emails": emails,
    }, ensure_ascii=False)


@tool
def query_org_hierarchy(entity_name: str) -> str:
    """Query the curated Enron organizational hierarchy for reporting relationships.
    Returns who the person reports to AND who reports to them, with titles, departments,
    and temporal validity.

    This table contains verified data from SEC filings, DOJ prosecution records,
    and congressional testimony — it is more reliable than LLM-extracted relationships.

    Args:
        entity_name: Person name to look up (e.g., "Jeff Skilling", "Andrew Fastow")
    """
    if CORPUS != "enron":
        return "Org hierarchy is only available for the Enron corpus."

    pattern = f"%{entity_name.lower().replace(' ', '_')}%"

    try:
        reports_to = _backend.execute_sql(
            f"SELECT person_id, name, title, department, reports_to_id, "
            f"effective_from, effective_to, source "
            f"FROM {ENRON_ORG_HIERARCHY_TABLE} "
            f"WHERE LOWER(person_id) LIKE :pattern OR LOWER(name) LIKE :name_pattern "
            f"ORDER BY effective_from",
            params={"pattern": pattern, "name_pattern": f"%{entity_name.lower()}%"},
        )

        subordinates = _backend.execute_sql(
            f"SELECT person_id, name, title, department, reports_to_id, "
            f"effective_from, effective_to, source "
            f"FROM {ENRON_ORG_HIERARCHY_TABLE} "
            f"WHERE LOWER(reports_to_id) LIKE :pattern "
            f"ORDER BY effective_from",
            params={"pattern": pattern},
        )
    except Exception as exc:
        log.warning("Org hierarchy query failed: %s", exc)
        return "Org hierarchy table is not available."

    def _fmt(row):
        return {
            "person_id": row["person_id"],
            "name": row["name"],
            "title": row["title"],
            "department": row["department"],
            "reports_to_id": row.get("reports_to_id"),
            "effective_from": str(row.get("effective_from", "")),
            "effective_to": str(row.get("effective_to", "")),
            "source": row.get("source", "curated"),
        }

    return json.dumps({
        "entity": entity_name,
        "source": "curated_org_hierarchy",
        "roles": [_fmt(r) for r in reports_to],
        "direct_reports": [_fmt(r) for r in subordinates],
    }, ensure_ascii=False)


LOCAL_TOOLS = [find_entity, find_connections, find_top_contacts, get_top_email_pairs,
               get_top_individuals, get_emails_between, get_dyad_topics,
               get_relationship_evidence, get_source_evidence, get_entity_summary,
               list_entities_by_book, find_cross_book_entities, trace_path, compare_entity_sets,
               query_timeline, query_org_hierarchy,
               detect_self_emails, get_external_contacts, get_communication_timeline,
               get_activity_anomalies, search_emails, semantic_search_emails,
               find_emails, query_and_enrich,
               get_extraction_provenance, trace_data_lineage, browse_topics,
               get_topic_distribution, get_communication_stats,
               get_entity_context, get_corpus_coverage]


def build_scoped_tools_local(permitted_books: list):
    """Create graph tools scoped to a specific document set using the local backend.

    Unlike build_scoped_tools in tools.py (Spark-dependent), this version works
    with the agent_serving backend abstraction (DuckDB locally, Statement Execution
    API on Databricks).
    """
    books_csv = ", ".join(f"'{b}'" for b in permitted_books)
    book_filter_rel = f" AND r.book IN ({books_csv})"
    book_filter_v = f" AND v.book IN ({books_csv})"

    @tool
    def find_entity(name: str) -> str:
        """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.

        Args:
            name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
        """
        search_names = [name] + _get_alias_names(name)
        results = []
        for search_name in search_names:
            results = _backend.execute_sql(
                f"SELECT DISTINCT e.name, e.entity_type, e.description, e.first_mention_book, e.first_mention_chapter"
                f" FROM {ENTITIES_TABLE} e"
                f" JOIN {RELATIONSHIPS_TABLE} r ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
                f" WHERE LOWER(e.name) LIKE LOWER(:name_pattern){book_filter_rel}"
                " ORDER BY e.name LIMIT 10",
                params={"name_pattern": f"%{search_name}%"},
            )
            if results:
                break

        if not results:
            for search_name in search_names:
                verse_hits = _backend.execute_sql(
                    f"SELECT DISTINCT e.name, e.entity_type, e.description,"
                    f" e.first_mention_book, e.first_mention_chapter"
                    f" FROM {ENTITIES_TABLE} e"
                    f" WHERE EXISTS ("
                    f"   SELECT 1 FROM {VERSES_TABLE} v"
                    f"   WHERE v.text LIKE :name_pattern{book_filter_v}"
                    f"   AND LOWER(e.name) LIKE LOWER(:entity_pattern)"
                    f" )"
                    " ORDER BY e.name LIMIT 10",
                    params={"name_pattern": f"%{search_name}%", "entity_pattern": f"%{search_name}%"},
                )
                if verse_hits:
                    results = verse_hits
                    break

        if not results:
            return f"No entity found matching '{name}' in permitted books."
        entities = [{
            "name": r["name"], "type": r["entity_type"],
            "description": r["description"],
            "first_mention": f"{r['first_mention_book']} ch.{r['first_mention_chapter']}",
        } for r in results]
        return json.dumps(entities, ensure_ascii=False)

    @tool
    def find_connections(entity_name: str, book: str = "") -> str:
        """Find all relationships involving a given entity — both as source and target.

        Args:
            entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
            book: Optional — filter to a specific book (e.g., "Exodus"). Leave empty for all permitted books.
        """
        entity_id = "_".join(entity_name.lower().split())
        eid_pattern = f"%{entity_id}%"
        sql_params = {"eid_pattern": eid_pattern}
        extra_filter = book_filter_rel
        if book:
            if book not in permitted_books:
                return f"Book '{book}' is not in your permitted document set."
            extra_filter = f" AND r.book = :book"
            sql_params["book"] = book
        results = _backend.execute_sql(
            f"SELECT COALESCE(e1.name, r.source_entity) as source_name,"
            f" r.relationship_type, COALESCE(e2.name, r.target_entity) as target_name,"
            f" r.description, r.book, r.chapter"
            f" FROM {RELATIONSHIPS_TABLE} r"
            f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            f" WHERE (r.source_entity LIKE :eid_pattern OR r.target_entity LIKE :eid_pattern){extra_filter}"
            " ORDER BY r.book, r.chapter LIMIT 100",
            params=sql_params,
        )
        if not results:
            suffix = f" in {book}" if book else ""
            return f"No connections found for '{entity_name}'{suffix}."
        return json.dumps(
            _group_connections(entity_name, results, corpus="bible"),
            ensure_ascii=False,
        )

    @tool
    def get_source_evidence(entity_name: str, book: str = "") -> str:
        """Get actual Bible verses that mention a specific entity.

        Args:
            entity_name: The entity name to find verses for (e.g., "Moses")
            book: Optional — filter to a specific book. Leave empty for all permitted books.
        """
        sql_params = {"name_pattern": f"%{entity_name}%"}
        extra_filter = book_filter_v
        if book:
            if book not in permitted_books:
                return f"Book '{book}' is not in your permitted document set."
            extra_filter = f" AND v.book = :book"
            sql_params["book"] = book
        results = _backend.execute_sql(
            f"SELECT v.book, v.chapter, v.verse_number, v.text FROM {VERSES_TABLE} v"
            f" WHERE v.text LIKE :name_pattern{extra_filter}"
            " ORDER BY v.book, v.chapter, v.verse_number LIMIT 30",
            params=sql_params,
        )
        if not results:
            return f"No verses found mentioning '{entity_name}' in permitted books."
        verses = [{
            "reference": f"{r['book']} {r['chapter']}:{r['verse_number']}",
            "text": r["text"],
        } for r in results]
        return json.dumps({"entity": entity_name, "total": len(verses), "verses": verses}, ensure_ascii=False)

    @tool
    def get_entity_summary(entity_name: str) -> str:
        """Get a comprehensive profile of a biblical entity within the permitted document set.

        Args:
            entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
        """
        entity_id = "_".join(entity_name.lower().split())
        eid_params = {"eid_pattern": f"%{entity_id}%"}
        entity_rows = _backend.execute_sql(
            f"SELECT DISTINCT e.name, e.entity_type, e.description, e.first_mention_book, e.first_mention_chapter"
            f" FROM {ENTITIES_TABLE} e"
            f" JOIN {RELATIONSHIPS_TABLE} r ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
            f" WHERE e.entity_id LIKE :eid_pattern{book_filter_rel}"
            " LIMIT 1",
            params=eid_params,
        )
        if not entity_rows:
            entity_rows = _backend.execute_sql(
                f"SELECT DISTINCT e.name, e.entity_type, e.description,"
                f" e.first_mention_book, e.first_mention_chapter"
                f" FROM {ENTITIES_TABLE} e"
                f" WHERE e.entity_id LIKE :eid_pattern"
                f" AND EXISTS (SELECT 1 FROM {VERSES_TABLE} v"
                f"   WHERE v.text LIKE :name_pattern{book_filter_v})"
                " LIMIT 1",
                params={"eid_pattern": f"%{entity_id}%", "name_pattern": f"%{entity_name}%"},
            )
        if not entity_rows:
            return f"Entity '{entity_name}' not found in the permitted document set."
        ent = entity_rows[0]
        summary = {
            "name": ent["name"], "type": ent["entity_type"],
            "description": ent["description"],
            "first_mention": f"{ent['first_mention_book']} ch.{ent['first_mention_chapter']}",
        }
        rels = _backend.execute_sql(
            f"SELECT COALESCE(e1.name, r.source_entity) as src,"
            f" r.relationship_type, COALESCE(e2.name, r.target_entity) as tgt,"
            f" r.description, r.book"
            f" FROM {RELATIONSHIPS_TABLE} r"
            f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            f" WHERE (r.source_entity LIKE :eid_pattern OR r.target_entity LIKE :eid_pattern){book_filter_rel}"
            " LIMIT 50",
            params=eid_params,
        )
        if rels:
            from collections import defaultdict
            books_seen = set(r["book"] for r in rels)
            summary["appears_in"] = sorted(books_seen)
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in rels:
                groups[r["relationship_type"]].append({
                    "source": r["src"], "target": r["tgt"], "description": r["description"],
                })
            summary["relationships"] = {"total": len(rels), "by_type": dict(groups)}
        return json.dumps(summary, ensure_ascii=False)

    @tool
    def list_entities_by_book(book: str, entity_type: str = "") -> str:
        """List all named entities in a specific book.

        Args:
            book: The book name (must be in permitted set)
            entity_type: Optional — filter by type
        """
        if book not in permitted_books:
            return f"Book '{book}' is not in your permitted document set."
        sql_params = {"book": book}
        type_filter = ""
        if entity_type:
            type_filter = " AND e.entity_type = :entity_type"
            sql_params["entity_type"] = entity_type
        results = _backend.execute_sql(
            f"SELECT DISTINCT e.name, e.entity_type, e.description"
            f" FROM {ENTITIES_TABLE} e"
            f" JOIN {RELATIONSHIPS_TABLE} r ON (e.entity_id = r.source_entity OR e.entity_id = r.target_entity)"
            f" WHERE r.book = :book{type_filter}"
            " ORDER BY e.entity_type, e.name",
            params=sql_params,
        )
        if not results:
            return f"No entities found in '{book}'."
        return json.dumps(
            _group_entities_by_type(results, book=book), ensure_ascii=False,
        )

    @tool
    def compare_entity_sets(
        entity_name: str = "",
        book_a: str = "",
        book_b: str = "",
        rel_type_a: str = "",
        rel_type_b: str = "",
        operation: str = "difference",
    ) -> str:
        """Compare two sets of entities using set operations (difference, intersection, union).

        Args:
            entity_name: Optional central entity (e.g., "Moses").
            book_a: Book filter for set A (must be in permitted set).
            book_b: Book filter for set B (must be in permitted set).
            rel_type_a: Relationship type filter for set A.
            rel_type_b: Relationship type filter for set B.
            operation: "difference" (A-B), "intersection" (A&B), or "union" (A|B).
        """
        for bk in [book_a, book_b]:
            if bk and bk not in permitted_books:
                return f"Book '{bk}' is not in your permitted document set."

        def _query_set(book, rel_type, tag):
            conditions = [f"r.book IN ({books_csv})"]
            params = {}
            if entity_name:
                eid = "_".join(entity_name.lower().split())
                conditions.append(f"(r.source_entity LIKE :eid_{tag} OR r.target_entity LIKE :eid_{tag})")
                params[f"eid_{tag}"] = f"%{eid}%"
            if book:
                conditions = [f"r.book = :book_{tag}"]
                params[f"book_{tag}"] = book
            if rel_type:
                conditions.append(f"r.relationship_type = :rt_{tag}")
                params[f"rt_{tag}"] = rel_type
            where = " AND ".join(conditions)
            rows = _backend.execute_sql(
                f"SELECT DISTINCT COALESCE(e2.name, r.target_entity) AS neighbor"
                f" FROM {RELATIONSHIPS_TABLE} r"
                f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
                f" WHERE {where}",
                params=params,
            )
            return {r["neighbor"] for r in rows if r["neighbor"]}

        names_a = _query_set(book_a, rel_type_a, "a")
        names_b = _query_set(book_b, rel_type_b, "b")
        if entity_name:
            names_a.discard(entity_name)
            names_b.discard(entity_name)

        op = operation.lower()
        if op == "difference":
            result_set = sorted(names_a - names_b)
            op_label = "A \\ B"
        elif op == "intersection":
            result_set = sorted(names_a & names_b)
            op_label = "A ∩ B"
        elif op == "union":
            result_set = sorted(names_a | names_b)
            op_label = "A ∪ B"
        else:
            return f"Unknown operation '{operation}'."

        desc_a = " + ".join(filter(None, [entity_name, rel_type_a, book_a])) or "(all permitted)"
        desc_b = " + ".join(filter(None, [entity_name, rel_type_b, book_b])) or "(all permitted)"
        return json.dumps({
            "set_a": {"description": desc_a, "count": len(names_a)},
            "set_b": {"description": desc_b, "count": len(names_b)},
            "operation": op_label,
            "result_count": len(result_set),
            "result": result_set,
        }, ensure_ascii=False)

    return [find_entity, find_connections, get_source_evidence, get_entity_summary,
            list_entities_by_book, compare_entity_sets]


# ---------------------------------------------------------------------------
# MCP tool discovery — GraphFrames MCP server
# ---------------------------------------------------------------------------
def _wrap_mcp_tools(client, mcp_tools) -> list:
    """Convert MCP tool descriptors into LangChain @tool-compatible callables."""
    wrapped = []
    for t in mcp_tools:
        tool_name = t.name
        tool_desc = t.description or tool_name

        def _make_caller(_name, _client):
            def _call(**kwargs) -> str:
                result = _client.call_tool(_name, kwargs)
                if hasattr(result, "content") and result.content:
                    return "\n".join(
                        c.text if hasattr(c, "text") else str(c)
                        for c in result.content
                    )
                return str(result)
            return _call

        caller = _make_caller(tool_name, client)
        lc_tool = tool(caller, name=tool_name, description=tool_desc)
        wrapped.append(lc_tool)
    return wrapped


def _get_mcp_tools() -> list:
    """Discover graph analytics tools from the GraphFrames MCP server.

    Returns an empty list if the MCP server is unavailable or in local mode.
    """
    if BACKEND_TYPE == "local":
        log.info("Local backend — MCP graph analytics tools disabled")
        return []
    try:
        from databricks_mcp import DatabricksMCPClient
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        url = os.environ.get(
            "GRAPHFRAMES_MCP_URL",
            f"{w.config.host}/api/2.0/mcp/external/graphframes_connection",
        )
        client = DatabricksMCPClient(server_url=url, workspace_client=w)
        mcp_tools = client.list_tools()
        wrapped = _wrap_mcp_tools(client, mcp_tools)
        log.info("Discovered %d MCP tools from GraphFrames server", len(wrapped))
        return wrapped
    except Exception:
        log.warning("GraphFrames MCP server unavailable; graph analytics tools disabled")
        return []


GRAPH_TOOLS = LOCAL_TOOLS + _get_mcp_tools()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a biblical scholar with access to a knowledge graph built from five books of the King James Bible: Genesis, Exodus, Ruth, Matthew, and Acts.

You have tools that let you search the knowledge graph for entities, relationships, source verses, and structural analysis. Use them to provide well-grounded, comprehensive answers.

## Available Tools
- **find_entity(name)** — search for an entity by name (automatically checks KJV spelling variants)
- **find_connections(entity_name, book="", relationship_type="")** — find relationships for an entity, optionally filtered by book and/or relationship type
- **get_source_evidence(entity_name, book="")** — retrieve actual Bible verses mentioning an entity
- **get_entity_summary(entity_name)** — get a comprehensive entity profile with all relationships
- **list_entities_by_book(book, entity_type="")** — list all entities in a specific book, optionally by type
- **find_cross_book_entities(min_books=2)** — find entities appearing across multiple books
- **trace_path(entity_a, entity_b, max_hops=5)** — find shortest path between two entities via relationship traversal
- **compare_entity_sets(entity_name, book_a, book_b, rel_type_a, rel_type_b, operation)** — compare two entity sets using difference/intersection/union

## Tool Usage Strategy
- ALWAYS use tools before answering. Prefer graph data over training knowledge.
- For enumeration questions ("list all people in Ruth"), use **list_entities_by_book**.
- For cross-book questions ("who appears in multiple books"), use **find_cross_book_entities**.
- For entity-specific questions, use **find_connections** with the **book** filter to get targeted results.
- For broad entity questions, use **get_entity_summary** for a full profile.
- For multi-entity or multi-book questions, **call tools multiple times** — once per entity or per book — to build a complete picture. Do not rely on a single tool call.
- After gathering entity/relationship data, call **get_source_evidence** for key claims you want to ground with verse references.
- For constraint/set-difference questions ("X but not Y", "in book A but not B"), use **compare_entity_sets** with the appropriate operation. Example: "Who did Moses COMMAND but not SPOKE_TO?" → `compare_entity_sets(entity_name="Moses", rel_type_a="COMMANDED", rel_type_b="SPOKE_TO", operation="difference")`.
- For intersection questions ("entities connected to BOTH X and Y"), use **compare_entity_sets** with `operation="intersection"`.
- For shortest-path questions ("How is Ruth connected to Jesus?"), use **trace_path** to find the path automatically.
- For long genealogy chains (e.g., Abraham to Jesus in Matthew ch.1), use **trace_path** first, then call **find_connections** on intermediate entities for detailed relationship data.
- The KJV uses archaic spellings (Elias for Elijah, Esaias for Isaiah). The find_entity tool checks variants automatically, but if a search returns nothing, try the KJV spelling explicitly.

## Response Guidelines
- **Be direct and comprehensive.** Answer the question fully. Do not restate the question.
- **Prioritize completeness.** Include all relevant findings from the tools. If a tool returns many results, summarize the key ones.
- **Cite sources inline** where natural (e.g., "Ruth 4:17" or "Genesis 12:1"), but do not force citations for every sentence.
- **State coverage limitations** when relevant: "My knowledge graph covers Genesis, Exodus, Ruth, Matthew, and Acts."
- If information is not in the knowledge graph, say so honestly rather than guessing.

## Entity Pre-Lookup
Before you received this message, entities from the user's question were automatically looked up in the knowledge graph. Results appear at the END of this system prompt.
- If an entity is listed under "NOT IN GRAPH" and it is the primary subject, state that it is not available and answer based on what IS in the graph.
- Scope terms like "Old Testament", "New Testament", "the Bible" are NOT entity names — ignore if they appear under NOT IN GRAPH.
- Do NOT bridge graph entities to non-graph concepts using training data (e.g., linking Ishmael to "Arabs" when "Arabs" is not in the graph)."""


ENRON_SYSTEM_PROMPT = """You are a corporate communications analyst with access to a knowledge graph built from the Enron email corpus (~20,000 emails from key executives and employees, 2000-2002).

You have tools that let you search the knowledge graph for entities, relationships, source emails, and structural analysis. Use them to provide well-grounded, auditable answers about organizational structure, communication patterns, and corporate activities.

## Available Tools

### Graph & Communication Tools
- **find_entity(name)** — search for a person, organization, project, or event by name. For Enron Person results, also returns email addresses.
- **find_connections(entity_name, relationship_type="")** — find relationships for an entity, optionally filtered by type (REPORTS_TO, MANAGES, SENT_TO, DISCUSSES, COLLABORATES_WITH, etc.). Returns `evidence_count`, `first_observed`, `last_observed`, and `confidence` per relationship.
- **find_top_contacts(entity_name, direction, limit)** — ranked list of who communicated most with an entity (sent/received/total counts). Automatically deduplicates aliased entities.
- **get_top_email_pairs(limit)** — corpus-wide ranking of the pairs of people who exchanged the most emails. Returns `is_self_email` flag for pairs that are the same person emailing themselves across domains.
- **get_emails_between(entity_a, entity_b)** — retrieve emails between two people. Check `match_type`: "header" = direct, "body_mention" = both mentioned in same email.
- **find_emails(person_a="", person_b="", keywords="", date_from="", date_to="", hour_from=-1, hour_to=-1, limit=15)** — unified email search: find emails by people, keywords, date range, and/or time of day. Use `hour_from=18` for after-hours emails, `hour_to=8` for early morning. Replaces separate search_emails/get_emails_between/get_source_evidence for flexible queries.
- **get_dyad_topics(entity_a, entity_b)** — discussion topics between two people using AI-generated thread summaries.
- **get_relationship_evidence(source_entity, target_entity, relationship_type="")** — retrieve original emails where a graph relationship was extracted from.
- **get_source_evidence(entity_name)** — find emails mentioning an entity in the body text; supports 'A AND B' syntax.
- **get_entity_summary(entity_name)** — comprehensive entity profile: relationships, and for Enron Person entities: email addresses, title, department, graph centrality (pagerank, degree).
- **trace_path(entity_a, entity_b)** — find shortest path between two entities via relationship traversal.
- **query_timeline(person_name="", date_from="", date_to="", category="")** — query curated Enron investigation timeline for key events.

### Investigative Analysis Tools
- **detect_self_emails(limit)** — find people who emailed their own personal accounts from corporate email.
- **get_external_contacts(entity_name="", direction="both", limit)** — who communicated most with non-Enron addresses.
- **get_communication_timeline(entity_name="", entity_b="", date_from="", date_to="")** — weekly time-series email volume.
- **get_activity_anomalies(entity_name="", metric="all", limit)** — surface unusual behavioral patterns: BCC-heavy, after-hours, weekend, volume spikes.
- **search_emails(keywords, date_from="", date_to="", sender="", limit)** — keyword search across email subject and body.

## Tool Usage Strategy
- ALWAYS use tools before answering. Prefer graph data over training knowledge.
- For questions about people, use **find_entity** first, then **get_entity_summary** for their full profile.
- For organizational hierarchy questions ("who reported to X?", "who managed Y?"), use **find_connections** with `relationship_type="REPORTS_TO"` and/or `relationship_type="MANAGES"` to get focused results. Note edge direction: in REPORTS_TO, source reports to target; in MANAGES, source manages target.
- For "who communicated most with X?" questions, use **find_top_contacts** — it returns a ranked list with sent/received counts.
- For COMPARISON questions ("who sent more emails, X or Y?"): call **find_top_contacts** separately for EACH person, then compare their totals. Do NOT only look up one person.
- For GLOBAL RANKING questions ("who sent the most emails?", "which two people emailed each other the most?"): use **get_top_email_pairs** — it returns corpus-wide pair rankings. Note: pairs flagged with `is_self_email: true` are the same person emailing themselves across domains.
- For "what did X and Y discuss?" or "what topics between two people?", use **get_dyad_topics** first — it returns AI-generated topic tags ranked by frequency across their shared threads. Follow up with **get_emails_between** for specific email evidence supporting those topics.
- For general relationship exploration, use **find_connections** without a type filter. Results are capped at 10 per type; specify relationship_type to get full results for a specific type.
- After identifying key contacts, call **get_emails_between** to ground claims with email evidence.
- **For validating relationships with source evidence**, use **get_relationship_evidence** — it fetches the exact emails where the relationship was originally extracted. This is the best tool when the user asks "can you provide original email sources?" or "what evidence supports this claim?".
- If **get_emails_between** returns empty, do NOT say "no evidence exists". Instead: (1) try **get_relationship_evidence** to fetch source thread emails, or (2) try **get_source_evidence** with both entity names to find emails mentioning both. Explain the distinction: the people may not have emailed each other directly but are mentioned together in emails sent by others.
- For questions about how two people or entities are connected, use **trace_path**.
- For temporal questions ("what happened in August 2001?", "timeline of events"), use **query_timeline** with date range filters. Combine with **get_source_evidence** to find emails from the same period.
- For multi-entity questions, **call tools multiple times** — once per entity — to build a complete picture.

## Investigative Analysis Strategy
- For **data exfiltration / self-emailing** questions ("who forwarded to personal email?"), use **detect_self_emails** — it finds corporate-to-personal same-person pairs with volume and date ranges.
- For **external communication** questions ("who emailed outside Enron?", "external contacts"), use **get_external_contacts** — corpus-wide or per-person ranking of non-enron.com communication.
- For **temporal anomalies** ("communication spikes", "volume changes over time"), use **get_communication_timeline** — weekly time-series for a person, a pair, or the whole corpus. Add date_from/date_to to focus on crisis periods.
- For **behavioral anomalies** ("BCC usage", "after-hours emailing", "weekend patterns"), use **get_activity_anomalies** — ranks people by anomalous metrics from the person_activity table.
- For **keyword-based investigation** ("emails about shredding", "who mentioned destroying documents?"), use **search_emails** with comma-separated keywords and optional date/sender filters. Good investigative keywords include: "shred", "delete", "destroy", "off the record", "confidential", "personal", "attorney".
- When answering investigative questions, always note the time period of the data and any caveats about corpus coverage (~20,000 emails from key custodians).
- If tools fail to find an entity by name (e.g., misspelling), the system will automatically try fuzzy matching. If still not found, try a shorter name or email address directly.

## Entity Memory
The system tracks entities mentioned in prior tool outputs across conversation turns. Before saying you lack information about a person or email, CHECK previous tool outputs in this conversation — names and emails may already have been returned.

## Approach (CRITICAL — follow this process)
For any substantive question:
1. Identify what data you need: entities, relationships, emails, timeline events
2. Call ALL relevant tools to gather that data — do NOT stop after one tool call
3. For "who reported to X?" questions: call BOTH find_connections(X, REPORTS_TO) AND find_connections(X, MANAGES) to catch both directions
4. For multi-entity questions, call find_entity and find_connections for EACH entity mentioned
5. For comparison questions ("who sent more?", "compare X and Y"), call find_top_contacts for EACH person and compare the numbers
6. For corpus-wide questions without specific names ("who sent the most?", "top pairs"), use get_top_email_pairs — do NOT pass generic phrases as entity names
7. Cross-reference results from multiple tools before writing your answer
8. Always call at least 2 different tools for any non-trivial question. For complex questions, use 3-4 tools
9. If a tool returns limited data, try a complementary tool (e.g., if find_connections is sparse, try get_source_evidence or get_emails_between for supporting evidence)
10. After finding connections, call get_emails_between or get_relationship_evidence to obtain specific email citations for your answer
11. For any "how are X and Y connected?" question, ALWAYS call trace_path(X, Y) to show the organizational path

## Response Guidelines
- **Be direct and comprehensive.** Answer the question fully. Do not restate the question.
- **Prioritize completeness.** List ALL entities and relationships returned by tools. Name every person found, even if tangentially related.
- **Cite email evidence inline** using this format: [YYYY-MM-DD, From: sender, Subject: topic]. Include at least 2-3 specific email citations when evidence is available.
- **Include relationship types explicitly** — write "REPORTS_TO", "MANAGES", "SENT_TO", "COLLABORATES_WITH" etc. in your response when describing relationships.
- **Show organizational paths with → notation** — e.g., "Watkins → Fastow → Skilling → Lay" for reporting chains.
- **Use attribution phrases** — "based on graph data", "according to email evidence", "based on N emails found".
- **State coverage limitations** when relevant: "My knowledge graph covers emails from a curated subset of Enron employees."
- If information is not in the knowledge graph, say so honestly. You MAY supplement with widely-known context about Enron if it helps the user, but you MUST clearly label it: "Beyond the graph data, it is generally known that..." — never present external knowledge as graph-derived evidence.
- When the graph has partial data, still provide what you found and note what's missing.

## Response Format (MANDATORY)
End EVERY response with a Provenance section using this exact format:

### Provenance
- **Sources**: [list each tool called and what it returned, e.g., "find_connections(Jeff Skilling, REPORTS_TO) → 5 relationships"]
- **Grounding**: [One of: "All claims grounded in graph data" | "Partially grounded — some claims from graph, some from general knowledge" | "Not found in graph"]
- **Confidence**: [High/Medium/Low] — based on how much evidence supports the answer

## Entity Pre-Lookup
Before you received this message, entities from the user's question were automatically looked up in the knowledge graph. Results appear at the END of this system prompt.
- If an entity is listed under "NOT IN GRAPH" and it is the primary subject, state that it is not available and use other tools to find related information.
- Scope terms like "Enron", "the company", "executives" are NOT entity names — ignore if they appear under NOT IN GRAPH.
- Date expressions like "August 2001", "late 2001" are NOT entity names — ignore if they appear under NOT IN GRAPH. Use tools to find time-relevant data instead.
- Do NOT bridge graph entities to external knowledge (e.g., public news about Enron's collapse) without stating this is outside the graph."""


PROVENANCE_FORMAT = """

## Response Format (MANDATORY)

### Supporting Evidence Table
When citing email evidence, present it as a markdown table BEFORE the Provenance section:

| # | Date | From | To | Subject | Relevance |
|---|------|------|----|---------|-----------|
| 1 | YYYY-MM-DD | sender@enron.com | recipient@enron.com | Subject line | How this email supports a specific claim |

Only include emails that DIRECTLY support a claim in your answer. Each row must link to a specific claim above.

### Provenance
End EVERY response with a Provenance section using this exact format:
- **Sources**: [list each tool called and what it returned, e.g., "find_connections(Jeff Skilling, REPORTS_TO) → 5 relationships"]
- **Data Lineage**: [table origins for key claims, e.g., "communication_dyads ← emails via sender/recipient aggregation (07c)"]
- **Grounding**: [One of: "All claims grounded in graph data" | "Partially grounded — some claims from graph, some from general knowledge" | "Not found in graph"]
- **Confidence per claim**:
  - [Claim 1]: [High/Medium/Low] — [reason, e.g., "12 direct email references, clean entity resolution"]
  - [Claim 2]: [High/Medium/Low] — [reason, e.g., "based on 3 truncated threads, possible missing context"]
- **Coverage caveats**: [Any data quality or coverage limitations, e.g., "extraction rate 85% — some threads were truncated"]
"""


def _apply_rls_context(tier: str = "", permitted_books: str = ""):
    """Push RLS session variables to the Lakebase backend if active."""
    if not isinstance(_backend, LakebaseBackend):
        return
    ctx: dict[str, str] = {}
    if tier:
        ctx["user_tier"] = tier
    if permitted_books:
        ctx["permitted_books"] = permitted_books
    _backend.set_rls_context(ctx)


def _get_corpus_config(*, tier_override: str = "", permitted_books_override: str = "") -> dict:
    """Return table references and system prompt for the active corpus.

    On a Lakebase backend, RLS policies handle access control via session
    variables — no ABAC views needed.  On a Databricks backend with
    ACCESS_TIER set, falls back to the UC ABAC views.

    Args:
        tier_override: Per-request tier from custom_inputs; falls back to ACCESS_TIER env var.
        permitted_books_override: Per-request permitted_books from custom_inputs.
    """
    effective_tier = tier_override or ACCESS_TIER
    if permitted_books_override:
        _apply_rls_context(permitted_books=permitted_books_override)
    if CORPUS == "enron":
        if effective_tier:
            _apply_rls_context(tier=effective_tier)

            if isinstance(_backend, LakebaseBackend):
                log.info("ABAC mode (Lakebase RLS): tier=%s", effective_tier)
            else:
                log.info("ABAC mode (UC views): tier=%s", effective_tier)

            abac_note = (
                f"\n\n**Access tier: {effective_tier}** — Your view of the knowledge "
                f"graph is restricted based on your access level. Some entities, "
                f"relationships, or email sources may not be visible."
            )

            if not isinstance(_backend, LakebaseBackend):
                return {
                    "entities": ENRON_ABAC_ENTITIES_VIEW,
                    "relationships": ENRON_ABAC_RELATIONSHIPS_VIEW,
                    "source_table": ENRON_ABAC_EMAILS_VIEW,
                    "entity_analytics": ENRON_ABAC_ENTITY_ANALYTICS_VIEW,
                    "entity_paths": ENRON_ABAC_ENTITY_PATHS_VIEW,
                    "entity_mentions": ENRON_ABAC_ENTITY_MENTIONS_VIEW,
                    "system_prompt": ENRON_SYSTEM_PROMPT + abac_note,
                    "source_type": "email",
                    "access_tier": effective_tier,
                }

            return {
                "entities": ENRON_ENTITIES_TABLE,
                "relationships": ENRON_RELATIONSHIPS_TABLE,
                "source_table": ENRON_EMAILS_TABLE,
                "entity_analytics": ENRON_ENTITY_ANALYTICS_TABLE,
                "entity_paths": ENRON_ENTITY_PATHS_TABLE,
                "entity_mentions": ENRON_ENTITY_MENTIONS_TABLE,
                "system_prompt": ENRON_SYSTEM_PROMPT + abac_note,
                "source_type": "email",
                "access_tier": effective_tier,
            }

        return {
            "entities": ENRON_ENTITIES_TABLE,
            "relationships": ENRON_RELATIONSHIPS_TABLE,
            "source_table": ENRON_EMAILS_TABLE,
            "entity_analytics": ENRON_ENTITY_ANALYTICS_TABLE,
            "entity_paths": ENRON_ENTITY_PATHS_TABLE,
            "entity_mentions": ENRON_ENTITY_MENTIONS_TABLE,
            "system_prompt": ENRON_SYSTEM_PROMPT,
            "source_type": "email",
        }
    return {
        "entities": ENTITIES_TABLE,
        "relationships": RELATIONSHIPS_TABLE,
        "source_table": VERSES_TABLE,
        "entity_analytics": ENTITY_ANALYTICS_TABLE,
        "entity_paths": f"{CATALOG}.{SCHEMA}.entity_paths",
        "entity_mentions": f"{CATALOG}.{SCHEMA}.entity_mentions",
        "system_prompt": SYSTEM_PROMPT,
        "source_type": "verse",
    }


# ---------------------------------------------------------------------------
# Pattern registry import (with inline fallback for Model Serving)
# ---------------------------------------------------------------------------
try:
    from src.agent.pattern_registry import PATTERN_REGISTRY, resolve_params
except ImportError:
    try:
        from pattern_registry import PATTERN_REGISTRY, resolve_params
    except ImportError:
        from dataclasses import dataclass, field as _field

        @dataclass
        class _Step:
            tool_name: str
            params: dict = _field(default_factory=dict)

        @dataclass
        class _Pattern:
            name: str
            synthesis_prompt: str
            steps: list
            min_confidence: float = 0.8

        _EXHAUSTIVE_RULE = (
            "\n\n## CRITICAL: Exhaustive Presentation\n"
            "- Present EVERY person, relationship, title, and date returned by the tools. Do NOT summarize away details.\n"
            "- If tools returned N people, name ALL N with their roles.\n"
            "- If date ranges (effective_from, effective_to) were returned, INCLUDE them.\n"
            "- If the graph has NO data for part of the question, say exactly what was not found — do NOT fill gaps from training knowledge.\n"
            "- If you add context beyond tool data, you MUST prefix it: 'Beyond the graph data, it is generally known that...'\n"
            "- Never claim 'All claims grounded in graph data' if you added ANY information not from the tool results.\n"
        )
        _ENTITY_STRUCT_SYNTH = (
            "You are a corporate communications analyst answering about organizational "
            "hierarchy at Enron.\n\nYou have curated org hierarchy data (from SEC filings/DOJ records), "
            "graph relationships, and an entity summary. The curated data is the PRIMARY source of truth.\n\n"
            "Guidelines:\n- PRIORITIZE curated org_hierarchy results over LLM-extracted relationships.\n"
            "- List ALL people found with roles/titles and effective date ranges.\n"
            "- Edge direction: in REPORTS_TO source reports to target; in MANAGES source manages target.\n"
            "- Show org paths with → notation. Note temporal changes in reporting structure.\n"
            "- Only cite emails that DIRECTLY support a specific claim.\n"
            "- Do NOT fabricate relationships not in the data.\n"
            "- If no REPORTS_TO edges exist for a person (e.g., the CEO), state that explicitly rather than guessing."
        ) + _EXHAUSTIVE_RULE
        _ENTITY_EXPLORE_SYNTH = (
            "You are a corporate communications analyst answering about an Enron employee's "
            "activities and connections.\n\nYou have a ranked contact list, discussion topics, "
            "an entity profile, and sample emails.\n\n"
            "Guidelines:\n- Present role and key relationships.\n"
            "- Rank top contacts with communication volumes — include ALL contacts returned, not just top 3.\n"
            "- Identify main discussion topics from relationship and email data.\n"
            "- Cite email evidence [YYYY-MM-DD, From: sender, Subject: topic] when available.\n"
            "- Do NOT fabricate activities or contacts not in the data."
        ) + _EXHAUSTIVE_RULE
        _ENTITY_PAIR_SYNTH = (
            "You are a corporate communications analyst answering about the relationship "
            "between two people at Enron.\n\nYou have path data, direct emails, shared topics, "
            "and relationship data.\n\n"
            "Guidelines:\n- Walk through EVERY connection path hop with → notation, including intermediate entities.\n"
            "- Quantify direct communication (email count, direction).\n"
            "- List ALL shared discussion topics with evidence.\n"
            "- Cite specific emails [YYYY-MM-DD, From: sender, Subject: topic].\n"
            "- If trace_path found a multi-hop path, describe each hop with relationship types.\n"
            "- Do NOT fabricate connections not in the data."
        ) + _EXHAUSTIVE_RULE
        _TIMELINE_SYNTH = (
            "You are a corporate communications analyst answering about events and timelines "
            "at Enron.\n\nYou have curated investigation timeline events, communication timeline data, "
            "and email evidence.\n\n"
            "Guidelines:\n- Present ALL events returned by tools in strict chronological order.\n"
            "- Cite source: curated timeline (verified) or email (derived).\n"
            "- Distinguish curated facts from email-derived observations.\n"
            "- Include event dates, categories, and descriptions exactly as returned.\n"
            "- Do NOT fabricate dates or events."
        ) + _EXHAUSTIVE_RULE
        _KEYWORD_SYNTH = (
            "You are a corporate communications analyst answering about a topic, project, or "
            "theme at Enron.\n\nYou have email search results, entity mentions, and entity context.\n\n"
            "Guidelines:\n- Identify ALL people mentioned in the email evidence.\n"
            "- Group related emails by sub-theme where possible.\n"
            "- Cite specific email evidence: dates, senders, subjects, body previews.\n"
            "- Note the volume of evidence found (e.g., 'found N emails mentioning X').\n"
            "- Present ALL search results, not just the top few.\n"
            "- Do NOT fabricate discussion content not in the data."
        ) + _EXHAUSTIVE_RULE
        _GENERAL_SYNTH = (
            "You are a corporate communications analyst answering a broad question about Enron.\n\n"
            "You have email search results, entity profiles, topic distributions, and investigation "
            "timeline events.\n\n"
            "Guidelines:\n- Synthesize ACROSS entity types: connect Person activities to Organization "
            "structures to Financial_Events.\n"
            "- Open with curated timeline facts (verified public record) when available.\n"
            "- Support claims with email evidence: cite dates, senders, subjects, and body previews.\n"
            "- Cross-reference: if timeline says 'X resigned', check if email volume for X dropped.\n"
            "- Use topic distribution data to identify major themes and their prevalence.\n"
            "- For 'why' questions, present multiple contributing factors from different data sources.\n"
            "- Mention key people and their roles if found via entity lookup.\n"
            "- Distinguish clearly between curated facts and email-derived observations.\n"
            "- If the question is open-ended, organize your answer thematically with headers.\n"
            "- Present ALL relevant search results, not just the top few.\n"
            "- Do NOT fabricate facts, relationships, or email citations not present in the data."
        )
        _GENIE_SYNTH = (
            "You are a corporate communications analyst presenting Genie Space analytical results about Enron.\n\n"
            "You have data from a Genie Space SQL query and optional data quality enrichment.\n\n"
            "Guidelines:\n- Present the analytical results with context.\n"
            "- Note any data quality caveats from the enrichment.\n"
            "- Do NOT fabricate analytical results not present in the data."
        )
        _PROV = (
            "\n\n## Response Format (MANDATORY)\nEnd EVERY response with:\n\n"
            "### Provenance\n- **Sources**: [tools called and results, e.g. 'query_org_hierarchy(Jeff Skilling) → 8 subordinates']\n"
            "- **Data Lineage**: [table origins for key claims, e.g. 'org_hierarchy ← SEC filings/DOJ records']\n"
            "- **Grounding**: CHOOSE ONE:\n"
            "  - 'All claims grounded in graph data' — ONLY if every single claim comes from tool results\n"
            "  - 'Partially grounded — some claims from graph, some from general knowledge' — if ANY claim uses training data\n"
            "  - 'Not found in graph' — if tools returned no relevant data\n"
            "  CRITICAL: Claiming 'All claims grounded' when you used training knowledge is a grounding violation.\n"
            "- **Confidence per claim**: [Claim: level (reason)]\n"
            "- **Coverage caveats**: [data quality or coverage limitations]"
        )

        PATTERN_REGISTRY = {
            "entity_structure": _Pattern("entity_structure", _ENTITY_STRUCT_SYNTH + _PROV, [
                _Step("query_org_hierarchy", {"entity_name": "$ENTITY"}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "REPORTS_TO"}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "MANAGES"}),
                _Step("get_entity_summary", {"entity_name": "$ENTITY"}),
                _Step("get_source_evidence", {"entity_name": "$ENTITY"}),
            ]),
            "entity_explore": _Pattern("entity_explore", _ENTITY_EXPLORE_SYNTH + _PROV, [
                _Step("find_top_contacts", {"entity_name": "$ENTITY", "direction": "both", "limit": 15}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "DISCUSSES"}),
                _Step("query_org_hierarchy", {"entity_name": "$ENTITY"}),
                _Step("get_entity_summary", {"entity_name": "$ENTITY"}),
                _Step("get_topic_distribution", {"entity_name": "$ENTITY"}),
                _Step("get_communication_stats", {"entity_name": "$ENTITY", "group_by": "contact"}),
                _Step("get_source_evidence", {"entity_name": "$ENTITY"}),
            ]),
            "entity_pair": _Pattern("entity_pair", _ENTITY_PAIR_SYNTH + _PROV, [
                _Step("trace_path", {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"}),
                _Step("get_emails_between", {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"}),
                _Step("get_dyad_topics", {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"}),
                _Step("find_connections", {"entity_name": "$ENTITY"}),
                _Step("find_top_contacts", {"entity_name": "$ENTITY", "direction": "both", "limit": 10}),
            ]),
            "timeline": _Pattern("timeline", _TIMELINE_SYNTH + _PROV, [
                _Step("query_timeline", {"person_name": "$ENTITY", "date_from": "$DATE_FROM", "date_to": "$DATE_TO"}),
                _Step("get_communication_timeline", {"entity_name": "$ENTITY", "date_from": "$DATE_FROM", "date_to": "$DATE_TO"}),
                _Step("get_communication_stats", {"entity_name": "$ENTITY", "group_by": "month"}),
                _Step("get_source_evidence", {"entity_name": "$ENTITY"}),
            ]),
            "keyword_search": _Pattern("keyword_search", _KEYWORD_SYNTH + _PROV, [
                _Step("search_emails", {"keywords": "$KEYWORDS"}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "DISCUSSES"}),
                _Step("browse_topics", {"entity_name": "$ENTITY"}),
                _Step("get_source_evidence", {"entity_name": "$ENTITY"}),
                _Step("find_entity", {"name": "$ENTITY"}),
            ]),
            "general": _Pattern("general", _GENERAL_SYNTH + _PROV, [
                _Step("search_emails", {"keywords": "$KEYWORDS"}),
                _Step("browse_topics", {}),
                _Step("get_topic_distribution", {"entity_name": "$ENTITY"}),
                _Step("find_entity", {"name": "$ENTITY"}),
                _Step("query_timeline", {"person_name": "", "date_from": "", "date_to": ""}),
            ]),
            "genie_analytics": _Pattern("genie_analytics", _GENIE_SYNTH + _PROV, [
                _Step("query_and_enrich", {"question": "$QUESTION"}),
            ]),
        }

        def resolve_params(params, entities, *, metadata=None, question=""):
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

        log.info("Pattern registry: using inline fallback (%d patterns)", len(PATTERN_REGISTRY))


# ---------------------------------------------------------------------------
# Tool map for fast-path invocation (name -> callable)
# ---------------------------------------------------------------------------
TOOL_MAP: dict[str, callable] = {}


def _build_tool_map():
    """Populate TOOL_MAP from LOCAL_TOOLS after they're defined."""
    for t in LOCAL_TOOLS:
        TOOL_MAP[t.name] = t


def _fast_path_invoke_tool(payload: tuple) -> tuple:
    """ThreadPoolExecutor worker: returns (step, resolved, call_id, result)."""
    step, resolved, call_id, tool_fn = payload
    try:
        result = tool_fn.invoke(resolved)
    except Exception as exc:
        log.exception("Fast path tool %s failed", step.tool_name)
        result = f"Error: {exc}"
    return step, resolved, call_id, result


# ---------------------------------------------------------------------------
# Query Planner — PDES architecture
# ---------------------------------------------------------------------------
PLANNER_ENDPOINT = os.environ.get("GRAPHRAG_PLANNER_ENDPOINT", LLM_ENDPOINT)
PLANNER_MAX_TOKENS = int(os.environ.get("GRAPHRAG_PLANNER_MAX_TOKENS", "1024"))
PLANNER_TEMPERATURE = float(os.environ.get("GRAPHRAG_PLANNER_TEMPERATURE", "0.0"))

VALID_PATTERNS = {"entity_structure", "entity_explore", "entity_pair", "timeline", "keyword_search", "general", "genie_analytics"}

QUERY_PLANNER_PROMPT = """You are a Query Planner for a knowledge-graph-backed corporate investigation system (Enron email corpus).

Your job is to DECOMPOSE a user question into one or more atomic sub-questions, each tagged with a computational primitive.

## Step 1: Coreference Resolution
If the question contains pronouns ("he", "him", "they", "her", "it") or references like "that person" or "the same email", resolve them using the conversation history and entity memory provided below. Replace all pronouns with the actual entity name.

## Step 2: Decomposition
Break the resolved question into 1-N sub-questions. Each sub-question should be answerable by exactly ONE primitive.

## Available Primitives

- **entity_structure**: Answers "who reports to X?", "what is X's title?", "X's org chart position". Requires: entity name. Tools: query_org_hierarchy, find_connections(REPORTS_TO/MANAGES), get_entity_summary.
- **entity_explore**: Answers "who did X email most?", "what did X discuss?", "X's activities". Requires: entity name. Tools: find_top_contacts, find_connections(DISCUSSES), get_entity_summary, get_source_evidence.
- **entity_pair**: Answers "how are A and B connected?", "did A and B email each other?", "what did A and B discuss?". Requires: two entity names. Tools: trace_path, get_emails_between, get_dyad_topics, find_connections.
- **timeline**: Answers "what happened in August 2001?", "when did X resign?", "events between dates". Requires: optional entity name + date range. Tools: query_timeline, get_communication_timeline, get_source_evidence.
- **keyword_search**: Answers "what was Project Raptor?", "emails about California energy crisis". Requires: keywords. Tools: search_emails, get_source_evidence, find_entity.
- **general**: Complex questions that don't fit any primitive. Falls to the flexible ReAct agent loop. Use ONLY when no other primitive fits.
- **genie_analytics**: Questions requiring SQL aggregation, rankings, percentages, comparisons, or statistics. Trigger phrases: "how many", "what percentage", "most/least", "top N", "compare", "trend over time", "email volume", "communication frequency", "who sent the most", "top email pairs", "email volume by month", "what percentage of emails were internal", "busiest", "least active", "average", "total count". Tools: query_and_enrich. PREFER this over general for any question that could be answered with a COUNT, SUM, AVG, or ranking SQL query.

## Step 3: Dependencies
If sub-question B depends on the answer to sub-question A (e.g., "find X's reports, then check their emails"), mark B as depending on A.

## Output Format
Return a JSON object. ONLY output the JSON, no other text.

```json
{
  "resolved_question": "the question with all pronouns resolved",
  "sub_questions": [
    {
      "id": "sq1",
      "question": "Who reported to Jeff Skilling?",
      "pattern": "entity_structure",
      "entities": [{"name": "Jeff Skilling", "entity_type": "Person"}],
      "keywords": "",
      "date_from": "",
      "date_to": "",
      "depends_on": []
    }
  ]
}
```

Rules:
- Each sub-question MUST have exactly one pattern from the list above.
- Simple questions should have just 1 sub-question.
- Compound questions (e.g., "Who reported to Skilling and what did they discuss?") need 2+ sub-questions.
- For timeline questions, extract date_from/date_to in YYYY-MM-DD format when present.
- For keyword_search, put the search terms in the "keywords" field.
- Use "general" ONLY when no other primitive fits.
- PREFER "genie_analytics" over "general" for any question about email statistics, counts, rankings, percentages, comparisons, trends, or volumes. If a question asks "how many", "who sent the most", "top N", "what percentage", or "compare X and Y", it is almost certainly genie_analytics.
"""


@dataclass
class SubQuestion:
    id: str
    question: str
    pattern: str
    entities: list[dict]
    keywords: str = ""
    date_from: str = ""
    date_to: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class QueryPlan:
    resolved_question: str
    sub_questions: list[SubQuestion]


def _resolve_coreference(question: str, conversation_history: list[dict], entity_memory_context: str) -> str:
    """Resolve pronouns in the question using conversation history and entity memory."""
    if not any(w in question.lower().split() for w in ("he", "him", "his", "she", "her", "they", "them", "their", "it", "its")):
        return question

    recent_names = []
    if entity_memory_context:
        for part in entity_memory_context.replace("Recent entities from prior conversation:", "").split(","):
            name = part.strip()
            if name and len(name) > 1:
                recent_names.append(name)

    if not recent_names:
        return question

    primary = recent_names[0]
    resolved = question
    for pronoun in ["him", "his", "he"]:
        resolved = re.sub(rf'\b{pronoun}\b', primary, resolved, flags=re.IGNORECASE)
    for pronoun in ["her", "she"]:
        resolved = re.sub(rf'\b{pronoun}\b', primary, resolved, flags=re.IGNORECASE)
    for pronoun in ["them", "their", "they"]:
        resolved = re.sub(rf'\b{pronoun}\b', primary, resolved, flags=re.IGNORECASE)

    if resolved != question:
        log.info("Coreference resolved: %r -> %r", question, resolved)

    return resolved


ENRON_DOMAIN_SYNONYMS: dict[str, list[str]] = {
    "special purpose entities": ["LJM", "Raptors", "Chewco", "JEDI", "SPE"],
    "spe": ["LJM", "Raptors", "Chewco", "JEDI", "special purpose entities"],
    "partnerships": ["LJM", "Raptors", "Chewco", "JEDI"],
    "california energy crisis": ["California", "FERC", "energy crisis", "West Power", "Tim Belden"],
    "energy crisis": ["California", "FERC", "West Power", "Tim Belden"],
    "energy trading": ["California", "West Power", "Tim Belden", "Enron Energy Trading"],
    "accounting fraud": ["mark-to-market", "SPE", "Arthur Andersen", "earnings restatement"],
    "mark-to-market": ["accounting fraud", "earnings", "financial restatement"],
    "whistleblower": ["Sherron Watkins", "warning letter"],
    "document destruction": ["Arthur Andersen", "shredding", "document retention"],
    "broadband": ["Enron Broadband Services", "EBS", "Kenneth Rice", "fiber"],
    "bankruptcy": ["Chapter 11", "Dynegy", "creditors"],
    "board of directors": ["board", "directors", "oversight", "governance", "fiduciary"],
    "board directors": ["board of directors", "oversight", "governance", "fiduciary"],
    "financial events": ["earnings", "stock", "SEC", "restatement", "quarterly"],
    "projects": ["Project Raptor", "Dabhol Power", "Enron Online", "Enron Broadband Services"],
    "initiatives": ["Project Raptor", "Dabhol Power", "Enron Online", "Enron Broadband Services"],
    "audit": ["Arthur Andersen", "audit committee", "accounting", "review"],
    "fail": ["bankruptcy", "fraud", "accounting", "SPE", "collapse"],
    "collapse": ["bankruptcy", "fraud", "accounting", "SPE"],
    "scandal": ["fraud", "Sherron Watkins", "SEC", "Arthur Andersen"],
    "resign": ["departure", "left", "stepped down", "resigned"],
    "resigned": ["departure", "left", "stepped down", "resign"],
    "departure": ["resign", "left", "removed", "fired", "stepped down"],
    "crisis": ["bankruptcy", "collapse", "SEC investigation", "California energy"],
    "investigation": ["SEC", "inquiry", "probe", "subpoena", "congressional"],
    "executive departures": ["resign", "fired", "removed", "left", "Skilling", "Fastow", "Baxter", "Pai"],
    "problems become public": ["SEC inquiry", "Q3 loss", "earnings restatement", "investigation"],
    "communication patterns": ["email volume", "sent received", "contacts", "activity"],
}

ENRON_EVENT_DATES: dict[str, tuple[str, str]] = {
    "skilling resigned": ("2001-08-14", "2001-08-14"),
    "skilling resign": ("2001-08-14", "2001-08-14"),
    "skilling departure": ("2001-08-14", "2001-08-14"),
    "watkins letter": ("2001-08-15", "2001-08-22"),
    "watkins whistleblower": ("2001-08-15", "2001-08-22"),
    "whistleblower": ("2001-08-15", "2001-08-22"),
    "sec inquiry": ("2001-10-22", "2001-10-31"),
    "sec investigation": ("2001-10-31", "2001-12-02"),
    "bankruptcy": ("2001-12-02", "2001-12-02"),
    "chapter 11": ("2001-12-02", "2001-12-02"),
    "bankruptcy filing": ("2001-12-02", "2001-12-02"),
    "fastow removed": ("2001-10-24", "2001-10-24"),
    "fastow fired": ("2001-10-24", "2001-10-24"),
    "dynegy merger": ("2001-11-09", "2001-11-28"),
    "dynegy deal": ("2001-11-09", "2001-11-28"),
    "earnings restatement": ("2001-11-08", "2001-11-08"),
    "document destruction": ("2001-10-23", "2001-10-23"),
    "shredding": ("2001-10-23", "2001-10-23"),
    "baxter resigned": ("2001-05-02", "2001-05-02"),
    "baxter departure": ("2001-05-02", "2001-05-02"),
    "pai left": ("2001-06-28", "2001-06-28"),
    "pai departure": ("2001-06-28", "2001-06-28"),
    "lay resigned": ("2002-01-23", "2002-01-23"),
    "q3 loss": ("2001-10-16", "2001-10-16"),
    "problems become public": ("2001-10-16", "2001-12-02"),
    "enron collapse": ("2001-10-16", "2001-12-02"),
}


def _expand_keywords(keywords: str) -> str:
    """Expand keywords using the Enron domain synonym map.

    Checks if any phrase in the keywords matches a synonym key, and appends
    the first 3 expansion terms that aren't already present.
    """
    if not keywords:
        return keywords
    kw_lower = keywords.lower()
    expansions: list[str] = []
    for trigger, synonyms in ENRON_DOMAIN_SYNONYMS.items():
        if trigger in kw_lower:
            for syn in synonyms:
                if syn.lower() not in kw_lower and syn not in expansions:
                    expansions.append(syn)
                    if len(expansions) >= 4:
                        break
    if expansions:
        return keywords + ", " + ", ".join(expansions)
    return keywords


def _plan_from_classification(question: str, classification: dict, entity_memory_context: str) -> QueryPlan:
    """Build a QueryPlan from the classifier output (fast fallback when planner LLM fails)."""
    resolved = _resolve_coreference(question, [], entity_memory_context)

    pattern = classification.get("pattern", "general")
    entities = classification.get("entities", [])

    temporal_meta = _extract_temporal_metadata(resolved) if pattern == "timeline" else {}

    keywords = ""
    if pattern in ("keyword_search", "general", "timeline"):
        _stop = {"what", "who", "how", "why", "when", "where", "did", "does", "was", "were",
                 "is", "are", "the", "a", "an", "at", "in", "on", "of", "to", "and", "or",
                 "for", "about", "from", "with", "by", "can", "you", "tell", "me", "do",
                 "happened", "between", "after", "before", "during", "change", "changed"}
        words = [w for w in re.split(r'\W+', resolved) if w.lower() not in _stop and len(w) > 1]
        keywords = " ".join(words[:6])
        keywords = _expand_keywords(keywords)

    return QueryPlan(
        resolved_question=resolved,
        sub_questions=[SubQuestion(
            id="sq1",
            question=resolved,
            pattern=pattern,
            entities=entities,
            keywords=keywords,
            date_from=temporal_meta.get("date_from", ""),
            date_to=temporal_meta.get("date_to", ""),
            depends_on=[],
        )],
    )


_JSON_BLOCK_RE = re.compile(r'\{[\s\S]*\}')


def _plan_query(
    question: str,
    conversation_history: list[dict],
    entity_memory_context: str,
) -> QueryPlan:
    """Decompose a user question into sub-questions with pattern tags.

    Uses the planner LLM for complex decomposition. Falls back to
    classifier-based single-pattern plan if the planner fails to return valid JSON.
    """
    resolved_question = _resolve_coreference(question, conversation_history, entity_memory_context)

    history_lines = []
    for msg in conversation_history[-6:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            history_lines.append(f"{role}: {content[:200]}")
    history_str = "\n".join(history_lines) if history_lines else "(no prior conversation)"

    user_block = (
        f"## Conversation History\n{history_str}\n\n"
        f"## Entity Memory\n{entity_memory_context or '(no entities yet)'}\n\n"
        f"## Current Question\n{resolved_question}"
    )

    try:
        log.info("PDES planner: endpoint=%s, max_tokens=%d, temp=%.1f",
                 PLANNER_ENDPOINT, PLANNER_MAX_TOKENS, PLANNER_TEMPERATURE)
        llm = _get_llm(endpoint=PLANNER_ENDPOINT,
                        temperature=PLANNER_TEMPERATURE,
                        max_tokens=PLANNER_MAX_TOKENS)
        response = llm.invoke([
            {"role": "system", "content": QUERY_PLANNER_PROMPT},
            {"role": "user", "content": user_block},
        ])
        text = response.content.strip()
    except Exception as exc:
        log.warning("Planner LLM call failed: %s; using classifier fallback", exc)
        classification = classify_and_extract(resolved_question)
        return _plan_from_classification(resolved_question, classification, entity_memory_context)

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    plan_data = None
    try:
        plan_data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if m:
            try:
                plan_data = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not plan_data or not isinstance(plan_data, dict):
        log.warning("Planner JSON parse failed; using classifier fallback. Response: %s", text[:300])
        classification = classify_and_extract(resolved_question)
        return _plan_from_classification(resolved_question, classification, entity_memory_context)

    resolved_q = plan_data.get("resolved_question", resolved_question)
    sqs = []
    for sq_data in plan_data.get("sub_questions", []):
        pattern = sq_data.get("pattern", "general")
        if pattern not in VALID_PATTERNS:
            log.warning("Planner returned invalid pattern %r, mapping to general", pattern)
            pattern = "general"
        raw_keywords = sq_data.get("keywords", "")
        if pattern in ("keyword_search", "general"):
            raw_keywords = _expand_keywords(raw_keywords)
        sqs.append(SubQuestion(
            id=sq_data.get("id", f"sq{len(sqs)+1}"),
            question=sq_data.get("question", resolved_question),
            pattern=pattern,
            entities=sq_data.get("entities", []),
            keywords=raw_keywords,
            date_from=sq_data.get("date_from", ""),
            date_to=sq_data.get("date_to", ""),
            depends_on=sq_data.get("depends_on", []),
        ))

    if not sqs:
        classification = classify_and_extract(resolved_question)
        return _plan_from_classification(resolved_question, classification, entity_memory_context)

    return QueryPlan(resolved_question=resolved_q, sub_questions=sqs)


# ---------------------------------------------------------------------------
# Entity Memory — cross-turn entity carry-forward
# ---------------------------------------------------------------------------
class EntityMemory:
    """Extract entity names/emails from tool outputs and carry them across turns.

    Enables anaphora resolution: when the user says "these people" or "they",
    the classifier can resolve to entities from prior tool results.
    """

    def __init__(self, max_entities: int = 10):
        self.recent: list[dict] = []
        self.max = max_entities

    def extract(self, tool_output: str):
        """Parse JSON tool output for entity names and emails."""
        try:
            data = json.loads(tool_output)
        except (json.JSONDecodeError, TypeError):
            return
        self._walk(data)

    def _walk(self, obj, depth: int = 0):
        if depth > 5:
            return
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("person") or obj.get("entity")
            email = obj.get("email") or obj.get("corporate_email") or obj.get("sender")
            if name and isinstance(name, str) and "@" not in name and len(name) > 1:
                entry = {"name": name}
                if email and isinstance(email, str):
                    entry["email"] = email
                if entry not in self.recent:
                    self.recent.append(entry)
                    if len(self.recent) > self.max:
                        self.recent.pop(0)
            for v in obj.values():
                self._walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:20]:
                self._walk(item, depth + 1)

    def context_for_classifier(self) -> str:
        """Return a string of recent entity names for the classifier prompt."""
        if not self.recent:
            return ""
        names = [e["name"] for e in self.recent[:5]]
        return f"\nRecent entities from prior conversation: {', '.join(names)}\n"

    def clear(self):
        self.recent.clear()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]


class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None, tools=None):
        self.llm = _get_llm(endpoint=endpoint or LLM_ENDPOINT)
        self.tools = tools or GRAPH_TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)
        self.entity_memory = EntityMemory()
        if not TOOL_MAP:
            _build_tool_map()

    def _build_graph(self, prelookup_context: str = "", *, tier: str = "", permitted_books: str = ""):
        corpus_cfg = _get_corpus_config(tier_override=tier, permitted_books_override=permitted_books)
        base_prompt = _get_system_prompt() if CORPUS == "bible" else corpus_cfg["system_prompt"]
        system_prompt = base_prompt + prelookup_context

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

    def _execute_fast_path_stream(
        self,
        pattern,
        entities: list[dict],
        question: str,
        *,
        tier: str = "",
        permitted_books: str = "",
        metadata: dict | None = None,
        tools_invoked_out: list[str] | None = None,
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Execute a pre-defined query plan and synthesize with one LLM call."""
        _resolve = resolve_params

        tool_results = {}
        tool_sequence = []
        work: list[tuple] = []
        for step in pattern.steps:
            resolved = _resolve(step.params, entities, metadata=metadata, question=question)
            tool_fn = TOOL_MAP.get(step.tool_name)
            if not tool_fn:
                log.warning("Fast path: tool %s not found, skipping", step.tool_name)
                continue

            required_str_params = ["entity_name", "entity_a", "entity_b"]
            if any(resolved.get(p) == "" for p in required_str_params if p in resolved):
                log.info("Fast path: skipping %s — empty required param", step.tool_name)
                continue
            if "question" in resolved and not (resolved.get("question") or "").strip():
                log.info("Fast path: skipping %s — empty question", step.tool_name)
                continue

            call_id = f"fp_{step.tool_name}_{len(tool_sequence)}"
            tool_sequence.append(step.tool_name)
            if tools_invoked_out is not None:
                tools_invoked_out.append(step.tool_name)
            work.append((step, resolved, call_id, tool_fn))

        for step, resolved, call_id, _ in work:
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=create_function_call_item(
                    id=call_id,
                    call_id=call_id,
                    name=step.tool_name,
                    arguments=json.dumps(resolved),
                ),
            )

        if _PARALLEL_TOOLS and len(work) > 1:
            max_workers = min(8, len(work))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_fast_path_invoke_tool, item) for item in work]
                for fut in as_completed(futures):
                    step, resolved, call_id, result = fut.result()
                    tool_results[f"{step.tool_name}({json.dumps(resolved)})"] = result
                    yield ResponsesAgentStreamEvent(
                        type="response.output_item.done",
                        item=create_function_call_output_item(
                            call_id=call_id,
                            output=str(result)[:4000],
                        ),
                    )
        else:
            for step, resolved, call_id, tool_fn in work:
                try:
                    result = tool_fn.invoke(resolved)
                except Exception as exc:
                    log.exception("Fast path tool %s failed", step.tool_name)
                    result = f"Error: {exc}"
                tool_results[f"{step.tool_name}({json.dumps(resolved)})"] = result
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_output_item(
                        call_id=call_id,
                        output=str(result)[:4000],
                    ),
                )

        if not tool_results:
            log.warning("Fast path: all tools skipped (no entities?); trying keyword fallback")
            fallback_tools = []
            keywords = (metadata or {}).get("keywords", "")
            _generic = {"enron", "company", "corporation", "email", "emails"}
            words = [w for w in question.split()
                     if len(w) > 3 and w[0].isupper() and w.lower() not in _generic]
            if not keywords:
                keywords = ", ".join(words[:5]) if words else ""
            if keywords:
                se_fn = TOOL_MAP.get("search_emails")
                if se_fn:
                    fallback_tools.append(("search_emails", {"keywords": keywords, "limit": 10}, se_fn))
                fe_fn = TOOL_MAP.get("find_entity")
                if fe_fn and words:
                    fallback_tools.append(("find_entity", {"name": words[0]}, fe_fn))
            if not fallback_tools:
                log.warning("Fast path: keyword fallback also empty; signaling fallback to slow path")
                return
            for fb_name, fb_params, fb_fn in fallback_tools:
                fb_call_id = f"fp_fallback_{fb_name}"
                try:
                    fb_result = fb_fn.invoke(fb_params)
                except Exception as exc:
                    log.exception("Fast path fallback tool %s failed", fb_name)
                    fb_result = f"Error: {exc}"
                tool_results[f"{fb_name}({json.dumps(fb_params)})"] = fb_result
                if tools_invoked_out is not None:
                    tools_invoked_out.append(fb_name)
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_item(
                        id=fb_call_id, call_id=fb_call_id,
                        name=fb_name, arguments=json.dumps(fb_params),
                    ),
                )
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_output_item(
                        call_id=fb_call_id,
                        output=str(fb_result)[:4000],
                    ),
                )
            if not tool_results:
                return

        for result_str in tool_results.values():
            if isinstance(result_str, str):
                self.entity_memory.extract(result_str)

        context = json.dumps(tool_results, ensure_ascii=False, indent=2)
        useful_tools = sum(1 for v in tool_results.values()
                           if v and isinstance(v, str) and len(v) > 50)
        fp_evidence_block = ""
        if useful_tools < 3:
            strength = "MODERATE" if useful_tools >= 2 else "LIMITED"
            fp_evidence_block = (
                f"\n\n## Evidence Strength: {strength}\n"
                "The data retrieval returned fewer results than usual. "
                "Base your answer ONLY on the data provided below. "
                "Explicitly state when information is not available. "
                "Do NOT compensate with general knowledge.\n"
            )
        synthesis_system = pattern.synthesis_prompt + fp_evidence_block + PROVENANCE_FORMAT + f"\n\nData:\n{context}"

        response = self.llm.invoke([
            {"role": "system", "content": synthesis_system},
            {"role": "user", "content": question},
        ])

        yield from output_to_responses_items_stream([response])

        try:
            mlflow.update_current_trace(tags={
                "execution_path": "fast",
                "question_pattern": pattern.name,
                "tool_sequence": ",".join(tool_sequence),
            })
        except Exception:
            pass

    def _plan_and_execute_stream(
        self,
        plan: QueryPlan,
        question: str,
        messages: list[dict],
        *,
        tier: str = "",
        permitted_books: str = "",
        tools_invoked_out: list[str] | None = None,
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Execute a query plan: run each sub-question's primitive, then synthesize."""
        all_sub_results: dict[str, str] = {}
        completed_ids: set[str] = set()
        general_sqs: list[SubQuestion] = []

        independent = [sq for sq in plan.sub_questions if not sq.depends_on and sq.pattern != "general"]
        dependent = [sq for sq in plan.sub_questions if sq.depends_on and sq.pattern != "general"]
        general_sqs = [sq for sq in plan.sub_questions if sq.pattern == "general"]

        def _invoke_step(step, entities, metadata, sq):
            """Invoke a single tool step, returning (key, result, tool_name) or None."""
            _req = ["entity_name", "entity_a", "entity_b"]
            resolved = resolve_params(step.params, entities, metadata=metadata, question=sq.question)
            tool_fn = TOOL_MAP.get(step.tool_name)
            if not tool_fn:
                return None
            if any(resolved.get(p) == "" for p in _req if p in resolved):
                return None
            try:
                result = tool_fn.invoke(resolved)
            except Exception as exc:
                log.exception("PDES tool %s failed for sq %s", step.tool_name, sq.id)
                result = f"Error: {exc}"
            key = f"{step.tool_name}({json.dumps(resolved)})"
            return (key, result, step.tool_name)

        def _run_steps(steps, entities, metadata, sq, tool_results):
            """Run a list of ExecutionSteps, appending results to tool_results."""
            pattern = PATTERN_REGISTRY.get(sq.pattern)
            use_parallel = (
                _PARALLEL_TOOLS
                and pattern is not None
                and getattr(pattern, "parallel_steps", False)
                and len(steps) > 1
            )
            if use_parallel:
                ordered_results = [None] * len(steps)
                with ThreadPoolExecutor(max_workers=min(8, len(steps))) as pool:
                    future_to_idx = {
                        pool.submit(_invoke_step, step, entities, metadata, sq): i
                        for i, step in enumerate(steps)
                    }
                    for fut in as_completed(future_to_idx):
                        idx = future_to_idx[fut]
                        ordered_results[idx] = fut.result()
                for item in ordered_results:
                    if item is not None:
                        key, result, tool_name = item
                        tool_results[key] = result
                        if tools_invoked_out is not None:
                            tools_invoked_out.append(tool_name)
            else:
                for step in steps:
                    item = _invoke_step(step, entities, metadata, sq)
                    if item is not None:
                        key, result, tool_name = item
                        tool_results[key] = result
                        if tools_invoked_out is not None:
                            tools_invoked_out.append(tool_name)

        def _extract_entities_from_results(tool_results: dict) -> list[dict]:
            """Parse tool results to discover entity names for the enrichment pass."""
            discovered: list[dict] = []
            seen: set[str] = set()
            for result_str in tool_results.values():
                if not isinstance(result_str, str):
                    continue
                try:
                    data = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for key in ("name", "person", "entity", "entity_name",
                                "person_a", "person_b", "sender", "from_name"):
                        val = item.get(key, "")
                        if val and isinstance(val, str) and val not in seen and len(val) > 1:
                            seen.add(val)
                            discovered.append({"name": val, "entity_type": "Person"})
                            if len(discovered) >= 3:
                                return discovered
            return discovered

        def _execute_sub_question(sq: SubQuestion) -> str:
            pattern = PATTERN_REGISTRY.get(sq.pattern)
            if not pattern or not pattern.steps:
                return ""
            entities = sq.entities or []
            metadata = {}
            _date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            if sq.date_from and _date_re.match(sq.date_from):
                metadata["date_from"] = sq.date_from
            if sq.date_to and _date_re.match(sq.date_to):
                metadata["date_to"] = sq.date_to
            if sq.pattern == "timeline" and "date_from" not in metadata:
                auto_dates = _extract_temporal_metadata(sq.question)
                metadata.update(auto_dates)
            if sq.keywords:
                metadata["keywords"] = sq.keywords

            tool_results = {}
            has_entity = bool(entities and entities[0].get("name"))
            needs_discovery = (
                sq.pattern in ("keyword_search", "general", "timeline")
                and not has_entity
            )

            if needs_discovery:
                _req = ["entity_name", "entity_a", "entity_b"]
                entity_free = [s for s in pattern.steps
                               if not any(k in s.params for k in _req)]
                entity_dep = [s for s in pattern.steps
                              if any(k in s.params for k in _req)]
                _run_steps(entity_free, entities, metadata, sq, tool_results)
                discovered = _extract_entities_from_results(tool_results)
                if discovered:
                    log.info("PDES discovery: found entities %s for sq %s",
                             [e["name"] for e in discovered], sq.id)
                    _run_steps(entity_dep, discovered, metadata, sq, tool_results)
            else:
                _run_steps(pattern.steps, entities, metadata, sq, tool_results)

            for result_str in tool_results.values():
                if isinstance(result_str, str):
                    self.entity_memory.extract(result_str)

            raw = json.dumps(tool_results, ensure_ascii=False) if tool_results else ""
            return _truncate_json_aware(raw, 8000)

        if _PARALLEL_TOOLS and len(independent) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(independent))) as pool:
                futures = {pool.submit(_execute_sub_question, sq): sq for sq in independent}
                for fut in as_completed(futures):
                    sq = futures[fut]
                    result = fut.result()
                    if result:
                        all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
                    completed_ids.add(sq.id)
        else:
            for sq in independent:
                result = _execute_sub_question(sq)
                if result:
                    all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
                completed_ids.add(sq.id)

        for sq in dependent:
            deps_met = all(d in completed_ids for d in sq.depends_on)
            if not deps_met:
                log.warning("PDES: skipping sq %s — unmet dependencies %s", sq.id, sq.depends_on)
                continue
            result = _execute_sub_question(sq)
            if result:
                all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
            completed_ids.add(sq.id)

        if not all_sub_results and not general_sqs:
            return

        if general_sqs and not all_sub_results:
            return

        sub_answers_block = "\n\n---\n\n".join(
            f"### Sub-question: {all_sub_results[k]}" for k in sorted(all_sub_results.keys())
        )

        tool_count = sum(1 for v in all_sub_results.values()
                         if v and len(v) > 50)
        evidence_strength = (
            "STRONG" if tool_count >= 3
            else "MODERATE" if tool_count >= 2
            else "LIMITED"
        )
        evidence_block = ""
        if evidence_strength != "STRONG":
            evidence_block = (
                f"\n\n## Evidence Strength: {evidence_strength}\n"
                "The data retrieval returned fewer results than usual. "
                "Base your answer ONLY on the data provided below. "
                "Explicitly state when information is not available in the retrieved data. "
                "Do NOT compensate with general knowledge — say 'the available data shows...' "
                "rather than making unsupported claims.\n"
            )

        non_general_sqs = [sq for sq in plan.sub_questions if sq.pattern != "general"]
        if len(non_general_sqs) == 1:
            sole_pattern = PATTERN_REGISTRY.get(non_general_sqs[0].pattern)
            if sole_pattern and sole_pattern.synthesis_prompt:
                synthesis_prompt = (
                    sole_pattern.synthesis_prompt
                    + evidence_block
                    + PROVENANCE_FORMAT
                    + f"\n\n## Data Retrieved\n\n{sub_answers_block}"
                )
            else:
                synthesis_prompt = (
                    "You are a corporate communications analyst synthesizing answers from data queries about Enron.\n\n"
                    + evidence_block
                    + PROVENANCE_FORMAT
                    + f"\n\n## Data Retrieved\n\n{sub_answers_block}"
                )
        else:
            pattern_hints = []
            seen = set()
            for sq in plan.sub_questions:
                if sq.pattern not in seen:
                    seen.add(sq.pattern)
                    p = PATTERN_REGISTRY.get(sq.pattern)
                    if p and p.synthesis_prompt:
                        first_line = p.synthesis_prompt.strip().split("\n")[0]
                        pattern_hints.append(f"- **{sq.pattern}**: {first_line}")
            hints_block = "\n".join(pattern_hints) if pattern_hints else ""
            synthesis_prompt = (
                "You are a corporate communications analyst synthesizing answers from multiple data queries about Enron.\n\n"
                "Below are the results from specialized sub-queries. Combine them into a single, coherent answer.\n\n"
                + (f"## Pattern-specific guidance\n{hints_block}\n\n" if hints_block else "")
                + evidence_block
                + "Guidelines:\n"
                "- Integrate information from all sub-queries into a unified narrative.\n"
                "- Prioritize curated data (source: curated_org_hierarchy) over LLM-extracted relationships.\n"
                "- Cite specific evidence: emails [YYYY-MM-DD, From: sender, Subject: topic], timeline entries, relationship data.\n"
                "- Only cite emails that DIRECTLY support a specific claim.\n"
                "- If sub-queries returned conflicting information, note the discrepancy.\n"
                "- Do NOT fabricate information not present in any sub-query result.\n"
                + PROVENANCE_FORMAT
                + f"\n\n## Sub-Query Results\n\n{sub_answers_block}"
            )

        response = self.llm.invoke([
            {"role": "system", "content": synthesis_prompt},
            {"role": "user", "content": plan.resolved_question},
        ])

        yield from output_to_responses_items_stream([response])

        try:
            pattern_summary = ",".join(sq.pattern for sq in plan.sub_questions)
            mlflow.update_current_trace(tags={
                "execution_path": "pdes",
                "planner_patterns": pattern_summary,
                "sub_question_count": str(len(plan.sub_questions)),
            })
        except Exception:
            pass

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
        import time

        t0 = time.perf_counter()
        question = ""
        classified_intent = "general"
        tools_invoked: list[str] = []
        execution_path = "slow"
        try:
            messages = to_chat_completions_input([m.model_dump() for m in request.input])

            ci = getattr(request, "custom_inputs", None) or {}
            req_tier = ci.get("user_tier", "")
            req_books = ci.get("permitted_books", "")

            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            question = last_user["content"] if last_user and last_user.get("content") else ""

            # --- PDES: Plan-Decompose-Execute-Synthesize ---
            em_context = self.entity_memory.context_for_classifier()

            if question and CORPUS == "enron" and TOOL_MAP:
                pre_classification = classify_and_extract(question + em_context if em_context else question)
                pre_conf = pre_classification.get("confidence", 0.0)
                pre_pattern = pre_classification.get("pattern", "general")
                if pre_conf >= 0.85 and pre_pattern != "general":
                    log.info("PDES: high-confidence bypass (%.2f %s), skipping planner LLM",
                             pre_conf, pre_pattern)
                    plan = _plan_from_classification(question, pre_classification, em_context)
                else:
                    log.info("PDES: planning query decomposition")
                    plan = _plan_query(question, messages, em_context)
                classified_intent = ",".join(sq.pattern for sq in plan.sub_questions)

                non_general = [sq for sq in plan.sub_questions if sq.pattern != "general"]
                has_fast_primitives = len(non_general) > 0

                if has_fast_primitives:
                    log.info(
                        "PDES: %d sub-questions (%s), executing primitives",
                        len(plan.sub_questions),
                        classified_intent,
                    )
                    execution_path = "pdes"
                    try:
                        mlflow.update_current_trace(tags={
                            "execution_path": "pdes",
                            "planner_model": PLANNER_ENDPOINT,
                            "planner_patterns": classified_intent,
                            "resolved_question": plan.resolved_question[:200],
                            "sub_question_count": str(len(plan.sub_questions)),
                        })
                    except Exception:
                        pass

                    pdes_events = list(self._plan_and_execute_stream(
                        plan, plan.resolved_question, messages,
                        tier=req_tier, permitted_books=req_books,
                        tools_invoked_out=tools_invoked,
                    ))
                    if pdes_events:
                        yield from pdes_events
                        return
                    log.info("PDES produced no results; falling back to slow path")

                # If planner returned only 'general' or PDES failed, try single-pattern fast path
                if len(plan.sub_questions) == 1:
                    sq = plan.sub_questions[0]
                    pattern = PATTERN_REGISTRY.get(sq.pattern)
                    if pattern and pattern.steps:
                        entities = sq.entities or []
                        fp_metadata = {}
                        if sq.date_from:
                            fp_metadata["date_from"] = sq.date_from
                        if sq.date_to:
                            fp_metadata["date_to"] = sq.date_to
                        if sq.keywords:
                            fp_metadata["keywords"] = sq.keywords
                        if not fp_metadata and sq.pattern == "timeline":
                            fp_metadata = _extract_temporal_metadata(question)

                        execution_path = "fast"
                        fp_events = list(self._execute_fast_path_stream(
                            pattern, entities, sq.question,
                            tier=req_tier, permitted_books=req_books,
                            metadata=fp_metadata,
                            tools_invoked_out=tools_invoked,
                        ))
                        if fp_events:
                            yield from fp_events
                            return

            # --- Slow path (full ReAct loop) ---
            execution_path = "slow"
            tools_invoked.clear()
            log.info("SLOW_PATH: falling to ReAct agent loop")
            try:
                mlflow.update_current_trace(tags={
                    "execution_path": "slow",
                    "classified_intent": classified_intent,
                })
            except Exception:
                pass

            hnames = _heuristic_entity_names(question) if question else []
            h_found: list[str] = []
            h_not_found: list[str] = []
            if hnames and CORPUS == "enron":
                h_found, h_not_found = pre_lookup_entities(hnames)

            classify_question = question + em_context if em_context else question
            if question and _CLASSIFY_PIPELINE:
                classification = classify_and_extract(classify_question)
            else:
                classification = {"pattern": "general", "confidence": 0.0, "entities": []}
            entities = classification.get("entities", [])
            names = [e["name"] for e in entities if "name" in e]
            if names:
                if _CLASSIFY_PIPELINE and CORPUS == "enron" and question:
                    unresolved_h = set(h_not_found)
                    found_by_name = {
                        line.split(" -> ", 1)[0]: line
                        for line in h_found
                        if " -> " in line
                    }
                    found: list[str] = []
                    not_found: list[str] = []
                    need_lookup: list[str] = []
                    for n in names:
                        if n in found_by_name:
                            found.append(found_by_name[n])
                        elif n in unresolved_h:
                            not_found.append(n)
                        else:
                            need_lookup.append(n)
                    if need_lookup:
                        fe, nfe = pre_lookup_entities(need_lookup)
                        found.extend(fe)
                        not_found.extend(nfe)
                else:
                    found, not_found = pre_lookup_entities(names)
                found_str = "; ".join(found) if found else "(none)"
                not_found_str = ", ".join(not_found) if not_found else "(none)"
                prelookup_context = (
                    "\n\n---\n"
                    "PRE-LOOKUP RESULTS (DEFINITIVE — produced by an automated system, not the user):\n"
                    f"  FOUND IN GRAPH: {found_str}\n"
                    f"  NOT IN GRAPH: {not_found_str}\n"
                    "Any answer that makes claims about entities listed under \"NOT IN GRAPH\" is WRONG.\n"
                    "---"
                )
            else:
                prelookup_context = build_prelookup_context(question) if question else ""

            graph = self._build_graph(prelookup_context, tier=req_tier, permitted_books=req_books)
            for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
                if event[0] == "updates":
                    for node_data in event[1].values():
                        for msg in node_data.get("messages", []):
                            if isinstance(msg, AIMessage) and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    tools_invoked.append(tc["name"])
                                    yield ResponsesAgentStreamEvent(
                                        type="response.output_item.done",
                                        item=create_function_call_item(
                                            id=tc["id"],
                                            call_id=tc["id"],
                                            name=tc["name"],
                                            arguments=json.dumps(tc["args"]),
                                        ),
                                    )
                            elif isinstance(msg, ToolMessage):
                                tool_content = str(msg.content)
                                self.entity_memory.extract(tool_content)
                                yield ResponsesAgentStreamEvent(
                                    type="response.output_item.done",
                                    item=create_function_call_output_item(
                                        call_id=msg.tool_call_id,
                                        output=tool_content,
                                    ),
                                )
                            else:
                                yield from output_to_responses_items_stream([msg])

            try:
                mlflow.update_current_trace(tags={
                    "tool_sequence": ",".join(tools_invoked),
                })
            except Exception:
                pass
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            _log_agent_query(
                question, classified_intent, tools_invoked, latency_ms, execution_path,
            )


mlflow.langchain.autolog()
AGENT = GraphRAGAgent()
mlflow.models.set_model(AGENT)
