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

QUERY_ENTITY_PROMPT = _CORPORATE_QUERY_ENTITY_PROMPT if CORPUS == "enron" else _BIBLE_QUERY_ENTITY_PROMPT


def _slugify(name: str) -> str:
    """Same normalisation used during corpus build (src/extraction/extraction.py)."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def extract_query_entities(question: str) -> list[dict]:
    """Call the small LLM to extract entity mentions from a user question."""
    llm = _get_llm(endpoint=SMALL_LLM_ENDPOINT, temperature=0.0, max_tokens=512)
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
    Batches all entity slug + alias patterns into a single SQL query.
    """
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
_ENRON_EMAIL_SUFFIXES = ("_enron_com", "_enron_net", "_ect_enron_com", "_enronxgate_com")


def _humanize_enron_id(eid: str) -> str:
    """Convert email-derived entity IDs like 'andrew_fastow_enron_com' to 'Andrew Fastow'."""
    for suffix in _ENRON_EMAIL_SUFFIXES:
        if eid.endswith(suffix):
            eid = eid[: -len(suffix)]
            break
    return eid.replace("_", " ").title()


def _maybe_humanize(name: str) -> str:
    """Humanize if the name looks like an email-derived entity ID."""
    if any(name.endswith(s) for s in _ENRON_EMAIL_SUFFIXES):
        return _humanize_enron_id(name)
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


@tool
def find_connections(entity_name: str, book: str = "") -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Kenneth Lay")
        book: Optional — filter to a specific book (Bible) or leave empty.
    """
    entity_id = "_".join(entity_name.lower().split())

    eid_pattern = f"%{entity_id}%"
    sql_params = {"eid_pattern": eid_pattern}

    if CORPUS == "enron":
        results = _backend.execute_sql(
            f"SELECT source_name, relationship_type, target_name,"
            f" MAX(description) as description,"
            f" SUM(COALESCE(edge_count, 1)) as frequency"
            f" FROM ("
            f"   SELECT COALESCE(e1.name, r.source_entity) as source_name,"
            f"   r.relationship_type, COALESCE(e2.name, r.target_entity) as target_name,"
            f"   r.description, r.edge_count"
            f"   FROM {RELATIONSHIPS_TABLE} r"
            f"   LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
            f"   LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
            f"   WHERE (r.source_entity LIKE :eid_pattern OR r.target_entity LIKE :eid_pattern)"
            f" ) sub"
            f" GROUP BY source_name, relationship_type, target_name"
            f" ORDER BY frequency DESC"
            " LIMIT 100",
            params=sql_params,
        )
        if not results:
            return f"No connections found for '{entity_name}'."
        return json.dumps(
            _group_connections(entity_name, results, corpus="enron"),
            ensure_ascii=False,
        )

    book_filter = ""
    if book:
        book_filter = " AND r.book = :book"
        sql_params["book"] = book

    results = _backend.execute_sql(
        f"SELECT COALESCE(e1.name, r.source_entity) as source_name,"
        f" r.relationship_type, COALESCE(e2.name, r.target_entity) as target_name,"
        f" r.description, r.book, r.chapter"
        f" FROM {RELATIONSHIPS_TABLE} r"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        f" WHERE (r.source_entity LIKE :eid_pattern OR r.target_entity LIKE :eid_pattern){book_filter}"
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
    eid_a = "_".join(entity_a.lower().split())
    eid_b = "_".join(entity_b.lower().split())

    start_rows = _backend.execute_sql(
        f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
        " WHERE entity_id LIKE :pattern LIMIT 3",
        params={"pattern": f"%{eid_a}%"},
    )
    end_rows = _backend.execute_sql(
        f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
        " WHERE entity_id LIKE :pattern LIMIT 3",
        params={"pattern": f"%{eid_b}%"},
    )

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


LOCAL_TOOLS = [find_entity, find_connections, get_context_verses, get_entity_summary,
               list_entities_by_book, find_cross_book_entities, trace_path, compare_entity_sets]


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
- **find_connections(entity_name, book="")** — find relationships for an entity, optionally filtered by book
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
- **find_connections(entity_name)** — find relationships for an entity (SENT_TO, MANAGES, DISCUSSES, etc.)
- **get_source_emails(entity_name)** — retrieve actual Enron emails mentioning an entity
- **get_entity_summary(entity_name)** — get a comprehensive entity profile with all relationships
- **trace_path(entity_a, entity_b)** — find shortest path between two entities via relationship traversal

## Tool Usage Strategy
- ALWAYS use tools before answering. Prefer graph data over training knowledge.
- For questions about people, use **find_entity** first, then **find_connections** for their network.
- For questions about who communicated with whom, use **find_connections** — SENT_TO relationships show email flows.
- After gathering entity/relationship data, call **get_source_emails** for key entities to ground claims with email evidence.
- For questions about how two people or entities are connected, use **trace_path**.
- For broad entity questions, use **get_entity_summary** for a full profile.
- For multi-entity questions, **call tools multiple times** — once per entity — to build a complete picture.

## Response Guidelines
- **Be direct and comprehensive.** Answer the question fully. Do not restate the question.
- **Prioritize completeness.** Include all relevant findings from the tools.
- **Cite email sources inline** where natural (e.g., date, sender, subject).
- **State coverage limitations** when relevant: "My knowledge graph covers emails from a curated subset of Enron employees."
- If information is not in the knowledge graph, say so honestly rather than guessing.

## Entity Pre-Lookup
Before you received this message, entities from the user's question were automatically looked up in the knowledge graph. Results appear at the END of this system prompt.
- If an entity is listed under "NOT IN GRAPH" and it is the primary subject, state that it is not available.
- Scope terms like "Enron", "the company", "executives" are NOT entity names — ignore if they appear under NOT IN GRAPH.
- Do NOT bridge graph entities to external knowledge (e.g., public news about Enron's collapse) without stating this is outside the graph."""


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
# Agent
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]


class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None, tools=None):
        self.llm = _get_llm(endpoint=endpoint or LLM_ENDPOINT)
        self.tools = tools or GRAPH_TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)

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
        prelookup_context = build_prelookup_context(question) if question else ""

        graph = self._build_graph(prelookup_context, tier=req_tier, permitted_books=req_books)
        for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
            if event[0] == "updates":
                for node_data in event[1].values():
                    for msg in node_data.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
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


mlflow.langchain.autolog()
AGENT = GraphRAGAgent()
mlflow.models.set_model(AGENT)
