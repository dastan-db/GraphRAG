"""
Self-contained GraphRAG agent for Model Serving.
Consolidates config, tools, and agent into a single importable module
so MLflow can load it without notebook %run dependencies.
"""
import json
import logging
import os
import re

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

CORPUS = os.environ.get("GRAPHRAG_CORPUS", "bible")

ENRON_SCHEMA = os.environ.get("GRAPHRAG_ENRON_SCHEMA", "graphrag_enron")
ENRON_ENTITIES_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entities"
ENRON_RELATIONSHIPS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.relationships"
ENRON_EMAILS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.emails"
ENRON_ENTITY_ANALYTICS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics"
ENRON_ENTITY_PATHS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_paths"
ENRON_ENTITY_MENTIONS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"

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
def _get_llm(endpoint: str = LLM_ENDPOINT, **kwargs):
    """Return a LangChain chat model for the configured provider."""
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
   - org_hierarchy: questions about reporting lines, authority, roles, org structure, who managed whom, who reported to whom, job titles
   - communication: questions about who communicated with whom, top contacts, email frequency, most frequent correspondents
   - temporal: questions about changes over time, before/after events, anomalies, spikes, timeline
   - topic: questions about what was discussed, what topics, what subjects, what deals or projects
   - path: questions about how two specific entities are connected, degrees of separation
   - general: anything that doesn't clearly fit the above categories

2. EXTRACT all significant entities mentioned in the question.
   For each entity provide:
   - name: The canonical name (e.g., "Kenneth Lay" not "Ken")
   - entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

Return a JSON object with exactly this structure:
{"pattern": "<one of the 6 pattern names>", "confidence": <0.0 to 1.0>, "entities": [{"name": "...", "entity_type": "..."}]}

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


def _extract_temporal_metadata(question: str) -> dict:
    """Parse date references from a question to build date_from/date_to filters."""
    matches = list(_TEMPORAL_DATE_RE.finditer(question))
    if not matches:
        return {}

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
        entities.append({
            "name": r["name"],
            "type": r["entity_type"],
            "description": r["description"],
            "first_mention": mention,
        })
    return json.dumps(entities, ensure_ascii=False)


def _resolve_enron_entity_id(entity_name: str) -> list[str]:
    """Resolve an entity name to all possible entity_id patterns for Enron.

    Returns a list of LIKE patterns to try. Checks the entity_aliases table
    and also tries common name transformations.
    """
    primary_id = "_".join(entity_name.lower().split())
    patterns = [f"%{primary_id}%"]

    parts = entity_name.lower().split()
    if len(parts) == 2:
        last_name = parts[1]
        first_name = parts[0]
        patterns.append(f"%{last_name}_{first_name}%")
        patterns.append(f"%{first_name[0]}_{last_name}%")

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


def _parse_search_terms(entity_name: str) -> list[str]:
    """Split 'A AND B' into multiple search terms; returns single-element list otherwise."""
    if " AND " in entity_name:
        return [t.strip() for t in entity_name.split(" AND ") if t.strip()]
    return [entity_name]


def _dedup_contacts(contacts: list[dict]) -> list[dict]:
    """Merge contacts that resolve to the same canonical entity via entity_aliases.

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

    grouped: dict[str, dict] = {}
    for c, slug in zip(contacts, raw_slugs):
        canonical = slug_map.get(slug, slug)
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
    Use this for questions like "who emailed X the most?" or "who did X communicate with most?".

    Args:
        entity_name: The person to find top contacts for (e.g., "Kenneth Lay")
        direction: Filter direction — "both" (default), "inbound" (emails TO entity), or "outbound" (emails FROM entity).
        limit: Max number of contacts to return (default 10).
    """
    if CORPUS != "enron":
        return "find_top_contacts is only available for the Enron corpus. Use find_connections instead."

    entity_id = "_".join(entity_name.lower().split())
    eid_pattern = f"%{entity_id}%"

    if direction == "outbound":
        sql = (
            f"SELECT COALESCE(e2.name, r.target_entity) AS contact,"
            f" SUM(COALESCE(r.edge_count, 1)) AS sent,"
            f" 0 AS received,"
            f" SUM(COALESCE(r.edge_count, 1)) AS total"
            f" FROM {RELATIONSHIPS_TABLE} r"
            f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            f" WHERE r.source_entity LIKE :eid_pattern AND r.relationship_type = 'SENT_TO'"
            f" GROUP BY contact ORDER BY total DESC LIMIT {int(limit)}"
        )
    elif direction == "inbound":
        sql = (
            f"SELECT COALESCE(e1.name, r.source_entity) AS contact,"
            f" 0 AS sent,"
            f" SUM(COALESCE(r.edge_count, 1)) AS received,"
            f" SUM(COALESCE(r.edge_count, 1)) AS total"
            f" FROM {RELATIONSHIPS_TABLE} r"
            f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f" WHERE r.target_entity LIKE :eid_pattern AND r.relationship_type = 'SENT_TO'"
            f" GROUP BY contact ORDER BY total DESC LIMIT {int(limit)}"
        )
    else:
        sql = (
            f"SELECT contact,"
            f" SUM(CASE WHEN dir = 'out' THEN freq ELSE 0 END) AS sent,"
            f" SUM(CASE WHEN dir = 'in' THEN freq ELSE 0 END) AS received,"
            f" SUM(freq) AS total"
            f" FROM ("
            f"   SELECT COALESCE(e2.name, r.target_entity) AS contact, 'out' AS dir,"
            f"   SUM(COALESCE(r.edge_count, 1)) AS freq"
            f"   FROM {RELATIONSHIPS_TABLE} r"
            f"   LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            f"   WHERE r.source_entity LIKE :eid_pattern AND r.relationship_type = 'SENT_TO'"
            f"   GROUP BY contact"
            f"   UNION ALL"
            f"   SELECT COALESCE(e1.name, r.source_entity) AS contact, 'in' AS dir,"
            f"   SUM(COALESCE(r.edge_count, 1)) AS freq"
            f"   FROM {RELATIONSHIPS_TABLE} r"
            f"   LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f"   WHERE r.target_entity LIKE :eid_pattern AND r.relationship_type = 'SENT_TO'"
            f"   GROUP BY contact"
            f" ) combined"
            f" GROUP BY contact ORDER BY total DESC LIMIT {int(limit)}"
        )

    results = _backend.execute_sql(sql, params={"eid_pattern": eid_pattern})

    if not results:
        return f"No email contacts found for '{entity_name}'."

    humanize = _maybe_humanize
    contacts = []
    for r in results:
        contacts.append({
            "name": humanize(r["contact"]),
            "sent": int(r.get("sent") or 0),
            "received": int(r.get("received") or 0),
            "total": int(r.get("total") or 0),
        })
    contacts = _dedup_contacts(contacts)
    return json.dumps({
        "entity": entity_name,
        "direction": direction,
        "top_contacts": contacts,
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

    a_lower = entity_a.lower()
    b_lower = entity_b.lower()
    a_dot = a_lower.replace(" ", ".")
    b_dot = b_lower.replace(" ", ".")

    a_patterns_hdr = list(dict.fromkeys([f"%{a_lower}%", f"%{a_dot}%"]))
    b_patterns_hdr = list(dict.fromkeys([f"%{b_lower}%", f"%{b_dot}%"]))

    results = []
    match_type = "header"
    for ai, a_pat in enumerate(a_patterns_hdr):
        for bi, b_pat in enumerate(b_patterns_hdr):
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
        "total": len(emails),
        "match_type": match_type,
        "emails": emails,
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
                + (" Try get_context_verses with 'A AND B' syntax to find emails mentioning both." if not relationship_type else ""),
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
                "Try get_context_verses with 'A AND B' syntax to find emails mentioning both.",
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
def get_context_verses(entity_name: str, book: str = "") -> str:
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
            f"SELECT sender, subject, date,"
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
                "date": str(r.get("date", ""))[:10],
                "sender": r.get("sender", ""),
                "subject": r.get("subject", ""),
                "body_preview": (r.get("body_preview", "") or "")[:200],
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
    entity_id = "_".join(entity_name.lower().split())
    eid_params = {"eid_pattern": f"%{entity_id}%"}

    if CORPUS == "enron":
        entity_cols = "entity_id, name, entity_type, description, first_mention_subject AS mention_a, first_mention_thread AS mention_b"
    else:
        entity_cols = "entity_id, name, entity_type, description, first_mention_book AS mention_a, first_mention_chapter AS mention_b"

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
        params=eid_params,
    )

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
        a_patterns = _resolve_enron_entity_id(entity_a)
        b_patterns = _resolve_enron_entity_id(entity_b)
    else:
        eid_a = "_".join(entity_a.lower().split())
        eid_b = "_".join(entity_b.lower().split())
        a_patterns = [f"%{eid_a}%"]
        b_patterns = [f"%{eid_b}%"]

    start_rows = []
    for pat in a_patterns:
        start_rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            " WHERE entity_id LIKE :pattern LIMIT 3",
            params={"pattern": pat},
        )
        if start_rows:
            break

    end_rows = []
    for pat in b_patterns:
        end_rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            " WHERE entity_id LIKE :pattern LIMIT 3",
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
    max_h = min(int(max_hops), 6)

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
        f"  ON r.source_entity = b.current_id OR r.target_entity = b.current_id"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
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
        conditions.append("ARRAY_CONTAINS(key_persons, :person_name)")
        params["person_name"] = person_name
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


LOCAL_TOOLS = [find_entity, find_connections, find_top_contacts, get_emails_between,
               get_relationship_evidence, get_context_verses, get_entity_summary,
               list_entities_by_book, find_cross_book_entities, trace_path, compare_entity_sets,
               query_timeline]


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
    def get_context_verses(entity_name: str, book: str = "") -> str:
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

    return [find_entity, find_connections, get_context_verses, get_entity_summary,
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
- **get_context_verses(entity_name, book="")** — retrieve actual Bible verses mentioning an entity
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
- After gathering entity/relationship data, call **get_context_verses** for key claims you want to ground with verse references.
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
- **find_entity(name)** — search for a person, organization, project, or event by name
- **find_connections(entity_name, relationship_type="")** — find relationships for an entity, optionally filtered by type (REPORTS_TO, MANAGES, SENT_TO, DISCUSSES, COLLABORATES_WITH, etc.). Returns `evidence_count` per relationship showing how many source emails back the claim.
- **find_top_contacts(entity_name, direction, limit)** — ranked list of who communicated most with an entity (sent/received/total counts). Automatically deduplicates aliased entities.
- **get_emails_between(entity_a, entity_b)** — retrieve emails between two people. First searches sender/recipient headers; if no direct emails exist, falls back to finding emails that mention both people in the body. Check the `match_type` field: "header" = direct email exchange, "body_mention" = both mentioned in the same email.
- **get_relationship_evidence(source_entity, target_entity, relationship_type="")** — retrieve the original emails where a graph relationship was extracted from. Use this to validate relationship claims with source evidence.
- **get_context_verses(entity_name)** — find emails mentioning an entity in the body text; supports 'A AND B' syntax
- **get_entity_summary(entity_name)** — get a comprehensive entity profile with all relationships
- **trace_path(entity_a, entity_b)** — find shortest path between two entities via relationship traversal
- **query_timeline(person_name="", date_from="", date_to="", category="")** — query curated Enron investigation timeline for key events by date range, person, or category

## Tool Usage Strategy
- ALWAYS use tools before answering. Prefer graph data over training knowledge.
- For questions about people, use **find_entity** first, then **get_entity_summary** for their full profile.
- For organizational hierarchy questions ("who reported to X?", "who managed Y?"), use **find_connections** with `relationship_type="REPORTS_TO"` and/or `relationship_type="MANAGES"` to get focused results. Note edge direction: in REPORTS_TO, source reports to target; in MANAGES, source manages target.
- For "who communicated most with X?" questions, use **find_top_contacts** — it returns a ranked list with sent/received counts.
- For "what did X and Y discuss?" or "what topics?", use **get_emails_between** — it retrieves actual emails by sender/recipient, not body search.
- For general relationship exploration, use **find_connections** without a type filter. Results are capped at 10 per type; specify relationship_type to get full results for a specific type.
- After identifying key contacts, call **get_emails_between** to ground claims with email evidence.
- **For validating relationships with source evidence**, use **get_relationship_evidence** — it fetches the exact emails where the relationship was originally extracted. This is the best tool when the user asks "can you provide original email sources?" or "what evidence supports this claim?".
- If **get_emails_between** returns empty, do NOT say "no evidence exists". Instead: (1) try **get_relationship_evidence** to fetch source thread emails, or (2) try **get_context_verses** with both entity names to find emails mentioning both. Explain the distinction: the people may not have emailed each other directly but are mentioned together in emails sent by others.
- For questions about how two people or entities are connected, use **trace_path**.
- For temporal questions ("what happened in August 2001?", "timeline of events"), use **query_timeline** with date range filters. Combine with **get_context_verses** to find emails from the same period.
- For multi-entity questions, **call tools multiple times** — once per entity — to build a complete picture.

## Approach (CRITICAL — follow this process)
For any substantive question:
1. Identify what data you need: entities, relationships, emails, timeline events
2. Call ALL relevant tools to gather that data — do NOT stop after one tool call
3. For "who reported to X?" questions: call BOTH find_connections(X, REPORTS_TO) AND find_connections(X, MANAGES) to catch both directions
4. For multi-entity questions, call find_entity and find_connections for EACH entity mentioned
5. Cross-reference results from multiple tools before writing your answer
6. Always call at least 2 different tools for any non-trivial question. For complex questions, use 3-4 tools
7. If a tool returns limited data, try a complementary tool (e.g., if find_connections is sparse, try get_context_verses or get_emails_between for supporting evidence)
8. After finding connections, call get_emails_between or get_relationship_evidence to obtain specific email citations for your answer
9. For any "how are X and Y connected?" question, ALWAYS call trace_path(X, Y) to show the organizational path

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
End EVERY response with a Provenance section using this exact format:

### Provenance
- **Sources**: [list each tool called and what it returned, e.g., "find_connections(Jeff Skilling, REPORTS_TO) → 5 relationships"]
- **Grounding**: [One of: "All claims grounded in graph data" | "Partially grounded — some claims from graph, some from general knowledge" | "Not found in graph"]
- **Confidence**: [High/Medium/Low] — based on how much evidence supports the answer
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

        _ORG_SYNTH = (
            "You are a corporate communications analyst answering about organizational "
            "hierarchy at Enron.\n\nYou have pre-fetched data: REPORTS_TO, MANAGES relationships, "
            "entity summary, and email evidence. Use ALL data for a comprehensive, well-cited answer.\n\n"
            "Guidelines:\n- List ALL people found with roles/titles.\n"
            "- Edge direction: in REPORTS_TO source reports to target; in MANAGES source manages target.\n"
            "- Cite email evidence [YYYY-MM-DD, From: sender, Subject: topic] when available.\n"
            "- Show org paths with → notation. Include REPORTS_TO/MANAGES labels.\n"
            "- Do NOT fabricate relationships not in the data."
        )
        _COMM_SYNTH = (
            "You are a corporate communications analyst answering about communication "
            "patterns at Enron.\n\nYou have pre-fetched data: ranked contacts with sent/received "
            "counts, entity profile, and sample emails. Use ALL data for a comprehensive answer.\n\n"
            "Guidelines:\n- Present ranked contacts with volumes.\n"
            "- Note directional patterns. Cite email evidence when available.\n"
            "- Do NOT fabricate communication patterns not in the data."
        )
        _PATH_SYNTH = (
            "You are a corporate communications analyst answering how entities are connected "
            "at Enron.\n\nYou have pre-fetched path data and email evidence between endpoints.\n\n"
            "Guidelines:\n- Walk through each path hop with → notation and relationship types.\n"
            "- Cite email evidence when available.\n"
            "- Do NOT fabricate connections not in the data."
        )
        _TEMP_SYNTH = (
            "You are a corporate communications analyst answering about events and timelines "
            "at Enron.\n\nYou have pre-fetched timeline events and emails from the relevant period.\n\n"
            "Guidelines:\n- Present events chronologically. Cite sources: timeline or email evidence.\n"
            "- Distinguish timeline facts from email-derived evidence.\n"
            "- Do NOT fabricate dates or events."
        )
        _TOPIC_SYNTH = (
            "You are a corporate communications analyst answering about discussion topics "
            "at Enron.\n\nYou have pre-fetched entity profile, DISCUSSES relationships, and emails.\n\n"
            "Guidelines:\n- Identify discussion themes from email subjects/body.\n"
            "- Group by topic. Cite email evidence.\n"
            "- Do NOT fabricate topics not in the data."
        )
        _PROV = (
            "\n\n## Response Format (MANDATORY)\nEnd EVERY response with:\n\n"
            "### Provenance\n- **Sources**: [tools called and results]\n"
            "- **Grounding**: [All claims grounded in graph data | Partially grounded]\n"
            "- **Confidence**: [High/Medium/Low]"
        )

        PATTERN_REGISTRY = {
            "org_hierarchy": _Pattern("org_hierarchy", _ORG_SYNTH + _PROV, [
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "REPORTS_TO"}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "MANAGES"}),
                _Step("get_entity_summary", {"entity_name": "$ENTITY"}),
                _Step("get_context_verses", {"entity_name": "$ENTITY"}),
            ], 0.8),
            "communication": _Pattern("communication", _COMM_SYNTH + _PROV, [
                _Step("find_top_contacts", {"entity_name": "$ENTITY", "direction": "both", "limit": 15}),
                _Step("get_entity_summary", {"entity_name": "$ENTITY"}),
                _Step("get_context_verses", {"entity_name": "$ENTITY"}),
            ], 0.8),
            "path": _Pattern("path", _PATH_SYNTH + _PROV, [
                _Step("trace_path", {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"}),
                _Step("find_connections", {"entity_name": "$ENTITY"}),
                _Step("get_emails_between", {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"}),
            ], 0.85),
            "temporal": _Pattern("temporal", _TEMP_SYNTH + _PROV, [
                _Step("query_timeline", {"person_name": "$ENTITY", "date_from": "$DATE_FROM", "date_to": "$DATE_TO"}),
                _Step("get_context_verses", {"entity_name": "$ENTITY"}),
            ], 0.75),
            "topic": _Pattern("topic", _TOPIC_SYNTH + _PROV, [
                _Step("get_entity_summary", {"entity_name": "$ENTITY"}),
                _Step("find_connections", {"entity_name": "$ENTITY", "relationship_type": "DISCUSSES"}),
                _Step("get_context_verses", {"entity_name": "$ENTITY"}),
            ], 0.75),
        }

        def resolve_params(params, entities, *, metadata=None):
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

        log.info("Pattern registry: using inline fallback (%d patterns)", len(PATTERN_REGISTRY))


# ---------------------------------------------------------------------------
# Tool map for fast-path invocation (name -> callable)
# ---------------------------------------------------------------------------
TOOL_MAP: dict[str, callable] = {}


def _build_tool_map():
    """Populate TOOL_MAP from LOCAL_TOOLS after they're defined."""
    for t in LOCAL_TOOLS:
        TOOL_MAP[t.name] = t


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
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Execute a pre-defined query plan and synthesize with one LLM call."""
        from src.agent.pattern_registry import resolve_params as _resolve

        tool_results = {}
        tool_sequence = []
        for step in pattern.steps:
            resolved = _resolve(step.params, entities, metadata=metadata)
            tool_fn = TOOL_MAP.get(step.tool_name)
            if not tool_fn:
                log.warning("Fast path: tool %s not found, skipping", step.tool_name)
                continue

            call_id = f"fp_{step.tool_name}_{len(tool_sequence)}"
            tool_sequence.append(step.tool_name)

            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=create_function_call_item(
                    id=call_id,
                    call_id=call_id,
                    name=step.tool_name,
                    arguments=json.dumps(resolved),
                ),
            )

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

        context = json.dumps(tool_results, ensure_ascii=False, indent=2)
        synthesis_system = pattern.synthesis_prompt + PROVENANCE_FORMAT + f"\n\nData:\n{context}"

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

        ci = getattr(request, "custom_inputs", None) or {}
        req_tier = ci.get("user_tier", "")
        req_books = ci.get("permitted_books", "")

        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        question = last_user["content"] if last_user and last_user.get("content") else ""

        # --- Classify + extract (Enron fast path) ---
        classification = classify_and_extract(question) if question else {
            "pattern": "general", "confidence": 0.0, "entities": [],
        }
        pattern_name = classification.get("pattern", "general")
        confidence = classification.get("confidence", 0.0)
        entities = classification.get("entities", [])

        log.info(
            "Classification: pattern=%s confidence=%.2f entities=%d",
            pattern_name, confidence, len(entities),
        )

        pattern = PATTERN_REGISTRY.get(pattern_name)
        if (
            pattern
            and confidence >= pattern.min_confidence
            and CORPUS == "enron"
            and TOOL_MAP
        ):
            log.info("FAST_PATH: %s (confidence=%.2f)", pattern_name, confidence)
            try:
                mlflow.update_current_trace(tags={
                    "question_pattern": pattern_name,
                    "pattern_confidence": str(round(confidence, 2)),
                    "execution_path": "fast",
                    "entities_found": ",".join(e.get("name", "") for e in entities),
                })
            except Exception:
                pass
            fp_metadata = _extract_temporal_metadata(question) if pattern_name == "temporal" else None
            yield from self._execute_fast_path_stream(
                pattern, entities, question,
                tier=req_tier, permitted_books=req_books,
                metadata=fp_metadata,
            )
            return

        # --- Slow path (full ReAct loop) ---
        log.info("SLOW_PATH: pattern=%s (confidence=%.2f below threshold or no pattern)", pattern_name, confidence)
        try:
            mlflow.update_current_trace(tags={
                "question_pattern": pattern_name,
                "pattern_confidence": str(round(confidence, 2)),
                "execution_path": "slow",
                "entities_found": ",".join(e.get("name", "") for e in entities),
            })
        except Exception:
            pass

        names = [e["name"] for e in entities if "name" in e]
        if names:
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
        tool_names_seen = []
        for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
            if event[0] == "updates":
                for node_data in event[1].values():
                    for msg in node_data.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tool_names_seen.append(tc["name"])
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
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.done",
                                item=create_function_call_output_item(
                                    call_id=msg.tool_call_id,
                                    output=str(msg.content),
                                ),
                            )
                        else:
                            yield from output_to_responses_items_stream([msg])

        try:
            mlflow.update_current_trace(tags={
                "tool_sequence": ",".join(tool_names_seen),
            })
        except Exception:
            pass


mlflow.langchain.autolog()
AGENT = GraphRAGAgent()
mlflow.models.set_model(AGENT)
