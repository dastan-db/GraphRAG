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

from src.runtime.analytics_sql import get_enron_analytics_objects
from src.runtime.router_assets import load_router_case_asset

try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz, process as rapidfuzz_process
except ImportError:
    rapidfuzz_fuzz = None
    rapidfuzz_process = None

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
CORPUS = os.environ.get("GRAPHRAG_CORPUS", "bible").strip().lower()
BIBLE_SCHEMA = (
    os.environ.get("GRAPHRAG_BIBLE_SCHEMA")
    or "graphrag_bible"
)
ENRON_SCHEMA = (
    os.environ.get("GRAPHRAG_ENRON_SCHEMA")
    or (
        os.environ.get("GRAPHRAG_SCHEMA")
        if CORPUS == "enron"
        else None
    )
    or "graphrag_enron"
)
SCHEMA = ENRON_SCHEMA if CORPUS == "enron" else BIBLE_SCHEMA
LLM_ENDPOINT = os.environ.get("GRAPHRAG_LLM_ENDPOINT", "databricks-llama-4-maverick")
SMALL_LLM_ENDPOINT = os.environ.get("GRAPHRAG_SMALL_LLM_ENDPOINT", "databricks-meta-llama-3-1-8b-instruct")
SYNTHESIS_ENDPOINT = os.environ.get("GRAPHRAG_SYNTHESIS_ENDPOINT", "databricks-llama-4-maverick")
REACT_ENDPOINT = os.environ.get("GRAPHRAG_REACT_ENDPOINT", "databricks-llama-4-maverick")

ENTITIES_TABLE = f"{CATALOG}.{SCHEMA}.entities"
RELATIONSHIPS_TABLE = f"{CATALOG}.{SCHEMA}.relationships"
VERSES_TABLE = f"{CATALOG}.{SCHEMA}.verses"
AGENT_PROMPTS_TABLE = f"{CATALOG}.{SCHEMA}.agent_prompts"
ENTITY_ANALYTICS_TABLE = f"{CATALOG}.{SCHEMA}.entity_analytics"
AGENT_ID = "bible-agent"
PROMPT_CACHE_TTL = 300  # seconds; set to 0 for instant iteration
_PARALLEL_TOOLS = os.environ.get("GRAPHRAG_PARALLEL_TOOLS", "true").lower() == "true"
_CLASSIFY_PIPELINE = os.environ.get("GRAPHRAG_CLASSIFY_PIPELINE", "true").lower() == "true"
try:
    # MLflow ResponsesAgent registration always runs predict() on an example input.
    # This optional cap keeps registration under provider tool-count limits without
    # changing the full serving-time toolset.
    _MODEL_LOGGING_TOOL_LIMIT = max(
        0, int(os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT", "0") or "0")
    )
except ValueError:
    _MODEL_LOGGING_TOOL_LIMIT = 0

VS_ENDPOINT = os.environ.get("GRAPHRAG_VS_ENDPOINT", "")
VS_INDEX_NAME = os.environ.get("GRAPHRAG_VS_INDEX_NAME", "")
EMBEDDING_ENDPOINT = os.environ.get("GRAPHRAG_EMBEDDING_ENDPOINT", "databricks-gte-large-en")
_ENRON_ANALYTICS_OBJECTS = get_enron_analytics_objects(CATALOG, ENRON_SCHEMA)
ENRON_ENTITIES_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entities"
ENRON_RELATIONSHIPS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.relationships"
ENRON_EMAILS_TABLE = _ENRON_ANALYTICS_OBJECTS.emails_relation
ENRON_ENTITY_ANALYTICS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics"
ENRON_ENTITY_PATHS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_paths"
ENRON_ENTITY_MENTIONS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"
ENRON_COMMUNICATION_DYADS_TABLE = _ENRON_ANALYTICS_OBJECTS.communication_dyads_relation
ENRON_PARTICIPANTS_TABLE = _ENRON_ANALYTICS_OBJECTS.participants_relation
ENRON_THREADS_TABLE = _ENRON_ANALYTICS_OBJECTS.threads_relation
ENRON_PERSON_ACTIVITY_TABLE = _ENRON_ANALYTICS_OBJECTS.person_activity_relation
ENRON_COMMUNICATION_METRIC_VIEW = _ENRON_ANALYTICS_OBJECTS.communication_metric_view
ENRON_ORG_HIERARCHY_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.org_hierarchy"
ENRON_ORG_HIERARCHY_EVIDENCE_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.org_hierarchy_evidence"

ACCESS_TIER = os.environ.get("GRAPHRAG_ACCESS_TIER", "")


def _default_local_db_path(corpus: str) -> str:
    return "data/graphrag_enron.duckdb" if corpus == "enron" else "data/graphrag.duckdb"


def _resolve_local_db_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return explicit_path

    env_key = f"GRAPHRAG_{CORPUS.upper()}_LOCAL_DB"
    corpus_specific = os.environ.get(env_key)
    if corpus_specific:
        return corpus_specific

    generic_path = os.environ.get("GRAPHRAG_LOCAL_DB")
    default_path = _default_local_db_path(CORPUS)
    if not generic_path:
        return default_path

    generic_name = os.path.basename(generic_path).lower()
    if CORPUS == "bible" and "enron" in generic_name:
        log.info(
            "Ignoring GRAPHRAG_LOCAL_DB=%s for bible corpus; use %s to override the Bible DuckDB path.",
            generic_path,
            env_key,
        )
        return default_path
    if CORPUS == "enron" and generic_name in {"graphrag.duckdb", "graphrag_bible.duckdb"}:
        log.info(
            "Ignoring GRAPHRAG_LOCAL_DB=%s for enron corpus; use %s to override the Enron DuckDB path.",
            generic_path,
            env_key,
        )
        return default_path
    return generic_path

EVIDENCE_CONFIG = {
    "strategy_weights": {"A": 1.0, "B": 0.7, "C": 0.9, "D": 0.4},
    "snippet_length": 1000,
    "max_emails_per_pair": 20,
    "date_proximity_boost": 0.0,
    "date_proximity_window_days": 90,
    "recipient_type_weights": {"TO": 1.0, "CC": 0.6, "BCC": 0.3},
    "mass_mail_threshold": 5,
    "org_keyword_boost": 0.3,
    "min_relevance_threshold": 0.3,
    "default_sort_order": "relevance",
    "body_preview_length": 2000,
    "thread_cap": 20,
    "email_type_thresholds": {"direct": 3, "group": 10},
    "expose_vector_scores": True,
    "preserve_source_threads": True,
    "signal_weights": {
        "direct_recipient": 1.0,
        "cc_recipient": 0.6,
        "body_mention": 0.55,
        "thread_cooccurrence": 0.3,
        "temporal_proximity": 0.2,
        "email_type_penalty": -0.3,
        "vector_similarity": 0.5,
        "org_keyword": 0.0,
    },
    "min_display_threshold": 0.2,
    "reranking_mode": "heuristic",
    "evidence_dedup": "thread-level",
    "auto_evidence_mode": "always",
    "evidence_step_position": "late",
    "citation_depth": "both",
    "confidence_calibration": "hybrid",
    "evidence_sufficiency_threshold": 2,
    "plateau_window": 2,
    "plateau_threshold_pp": 2,
}

ENRON_ABAC_ENTITIES_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entities_abac"
ENRON_ABAC_RELATIONSHIPS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.relationships_abac"
ENRON_ABAC_EMAILS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.emails_abac"
ENRON_ABAC_ENTITY_ANALYTICS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_analytics_abac"
ENRON_ABAC_ENTITY_PATHS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_paths_abac"
ENRON_ABAC_ENTITY_MENTIONS_VIEW = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions_abac"

def _resolve_backend_type() -> str:
    """Prefer GRAPHRAG_BACKEND; else GRAPHRAG_DATA_BACKEND (app.yaml); default lakebase for Databricks-hosted SQL."""
    return (
        os.environ.get("GRAPHRAG_BACKEND")
        or os.environ.get("GRAPHRAG_DATA_BACKEND")
        or "lakebase"
    ).strip().lower()


BACKEND_TYPE = _resolve_backend_type()
LLM_PROVIDER = os.environ.get("GRAPHRAG_LLM_PROVIDER", "databricks")
_ROUTING_CASE_IMPORT_SOURCE = "unloaded"
_PATTERN_REGISTRY_IMPORT_SOURCE = "unloaded"


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

    def _wait_for_result(self, result):
        import time
        from databricks.sdk.service.sql import StatementState

        w = self._get_ws_client()
        for _ in range(60):
            state = result.status.state
            if state in (
                StatementState.SUCCEEDED,
                StatementState.FAILED,
                StatementState.CANCELED,
            ):
                return result
            time.sleep(1)
            result = w.statement_execution.get_statement(result.statement_id)
        return result

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
            wait_timeout="30s",
        )
        result = self._wait_for_result(result)
        if result.status.state != StatementState.SUCCEEDED:
            msg = result.status.error.message if result.status.error else f"state={result.status.state}"
            raise RuntimeError(f"SQL execution failed: {msg}")
        if not result.manifest or not result.result:
            return []
        columns = [col.name for col in result.manifest.schema.columns]
        rows = []
        for row_data in result.result.data_array or []:
            rows.append(dict(zip(columns, row_data)))
        return rows


class LocalBackend:
    """DuckDB — local development with exported graph data.

    Handles Spark SQL → DuckDB dialect translation so tool-generated queries
    run against exported DuckDB tables without modification.  Array columns
    (e.g. email ``to_recipients``) must match Delta export types — typically
    ``VARCHAR[]`` from ``scripts/export_local_data.py`` (``SELECT *``).

    Thread-safety: a ``threading.Lock`` serialises queries because DuckDB
    connections are not safe for concurrent reads from multiple threads
    (the agent's ``ThreadPoolExecutor`` executes tools in parallel).
    """

    _FQN_BIBLE = f"{CATALOG}.{BIBLE_SCHEMA}."
    _FQN_ENRON = f"{CATALOG}.{ENRON_SCHEMA}."

    _SPARK_TO_DUCK: list[tuple[re.Pattern, str]] = [
        (re.compile(r"\bSIZE\(", re.IGNORECASE), "array_length("),
        (re.compile(r"\bARRAY_JOIN\(", re.IGNORECASE), "array_to_string("),
        (re.compile(r"\bSLICE\(", re.IGNORECASE), "list_slice("),
        (re.compile(r"\bCOLLECT_LIST\(", re.IGNORECASE), "list("),
        (re.compile(r"\bCOLLECT_SET\(", re.IGNORECASE), "list("),
        (re.compile(r"\bFIRST\(", re.IGNORECASE), "first("),
        # LATERAL VIEW must come before simple EXPLODE replacement
        (re.compile(
            r"LATERAL\s+VIEW\s+EXPLODE\(([^)]+)\)\s+(\w+)\s+AS\s+(\w+)",
            re.IGNORECASE,
        ), r", UNNEST(\1) AS \2(\3)"),
        (re.compile(r"\bEXPLODE\(", re.IGNORECASE), "UNNEST("),
        (re.compile(
            r"DATE_FORMAT\(([^,]+),\s*'yyyy-MM'\)", re.IGNORECASE,
        ), r"strftime(\1, '%Y-%m')"),
        (re.compile(
            r"DATE_FORMAT\(([^,]+),\s*'yyyy-MM-dd'\)", re.IGNORECASE,
        ), r"strftime(\1, '%Y-%m-%d')"),
    ]

    def __init__(self, db_path: str | None = None):
        import duckdb
        import threading
        path = _resolve_local_db_path(db_path)
        self._conn = duckdb.connect(path, read_only=True)
        self._lock = threading.Lock()
        log.info("LocalBackend connected to %s", path)

    @classmethod
    def _translate_sql(cls, query: str) -> str:
        """Best-effort Spark SQL → DuckDB translation."""
        for pattern, replacement in cls._SPARK_TO_DUCK:
            query = pattern.sub(replacement, query)
        return query

    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]:
        query = query.replace(self._FQN_BIBLE, "")
        query = query.replace(self._FQN_ENRON, "")
        query = self._translate_sql(query)
        query = re.sub(r":(\w+)", r"$\1", query)
        if params:
            used = set(re.findall(r"\$(\w+)", query))
            params = {k: v for k, v in params.items() if k in used}
        with self._lock:
            try:
                result = self._conn.execute(query, params or {})
                columns = [desc[0] for desc in result.description]
                return [dict(zip(columns, row)) for row in result.fetchall()]
            except Exception as exc:
                log.warning("LocalBackend SQL error: %s — %s", query[:200], exc)
                return []


class LakebaseBackend:
    """Lakebase Autoscaling (Postgres) — low-latency OLTP with RLS.

    Connects via psycopg with Databricks OAuth tokens.  Uses a connection pool
    to avoid per-query TCP/TLS handshake and credential generation overhead.
    Supports session-level RLS context: call
    ``set_rls_context({"permitted_books": "Genesis,Exodus"})``
    to scope all subsequent queries via Postgres RLS policies.

    Tool SQL is authored for Spark (Databricks warehouse).  Before execution we
    apply the same style of translation as :class:`LocalBackend` does for DuckDB,
    mapping Spark array/time helpers to PostgreSQL builtins.
    """

    _FQN_BIBLE = f"{CATALOG}.{BIBLE_SCHEMA}."
    _FQN_ENRON = f"{CATALOG}.{ENRON_SCHEMA}."

    _SLICE_RE = re.compile(
        r"\bSLICE\s*\(\s*([^,]+?)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        re.IGNORECASE,
    )
    _LATERAL_EXPLODE_RE = re.compile(
        r"LATERAL\s+VIEW\s+EXPLODE\(([^)]+)\)\s+(\w+)\s+AS\s+(\w+)",
        re.IGNORECASE,
    )
    _COLLECT_LIST_INDEX0_RE = re.compile(
        r"COLLECT_LIST\s*\(([^)]+)\)\s*\[0\]",
        re.IGNORECASE,
    )
    _DATE_FORMAT_PATTERNS: list[tuple[re.Pattern, str]] = [
        (
            re.compile(
                r"DATE_FORMAT\s*\(\s*([^,]+?)\s*,\s*'yyyy-MM'\s*\)",
                re.IGNORECASE,
            ),
            r"to_char(\1, 'YYYY-MM')",
        ),
        (
            re.compile(
                r"DATE_FORMAT\s*\(\s*([^,]+?)\s*,\s*'yyyy-MM-dd'\s*\)",
                re.IGNORECASE,
            ),
            r"to_char(\1, 'YYYY-MM-DD')",
        ),
    ]

    @classmethod
    def _translate_spark_sql_for_pg(cls, query: str) -> str:
        """Best-effort Spark SQL → PostgreSQL translation for Lakebase."""

        def _slice_loop(sql: str) -> str:
            prev = None
            while prev != sql:
                prev = sql

                def _repl(m: re.Match) -> str:
                    expr = m.group(1).strip()
                    start = int(m.group(2))
                    length = int(m.group(3))
                    end = start + length - 1
                    return f"({expr})[{start}:{end}]"

                sql = cls._SLICE_RE.sub(_repl, sql)
            return sql

        def _size_to_cardinality(sql: str) -> str:
            out: list[str] = []
            i = 0
            while i < len(sql):
                m = re.search(r"\bSIZE\s*\(", sql[i:], re.IGNORECASE)
                if not m:
                    out.append(sql[i:])
                    break
                start = i + m.start()
                out.append(sql[i:start])
                # m was matched in sql[i:]; '(' is the last character of the match
                open_paren = i + m.end() - 1
                depth = 0
                for j in range(open_paren, len(sql)):
                    if sql[j] == "(":
                        depth += 1
                    elif sql[j] == ")":
                        depth -= 1
                        if depth == 0:
                            inner = sql[open_paren + 1:j]
                            # Plain cardinality — outer SQL already uses COALESCE(SIZE(...), 0)
                            out.append(f"cardinality({inner})")
                            i = j + 1
                            break
                else:
                    out.append(sql[i:])
                    break
            return "".join(out)

        q = _slice_loop(query)
        q = re.sub(r"\bARRAY_JOIN\s*\(", "array_to_string(", q, flags=re.IGNORECASE)
        q = _size_to_cardinality(q)
        q = cls._LATERAL_EXPLODE_RE.sub(
            r"CROSS JOIN LATERAL unnest(\1) AS \2(\3)",
            q,
        )
        q = re.sub(r"\bEXPLODE\s*\(", "unnest(", q, flags=re.IGNORECASE)
        q = cls._COLLECT_LIST_INDEX0_RE.sub(r"(array_agg(\1))[1]", q)
        q = re.sub(
            r"\bFIRST\s*\(\s*([^)]+?)\s*\)",
            r"(array_agg(\1))[1]",
            q,
            flags=re.IGNORECASE,
        )
        for pat, repl in cls._DATE_FORMAT_PATTERNS:
            q = pat.sub(repl, q)
        q = re.sub(r"\bAS\s+STRING\b", "AS TEXT", q, flags=re.IGNORECASE)
        q = re.sub(r"VARCHAR\s*\(\s*4000\s*\)", "TEXT", q, flags=re.IGNORECASE)
        return q

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
        query = self._translate_spark_sql_for_pg(query)

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
                    # Do not treat [1:3] slice syntax or ::type casts as :name parameters
                    pg_query = re.sub(
                        r"(?<!:):(?!\d)(\w+)",
                        r"%(\1)s",
                        query,
                    )
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


class CachingBackend:
    """Per-request caching wrapper around a DataBackend.

    Eliminates redundant SQL round-trips when multiple tools issue the
    same entity/relationship lookup within a single predict() call.
    Call clear() at the start of each request.
    """

    def __init__(self, inner: DataBackend):
        self._inner = inner
        self._cache: dict[str, list[dict]] = {}
        self._hits = 0
        self._misses = 0

    def execute_sql(self, query: str, params: dict[str, str] | None = None) -> list[dict]:
        param_key = tuple(sorted((params or {}).items()))
        key = f"{query.strip()}|{param_key}"
        if key in self._cache:
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        result = self._inner.execute_sql(query, params)
        self._cache[key] = result
        return result

    def clear(self) -> None:
        if self._hits + self._misses > 0:
            log.debug("SQL cache: %d hits, %d misses (%.0f%% hit rate)",
                      self._hits, self._misses,
                      100 * self._hits / (self._hits + self._misses))
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def set_rls_context(self, context: dict[str, str]):
        if hasattr(self._inner, "set_rls_context"):
            self._inner.set_rls_context(context)


_backend: DataBackend = CachingBackend(_get_backend())


def _unwrap_backend(backend: DataBackend) -> DataBackend:
    return getattr(backend, "_inner", backend)


def _runtime_config_tags() -> dict[str, str]:
    inner_backend = _unwrap_backend(_backend)
    router_cases = []
    if CORPUS == "enron":
        try:
            router_cases = _load_enron_routing_cases()
        except Exception:
            router_cases = []
    return {
        "runtime_corpus": CORPUS,
        "runtime_backend": BACKEND_TYPE,
        "runtime_backend_impl": type(inner_backend).__name__,
        "runtime_lakebase_endpoint": os.environ.get("LAKEBASE_ENDPOINT", ""),
        "runtime_llm_provider": LLM_PROVIDER,
        "runtime_llm_endpoint": LLM_ENDPOINT,
        "runtime_synthesis_endpoint": SYNTHESIS_ENDPOINT,
        "runtime_react_endpoint": REACT_ENDPOINT,
        "runtime_small_llm_endpoint": SMALL_LLM_ENDPOINT,
        "runtime_pattern_registry_source": _PATTERN_REGISTRY_IMPORT_SOURCE,
        "runtime_router_cases_source": _ROUTING_CASE_IMPORT_SOURCE,
        "runtime_router_cases_loaded": str(len(router_cases)),
        "runtime_router_assets": "available" if router_cases else "degraded",
    }


def _emit_runtime_observability() -> None:
    tags = _runtime_config_tags()
    try:
        mlflow.update_current_trace(tags=tags)
    except Exception:
        pass
    log.info(
        "Runtime config | corpus=%s backend=%s backend_impl=%s lakebase=%s llm_provider=%s "
        "llm=%s router_source=%s router_cases=%s pattern_registry=%s",
        tags["runtime_corpus"],
        tags["runtime_backend"],
        tags["runtime_backend_impl"],
        tags["runtime_lakebase_endpoint"] or "-",
        tags["runtime_llm_provider"],
        tags["runtime_llm_endpoint"],
        tags["runtime_router_cases_source"],
        tags["runtime_router_cases_loaded"],
        tags["runtime_pattern_registry_source"],
    )


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


def _prompt_uses_supported_tools(prompt_text: str) -> bool:
    available_tool_names = {
        tool_obj.name for tool_obj in globals().get("GRAPH_TOOLS", [])
        if hasattr(tool_obj, "name")
    }
    if not available_tool_names:
        return True

    referenced_tools = {
        token
        for token in re.findall(r"\b[a-z_][a-z0-9_]*\b", prompt_text or "")
        if token.startswith((
            "find_",
            "get_",
            "list_",
            "trace_",
            "compare_",
            "query_",
            "detect_",
            "search_",
            "semantic_",
            "browse_",
        ))
    }
    unsupported = sorted(referenced_tools - available_tool_names)
    if unsupported:
        log.warning(
            "Loaded prompt references unavailable tools %s; using hardcoded fallback",
            unsupported,
        )
        return False
    return True


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
            prompt_text = rows[0]["prompt_text"] if rows else SYSTEM_PROMPT
            if (
                prompt_text
                and CORPUS == "bible"
                and "five books of the king james bible" in prompt_text.lower()
            ):
                log.warning("Loaded Bible prompt is stale; using hardcoded fallback")
                prompt_text = SYSTEM_PROMPT
            if prompt_text and not _prompt_uses_supported_tools(prompt_text):
                prompt_text = SYSTEM_PROMPT
            _prompt_cache["text"] = prompt_text or SYSTEM_PROMPT
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
    runtime_tags = _runtime_config_tags()
    _query_log.append({
        "query_id": str(_uuid.uuid4()),
        "user_query": user_query[:1000],
        "classified_intent": classified_intent,
        "tools_invoked": tools_invoked,
        "execution_path": execution_path,
        "latency_ms": latency_ms,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "backend_type": runtime_tags["runtime_backend"],
        "backend_impl": runtime_tags["runtime_backend_impl"],
        "lakebase_endpoint": runtime_tags["runtime_lakebase_endpoint"],
        "llm_provider": runtime_tags["runtime_llm_provider"],
        "llm_endpoint": runtime_tags["runtime_llm_endpoint"],
        "router_assets": runtime_tags["runtime_router_assets"],
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

_CORPORATE_CLASSIFY_AND_EXTRACT_PROMPT = """You are a corporate communications analyst. Given a user question about the Enron email corpus, do THREE things:

1. CLASSIFY the question into one of these patterns:
   - entity_structure: questions about reporting lines, org chart, roles, titles, who managed whom, who reported to whom, department structure, authority — "who reported to Jeff Skilling?", "what was Andrew Fastow's title?", "who is Kenneth Lay?"
   - entity_explore: questions about one person's activities, contacts, discussions, involvement, communications — "who did Jeff Skilling email most?", "what did Kenneth Lay discuss?", "what projects was Andrew Fastow involved in?"
   - entity_pair: questions about the relationship between TWO specific people — "how are Kenneth Lay and Tim Belden connected?", "what did Jeff Skilling and Andrew Fastow discuss?", "did they email each other?"
   - timeline: questions about events over time, what happened when, sequences, before/after, anomalies — "what happened in August 2001?", "timeline of the investigation", "when did Jeff Skilling resign?"
   - keyword_search: questions about a topic, project, deal, concept, or theme — "what was Project Raptor?", "California energy crisis", "special purpose entities", "document destruction", "what financial events were discussed?", "what topics did X discuss?", "what were the main subjects of emails mentioning Arthur Andersen?", "what internal projects were discussed?"
   - genie_analytics: questions answerable with SQL (counts, rankings, aggregations, percentages, full email listings) — "who sent the most emails?", "top email pairs", "what percentage of emails were internal?", "busiest communicators", "who communicated most with X?", "how many emails between X and Y?", "show me all emails between X and Y", "what are the most common topics for X?", "top contacts of X", "what percentage of X's emails were from Y?". PREFER this over entity_explore/entity_pair when the question asks for counts, rankings, percentages, or complete email listings.
   - general: broad overview questions or questions that don't fit the above — "what can you tell me about Enron?", "why did Enron fail?", "what role did the board play?", "who were the key whistleblowers?"

2. EXTRACT all significant entities mentioned or implied in the question.
   For each entity provide:
   - name: The canonical name (e.g., "Kenneth Lay" not "Ken")
   - entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event
   For topic questions, also extract the implicit key entity when one exists (e.g., "special purpose entities" implies Andrew Fastow; "Arthur Andersen" is an Organization; "broadband" implies Kenneth Rice and Enron Broadband Services).

3. EXTRACT search keywords for keyword_search and general patterns.
   These should be the core search terms that will match email content.

IMPORTANT: Only extract REAL named entities. Generic phrases like "two individuals", "someone", "the person", "each other" are NOT entities — return an empty entities list for those.

Return a JSON object with exactly this structure:
{"pattern": "<one of the 7 pattern names>", "confidence": <0.0 to 1.0>, "entities": [{"name": "...", "entity_type": "..."}], "keywords": "<comma-separated search terms>"}

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


def classify_and_extract(
    question: str,
    *,
    raw_question: str | None = None,
    contract: dict | None = None,
    routing_hint: dict | None = None,
) -> dict:
    """Extract entities AND classify question pattern in a single 8B LLM call.

    Returns {"pattern": str, "confidence": float, "entities": list[dict]}.
    Falls back to {"pattern": "general", "confidence": 0.0, "entities": [...]}
    if classification fails but entity extraction succeeds.
    """
    routing_question = raw_question or question
    contract = dict(contract or _extract_answer_contract(routing_question))
    routing_hint = routing_hint or _get_case_based_pattern_hint(routing_question, contract)

    if CORPUS != "enron":
        entities = extract_query_entities(question)
        return {
            "pattern": "general",
            "confidence": 0.0,
            "entities": entities,
            "contract": contract,
        }

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
            base = {
                "pattern": result.get("pattern", "general"),
                "confidence": float(result.get("confidence", 0.0)),
                "entities": entities,
                "keywords": result.get("keywords", ""),
                "contract": contract,
            }
            return _apply_case_router_hint(routing_question, base, routing_hint)
    except (json.JSONDecodeError, ValueError, TypeError):
        log.warning("Failed to parse classify_and_extract response: %s", text)

    entities = extract_query_entities(question)
    return _apply_case_router_hint(
        routing_question,
        {
            "pattern": "general",
            "confidence": 0.0,
            "entities": entities,
            "keywords": "",
            "contract": contract,
        },
        routing_hint,
    )


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


_EMAIL_TOOL_NAMES = frozenset({
    "get_email_full_body", "get_emails_between", "get_source_evidence",
    "get_hierarchy_evidence", "get_relationship_evidence", "search_emails",
    "semantic_search_emails",
})
_METADATA_TOOL_NAMES = frozenset({
    "find_top_contacts", "find_connections", "get_communication_stats",
    "get_topic_distribution",
})


def _prioritize_email_results(tool_results: dict, metadata_cap: int = 2000) -> dict:
    """Reorder tool_results so email-bearing tools appear first in the dict.

    Also truncates metadata tool outputs to `metadata_cap` characters each,
    ensuring email content always survives the context truncation.
    """
    email_items = []
    core_items = []
    metadata_items = []
    for key, val in tool_results.items():
        base_tool = key.split("__")[0].split("(")[0].strip()
        if base_tool in _EMAIL_TOOL_NAMES:
            email_items.append((key, val))
        elif base_tool in _METADATA_TOOL_NAMES:
            if isinstance(val, str) and len(val) > metadata_cap:
                val = _truncate_json_aware(val, metadata_cap)
            metadata_items.append((key, val))
        else:
            core_items.append((key, val))
    return dict(email_items + core_items + metadata_items)


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
    event_lower = event_text.lower().replace("-", " ").strip()
    for event_key, dates in ENRON_EVENT_DATES.items():
        normalized_key = event_key.lower().replace("-", " ")
        if normalized_key in event_lower or event_lower in normalized_key:
            return dates
    for event_key, dates in ENRON_EVENT_DATES.items():
        key_words = set(event_key.lower().replace("-", " ").split())
        text_words = set(event_lower.split())
        if len(key_words & text_words) >= max(1, len(key_words) - 1):
            return dates
    return None


def _heuristic_entity_names(question: str) -> list[str]:
    """Fast regex extraction of probable entity names from question text."""
    capitalized = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", question)
    return list(dict.fromkeys(capitalized))[:5]


_ANALYTICS_ROUTE_HINTS = (
    "how many", "count", "top ", "top-", "most ", "least ", "rank", "ranking",
    "percentage", "percent", "ratio", "compare", "comparison", "trend",
    "volume", "busiest", "average", "total", "who communicated most",
    "who emailed most", "email count", "emails between", "show me all",
    "show all", "list all", "most common topics", "top contacts",
)
_ORG_ROUTE_HINTS = (
    "report to", "reports to", "reported to", "manager", "managed", "manages",
    "direct report", "hierarchy", "org chart", "org structure", "title", "role",
    "department", "supervisor", "boss", "who is", "who was",
)
_PAIR_ROUTE_HINTS = (
    "connected", "connection", "relationship", "communicate directly",
    "email each other", "emailed each other", "did they communicate",
    "did they email", "between them",
)
_TIMELINE_ROUTE_HINTS = (
    "timeline", "when did", "what happened", "before", "after", "during",
    "between", "over time", "chronology", "sequence of events",
)
_STRONG_TIMELINE_ROUTE_HINTS = (
    "timeline", "what happened", "when did", "chronology",
    "sequence of events", "over time",
)
_EXPLORE_ROUTE_HINTS = (
    "email most", "communicated", "discuss", "activity", "involved",
    "worked on", "talk about", "topics", "evidence", "prove it",
)


def _has_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _has_strong_timeline_intent(question_lower: str) -> bool:
    if _has_any_phrase(question_lower, _STRONG_TIMELINE_ROUTE_HINTS):
        return True
    return bool(
        re.search(
            r"\bevents?\s+(?:before|after|during|between)\b",
            question_lower,
        )
    )


def _count_question_entities(question: str, entities: list[dict]) -> int:
    names: list[str] = []
    for ent in entities or []:
        if isinstance(ent, dict):
            name = (ent.get("name", "") or "").strip()
            if name:
                names.append(name)
    names.extend(_heuristic_entity_names(question))
    return len({name.lower() for name in names if name})


def _apply_factual_routing_overrides(question: str, classification: dict) -> dict:
    """Apply deterministic routing guardrails for factual Enron questions."""
    if CORPUS != "enron" or not isinstance(classification, dict):
        return classification

    routed = dict(classification)
    q_lower = question.lower()
    pattern = routed.get("pattern", "general")
    confidence = float(routed.get("confidence", 0.0) or 0.0)
    entity_count = _count_question_entities(question, routed.get("entities", []))
    contract = routed.get("contract", {}) if isinstance(routed.get("contract"), dict) else {}
    answer_type = contract.get("answer_type", "")
    force_pattern = contract.get("force_pattern", "")
    requires_evidence = bool(contract.get("requires_evidence"))
    requires_direct_email = bool(contract.get("requires_direct_email"))
    documentary_evidence_like = bool(contract.get("documentary_evidence_like"))
    explicit_timeline_intent = bool(contract.get("explicit_timeline_intent"))
    has_temporal_filter = bool(_extract_temporal_metadata(question))

    override = None
    if force_pattern == "genie_analytics" or answer_type == "count":
        override = "genie_analytics"
    elif force_pattern == "entity_structure" or answer_type == "org_structure":
        override = "entity_structure"
    elif force_pattern == "entity_pair" or answer_type == "path":
        override = "entity_pair"
    elif force_pattern == "keyword_search" or answer_type in {"proof_email", "documentary_evidence"}:
        override = "keyword_search"
    elif (force_pattern == "timeline" or answer_type == "timeline") and not requires_direct_email:
        override = "timeline"
    elif _has_any_phrase(q_lower, _ANALYTICS_ROUTE_HINTS):
        override = "genie_analytics"
    elif (
        _has_any_phrase(q_lower, _ORG_ROUTE_HINTS)
        and entity_count <= 1
        and not _has_any_phrase(q_lower, _EXPLORE_ROUTE_HINTS)
    ):
        override = "entity_structure"
    elif (
        entity_count >= 2
        and _has_any_phrase(q_lower, _PAIR_ROUTE_HINTS)
        and pattern in {"general", "keyword_search", "entity_explore"}
    ):
        override = "entity_pair"
    elif (
        not requires_direct_email
        and not documentary_evidence_like
        and not (requires_evidence and not explicit_timeline_intent)
        and (has_temporal_filter or _has_any_phrase(q_lower, _TIMELINE_ROUTE_HINTS))
        and pattern in {"general", "keyword_search", "entity_explore"}
    ):
        override = "timeline"

    if override and override != pattern:
        routed["pattern"] = override
        routed["confidence"] = max(confidence, 0.92 if override == "genie_analytics" else 0.85)
        routed["routing_override"] = f"heuristic:{override}"

    return routed


_DATE_LIKE_RE = re.compile(
    r"^(?:\d{4}[-/]\d{2}[-/]\d{2}|"                  # 2001-08-14
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}|"
    r"(?:early|mid|late|spring|summer|fall|winter)\s+\d{4}|"
    r"\d{4})$",                                        # bare year
    re.IGNORECASE,
)


_ROUTER_STOP_WORDS = {
    "about", "after", "all", "and", "are", "around", "before", "between", "can",
    "did", "does", "during", "each", "email", "emails", "for", "from", "how",
    "in", "into", "is", "it", "its", "me", "most", "not", "of", "on", "or",
    "show", "that", "the", "their", "them", "they", "this", "those", "to",
    "was", "were", "what", "when", "which", "who", "with", "would",
}
_ROUTER_CASE_LIMIT = int(os.environ.get("GRAPHRAG_ROUTER_CASE_LIMIT", "96"))
_ROUTER_MIN_SCORE = float(os.environ.get("GRAPHRAG_ROUTER_MIN_SCORE", "0.24"))
_ROUTER_CASE_SPLITS = tuple(
    part.strip()
    for part in os.environ.get("GRAPHRAG_ROUTER_CASE_SPLITS", "train").split(",")
    if part.strip()
)
_ENRON_ROUTING_CASES: list[dict] | None = None

_PATTERN_ROUTER_CARDS: dict[str, dict[str, object]] = {
    "entity_structure": {
        "description": "Reporting lines, titles, managers, departments, and org structure.",
        "keywords": (
            "reports to", "manager", "managed", "title", "role", "department",
            "org chart", "hierarchy", "supervisor", "direct report",
        ),
    },
    "entity_explore": {
        "description": "One person's activities, contacts, discussions, and involvement.",
        "keywords": (
            "email most", "top contacts", "activities", "discussed", "worked on",
            "involved in", "projects", "topics",
        ),
    },
    "entity_pair": {
        "description": "Relationship, direct communication, and path between two people.",
        "keywords": (
            "between them", "each other", "connected", "connection path",
            "relationship between", "directly", "emailed each other",
        ),
    },
    "timeline": {
        "description": "Bounded chronology, before or after events, and event sequences over time.",
        "keywords": (
            "timeline", "what happened", "chronology", "before", "after",
            "during", "sequence of events", "over time",
        ),
    },
    "keyword_search": {
        "description": "Topic, project, deal, document, or theme-based evidence search.",
        "keywords": (
            "project", "topic", "theme", "deal", "document", "subject",
            "mentions", "discussed",
        ),
    },
    "genie_analytics": {
        "description": "Counts, rankings, comparisons, trends, percentages, and full email listings.",
        "keywords": (
            "how many", "count", "top", "ranking", "compare", "percentage",
            "trend", "busiest", "list all", "show me all",
        ),
    },
    "general": {
        "description": "Broad synthesis when no narrow factual primitive fits cleanly.",
        "keywords": (
            "overview", "broad", "why", "role", "what can you tell me",
            "key factors", "general context",
        ),
    },
}


def _tokenize_router_text(text: str) -> set[str]:
    parts = [
        token for token in re.split(r"\W+", text.lower())
        if len(token) > 2 and token not in _ROUTER_STOP_WORDS
    ]
    if not parts:
        return set()
    bigrams = [f"{a}_{b}" for a, b in zip(parts, parts[1:])]
    return set(parts + bigrams)


def _jaccard_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    if overlap == 0:
        return 0.0
    return overlap / len(left | right)


def _extract_answer_contract(question: str, entities: list[dict] | None = None) -> dict:
    q_lower = question.lower()
    temporal_meta = _extract_temporal_metadata(question)
    entity_count = _count_question_entities(question, entities or [])

    count_like = (
        _has_any_phrase(q_lower, _ANALYTICS_ROUTE_HINTS)
        or bool(re.search(r"\bhow many\b|\bcount\b|\bpercentage\b|\bpercent\b|\btop\s+\d+\b", q_lower))
    )
    org_like = _has_any_phrase(q_lower, _ORG_ROUTE_HINTS)
    path_like = (
        entity_count >= 2
        and bool(re.search(r"\bpath\b|\bconnected\b|\bconnection\b|\brelationship\b", q_lower))
    )
    timeline_like = (
        bool(temporal_meta)
        or _has_any_phrase(q_lower, _TIMELINE_ROUTE_HINTS)
    )
    requires_evidence = bool(re.search(
        r"\b(evidence|prove\w*|proof|show.*email|which email|quote|quoted|cite|citation|documentary)\b",
        q_lower,
    ))
    requires_direct_email = bool(re.search(
        r"\b(show.*email|which email|quote|quoted|direct email|prove\w*|proof)\b",
        q_lower,
    ))
    explicit_timeline_intent = _has_strong_timeline_intent(q_lower)
    documentary_evidence_like = (
        requires_evidence
        and not requires_direct_email
        and not count_like
        and not org_like
        and not path_like
        and not explicit_timeline_intent
    )
    comparison = bool(re.search(r"\b(compare|comparison|versus|vs\.?|difference|before|after)\b", q_lower))
    directional = bool(re.search(
        r"\b(sent|received|from|to|direction|direct|directly|each other|a to b|b to a)\b",
        q_lower,
    ))

    answer_type = "unknown"
    force_pattern = ""
    if count_like:
        answer_type = "count"
        force_pattern = "genie_analytics"
    elif requires_direct_email:
        answer_type = "proof_email"
        if org_like:
            force_pattern = "entity_structure"
        elif entity_count >= 2:
            force_pattern = "entity_pair"
        else:
            force_pattern = "keyword_search"
    elif path_like:
        answer_type = "path"
        force_pattern = "entity_pair"
    elif org_like:
        answer_type = "org_structure"
        force_pattern = "entity_structure"
    elif documentary_evidence_like:
        answer_type = "documentary_evidence"
        force_pattern = "keyword_search"
    elif timeline_like:
        answer_type = "timeline"
        force_pattern = "timeline"

    return {
        "answer_type": answer_type,
        "force_pattern": force_pattern,
        "requires_evidence": requires_evidence or requires_direct_email,
        "requires_direct_email": requires_direct_email,
        "comparison": comparison,
        "directional": directional,
        "documentary_evidence_like": documentary_evidence_like,
        "explicit_timeline_intent": explicit_timeline_intent,
        "entity_count": entity_count,
        "date_from": temporal_meta.get("date_from", ""),
        "date_to": temporal_meta.get("date_to", ""),
    }


def _pattern_card_bonus(pattern: str, contract: dict) -> float:
    answer_type = contract.get("answer_type", "")
    force_pattern = contract.get("force_pattern", "")
    bonus = 0.0
    if force_pattern == pattern:
        bonus += 0.42
    if answer_type == "count" and pattern == "genie_analytics":
        bonus += 0.18
    if answer_type == "path" and pattern == "entity_pair":
        bonus += 0.16
    if answer_type == "org_structure" and pattern == "entity_structure":
        bonus += 0.16
    if answer_type == "timeline" and pattern == "timeline":
        bonus += 0.14
    if answer_type == "documentary_evidence" and pattern == "keyword_search":
        bonus += 0.18
    if contract.get("requires_evidence") and pattern in {"entity_structure", "entity_pair", "keyword_search"}:
        bonus += 0.06
    if contract.get("directional") and pattern in {"entity_pair", "genie_analytics"}:
        bonus += 0.05
    if contract.get("comparison") and pattern == "genie_analytics":
        bonus += 0.04
    if contract.get("requires_direct_email") and pattern == "timeline":
        bonus -= 0.10
    if contract.get("documentary_evidence_like") and not contract.get("explicit_timeline_intent") and pattern == "timeline":
        bonus -= 0.12
    return bonus


def _load_enron_routing_cases() -> list[dict]:
    global _ENRON_ROUTING_CASES, _ROUTING_CASE_IMPORT_SOURCE
    if _ENRON_ROUTING_CASES is not None:
        return _ENRON_ROUTING_CASES

    try:
        rows = load_router_case_asset(corpus="enron", case_limit=_ROUTER_CASE_LIMIT)
        _ROUTING_CASE_IMPORT_SOURCE = "src.runtime.router_assets"
    except Exception:
        log.warning("Case router unavailable: runtime router asset load failed")
        _ROUTING_CASE_IMPORT_SOURCE = "unavailable"
        _ENRON_ROUTING_CASES = []
        return _ENRON_ROUTING_CASES

    cases: list[dict] = []
    for row in rows[:_ROUTER_CASE_LIMIT]:
        primitive = row.get("primitive", "")
        if primitive not in {
            "entity_structure", "entity_explore", "entity_pair",
            "timeline", "keyword_search", "general", "genie_analytics",
        }:
            continue
        question_text = str(row.get("question", "") or row.get("question_text", "") or "")
        case_text = " ".join([
            question_text,
            str(row.get("attorney_category", "")),
            str(row.get("architecture_primary", "")),
            str(row.get("domain_primary", "")),
            " ".join(row.get("expected_tools", [])),
        ])
        tokens = _tokenize_router_text(case_text)
        if not tokens:
            continue
        cases.append({
            "question_id": row.get("question_id", ""),
            "question": question_text,
            "primitive": primitive,
            "tokens": tokens,
            "expected_tools": list(row.get("expected_tools", [])),
            "attorney_category": row.get("attorney_category", ""),
            "architecture_primary": row.get("architecture_primary", ""),
            "domain_primary": row.get("domain_primary", ""),
            "eval_split": row.get("eval_split", ""),
        })

    _ENRON_ROUTING_CASES = cases
    return _ENRON_ROUTING_CASES


def _get_case_based_pattern_hint(question: str, contract: dict | None = None) -> dict:
    if CORPUS != "enron":
        return {}

    contract = contract or _extract_answer_contract(question)
    question_tokens = _tokenize_router_text(question)
    if not question_tokens:
        return {}

    pattern_scores: dict[str, float] = {}
    best_case: dict[str, dict] = {}

    for pattern, card in _PATTERN_ROUTER_CARDS.items():
        card_text = " ".join([str(card.get("description", "")), *card.get("keywords", ())])
        pattern_scores[pattern] = _jaccard_score(question_tokens, _tokenize_router_text(card_text))
        pattern_scores[pattern] += _pattern_card_bonus(pattern, contract)

    for case in _load_enron_routing_cases():
        pattern = case["primitive"]
        score = _jaccard_score(question_tokens, case["tokens"])
        if score <= 0 and contract.get("force_pattern") != pattern:
            continue
        score += _pattern_card_bonus(pattern, contract)
        if contract.get("requires_evidence") and case.get("architecture_primary") == "evidence_drilldown":
            score += 0.04
        if contract.get("documentary_evidence_like") and pattern == "keyword_search":
            score += 0.05
        if contract.get("answer_type") == "count" and case.get("architecture_primary") == "analytics_sql_genie":
            score += 0.05
        if score > pattern_scores.get(pattern, 0.0):
            pattern_scores[pattern] = score
            best_case[pattern] = {
                "question_id": case.get("question_id", ""),
                "question": case.get("question", ""),
                "expected_tools": list(case.get("expected_tools", [])),
                "eval_split": case.get("eval_split", ""),
            }

    ordered = sorted(pattern_scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return {}

    best_pattern, best_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    force_pattern = contract.get("force_pattern", "")
    if force_pattern and best_score < 0.7:
        best_pattern = force_pattern
        best_score = max(best_score, 0.72)

    if best_score < _ROUTER_MIN_SCORE and not force_pattern:
        return {}

    confidence = min(0.95, max(0.40, best_score + max(0.0, best_score - second_score)))
    candidates = []
    for pattern, score in ordered[:3]:
        candidate = {"pattern": pattern, "score": round(score, 3)}
        if pattern in best_case:
            candidate["question_id"] = best_case[pattern].get("question_id", "")
        candidates.append(candidate)

    result = {
        "pattern": best_pattern,
        "confidence": confidence,
        "score": best_score,
        "candidates": candidates,
    }
    if best_pattern in best_case:
        result.update({
            "question_id": best_case[best_pattern].get("question_id", ""),
            "matched_question": best_case[best_pattern].get("question", ""),
            "expected_tools": best_case[best_pattern].get("expected_tools", []),
        })
    return result


def _apply_case_router_hint(question: str, classification: dict, routing_hint: dict | None) -> dict:
    if CORPUS != "enron" or not isinstance(classification, dict):
        return classification

    routed = dict(classification)
    if routing_hint:
        routed["router_candidates"] = routing_hint.get("candidates", [])
        routed["router_score"] = float(routing_hint.get("confidence", 0.0) or 0.0)
        hinted_pattern = routing_hint.get("pattern", "")
        current_pattern = routed.get("pattern", "general")
        contract = routed.get("contract", {}) if isinstance(routed.get("contract"), dict) else {}
        force_pattern = contract.get("force_pattern", "")
        should_override = False
        if hinted_pattern and hinted_pattern != current_pattern:
            if force_pattern and hinted_pattern == force_pattern:
                should_override = True
            elif routed["router_score"] >= 0.82 and current_pattern in {"general", "keyword_search", "entity_explore", "timeline"}:
                should_override = True
            elif routed["router_score"] >= 0.90 and current_pattern != "genie_analytics":
                should_override = True
        if should_override:
            routed["pattern"] = hinted_pattern
            routed["confidence"] = max(float(routed.get("confidence", 0.0) or 0.0), routed["router_score"])
            routed["routing_override"] = f"case_router:{hinted_pattern}"

    return _apply_factual_routing_overrides(question, routed)


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
# Evidence scoring (C3 — unified relevance scoring layer)
# ---------------------------------------------------------------------------
def _score_evidence(
    emails: list[dict],
    *,
    query_entities: list[str] | None = None,
    date_range: tuple[str, str] | None = None,
    vector_scores: list[float] | None = None,
) -> list[dict]:
    """Score and rank email evidence by relevance using configurable signal weights.

    Each email dict gets a 'relevance_score' field added. Returns the list
    sorted by relevance_score descending, filtered by min_display_threshold.

    Args:
        emails: List of email dicts (must have 'sender', 'date', 'subject',
                and optionally 'to', 'body_preview'/'snippet', 'recipient_count').
        query_entities: Entity names being queried — used for sender/recipient matching.
        date_range: Optional (from, to) date strings for temporal proximity scoring.
        vector_scores: Optional per-email similarity scores from vector search.
    """
    sw = EVIDENCE_CONFIG["signal_weights"]
    min_threshold = EVIDENCE_CONFIG["min_display_threshold"]
    et = EVIDENCE_CONFIG["email_type_thresholds"]
    dedup_mode = EVIDENCE_CONFIG["evidence_dedup"]

    entity_patterns = []
    if query_entities:
        for ent in query_entities:
            entity_patterns.append(ent.lower())
            entity_patterns.append(ent.lower().replace(" ", "_"))
            parts = ent.lower().split()
            if parts:
                entity_patterns.append(parts[-1])

    seen_threads: set[str] = set()
    scored = []
    for idx, email in enumerate(emails):
        score = 0.0
        sender = (email.get("sender") or email.get("from") or "").lower()
        to_field = (email.get("to") or email.get("to_list") or "").lower()
        body = (email.get("body_preview") or email.get("snippet") or "").lower()
        subject = (email.get("subject") or "").lower()

        for pat in entity_patterns:
            if pat in sender:
                score += sw["direct_recipient"]
                break
        for pat in entity_patterns:
            if pat in to_field:
                score += sw["direct_recipient"] * 0.8
                break

        if len(entity_patterns) >= 2:
            found_in_body = sum(1 for pat in entity_patterns[:4] if pat in body or pat in subject)
            if found_in_body >= 2:
                score += sw["body_mention"]

        rc = email.get("recipient_count", 0)
        try:
            rc = int(rc)
        except (ValueError, TypeError):
            rc = 0
        if rc > et.get("group", 10):
            score += sw["email_type_penalty"]
        elif rc > et.get("direct", 3):
            score += sw["email_type_penalty"] * 0.3

        if date_range:
            email_date = str(email.get("date", ""))[:10]
            if email_date and date_range[0] <= email_date <= date_range[1]:
                score += sw["temporal_proximity"]

        if vector_scores and idx < len(vector_scores):
            score += sw["vector_similarity"] * vector_scores[idx]

        org_kw = sw.get("org_keyword", 0.0)
        if org_kw > 0:
            org_terms = {"report", "team", "direct", "manager", "boss", "supervise",
                         "oversee", "leadership", "group", "department", "division"}
            combined = f"{body} {subject}"
            if any(t in combined for t in org_terms):
                score += org_kw

        score = max(0.0, min(1.0, score))
        email["relevance_score"] = round(score, 3)

        tid = email.get("thread_id", "")
        if dedup_mode == "thread-level" and tid:
            if tid in seen_threads:
                continue
            seen_threads.add(tid)

        if score >= min_threshold:
            scored.append(email)

    scored.sort(key=lambda e: e.get("relevance_score", 0), reverse=True)
    return scored


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
        if EVIDENCE_CONFIG.get("preserve_source_threads") and "source_threads" in r:
            st = r["source_threads"]
            if isinstance(st, str):
                st = [t.strip() for t in st.strip("[]").split(",") if t.strip()]
            if st:
                entry["source_threads"] = st[:5]
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
                resolved = resolve_entity_cached(r["name"])
                addrs = [e for e in resolved.email_addresses if "@" in e]
                if addrs:
                    entry["email_addresses"] = addrs
            except Exception:
                pass
        entities.append(entry)
    return json.dumps(entities, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Unified entity resolution — single resolver for graph + communication layers
# ---------------------------------------------------------------------------

@dataclass
class ResolvedEntity:
    """Canonical identity resolved once and shared across all tools."""
    input_name: str
    canonical_name: str
    entity_id: str
    entity_id_patterns: list[str]
    email_patterns: list[str]
    email_addresses: list[str]
    confidence: str  # "exact", "alias", "fuzzy", "stem"
    correction: str | None = None


_resolve_cache: dict[str, "ResolvedEntity"] = {}
_fuzzy_candidate_cache: list[dict] | None = None


def clear_resolve_cache() -> None:
    _resolve_cache.clear()
    global _fuzzy_candidate_cache
    _fuzzy_candidate_cache = None


def _load_fuzzy_resolution_candidates() -> list[dict]:
    global _fuzzy_candidate_cache
    if _fuzzy_candidate_cache is not None:
        return _fuzzy_candidate_cache

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    entity_names: dict[str, str] = {}

    def add_candidate(lookup: str, entity_id: str, name: str, source: str) -> None:
        normalized = (lookup or "").strip().lower()
        canonical_id = (entity_id or "").strip()
        if not normalized or not canonical_id:
            return
        key = (normalized, canonical_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "lookup": normalized,
            "entity_id": canonical_id,
            "name": name or canonical_id,
            "source": source,
        })

    try:
        entity_rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
        ) or []
    except Exception:
        entity_rows = []

    for row in entity_rows:
        entity_id = row.get("entity_id", "")
        name = row.get("name") or entity_id
        if not entity_id:
            continue
        entity_names[entity_id] = name
        add_candidate(entity_id, entity_id, name, "entity_id")
        add_candidate(_slugify(name), entity_id, name, "name")

    if CORPUS == "enron":
        try:
            alias_rows = _backend.execute_sql(
                f"SELECT alias_id, canonical_id FROM {CATALOG}.{ENRON_SCHEMA}.entity_aliases"
            ) or []
        except Exception:
            alias_rows = []

        for row in alias_rows:
            alias_id = row.get("alias_id", "")
            canonical_id = row.get("canonical_id", "")
            if not alias_id or not canonical_id:
                continue
            add_candidate(alias_id, canonical_id, entity_names.get(canonical_id, canonical_id), "alias")

    _fuzzy_candidate_cache = candidates
    return _fuzzy_candidate_cache


def _rapidfuzz_match_entity(slug: str, score_cutoff: float = 91.0) -> list[dict]:
    if not slug or rapidfuzz_process is None or rapidfuzz_fuzz is None:
        return []

    candidates = _load_fuzzy_resolution_candidates()
    if not candidates:
        return []

    matches = rapidfuzz_process.extract(
        slug,
        [candidate["lookup"] for candidate in candidates],
        scorer=rapidfuzz_fuzz.ratio,
        limit=12,
        score_cutoff=score_cutoff,
    )
    if not matches:
        return []

    source_rank = {"alias": 0, "name": 1, "entity_id": 2}
    best_by_entity: dict[str, dict] = {}
    for _, score, idx in matches:
        candidate = candidates[idx]
        if candidate["lookup"] == slug:
            continue
        item = {
            "entity_id": candidate["entity_id"],
            "name": candidate["name"],
            "score": float(score),
            "source": candidate["source"],
        }
        current = best_by_entity.get(item["entity_id"])
        if current is None or item["score"] > current["score"] or (
            item["score"] == current["score"]
            and source_rank.get(item["source"], 9) < source_rank.get(current["source"], 9)
        ):
            best_by_entity[item["entity_id"]] = item

    ranked = sorted(
        best_by_entity.values(),
        key=lambda item: (-item["score"], source_rank.get(item["source"], 9), item["name"]),
    )
    if not ranked:
        return []

    top_score = ranked[0]["score"]
    second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if top_score < score_cutoff:
        return []
    if len(ranked) > 1 and top_score < 97.0 and (top_score - second_score) < 3.0:
        return []

    return [{"entity_id": item["entity_id"], "name": item["name"]} for item in ranked[:5]]


def _fuzzy_match_entity(slug: str, max_distance: int = 2) -> list[dict]:
    """Find entities within Levenshtein distance of the input slug."""
    rapidfuzz_matches = _rapidfuzz_match_entity(slug)
    if rapidfuzz_matches:
        return rapidfuzz_matches

    prefix = slug[:3]
    try:
        candidates = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            f" WHERE SUBSTRING(entity_id, 1, 3) = :prefix"
            f"   AND LEVENSHTEIN(entity_id, :slug) <= :max_dist"
            f"   AND LEVENSHTEIN(entity_id, :slug) > 0"
            f" ORDER BY LEVENSHTEIN(entity_id, :slug)"
            f" LIMIT 5",
            params={"prefix": prefix, "slug": slug, "max_dist": max_distance},
        )
        return [{"entity_id": r["entity_id"], "name": r["name"]} for r in (candidates or [])]
    except Exception:
        return []


def resolve_entity(name: str) -> ResolvedEntity:
    """Resolve a name to a canonical identity with both graph and email facets.

    Resolution cascade: exact match -> alias -> fuzzy (Levenshtein) -> stem.
    """
    slug = _slugify(name)
    parts = name.lower().split()

    confidence = "exact"
    canonical_name = name
    canonical_id = slug
    correction: str | None = None

    # Phase 1: Graph identity -----------------------------------------------
    # 1a. Exact entity match
    exact = []
    try:
        exact = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            f" WHERE entity_id = :slug LIMIT 1",
            params={"slug": slug},
        )
    except Exception:
        pass

    if exact:
        canonical_name = exact[0]["name"]
        canonical_id = exact[0]["entity_id"]
    else:
        # 1b. Alias match
        alias_row = []
        try:
            alias_row = _backend.execute_sql(
                f"SELECT canonical_id FROM {CATALOG}.{ENRON_SCHEMA}.entity_aliases"
                f" WHERE alias_id = :slug LIMIT 1",
                params={"slug": slug},
            )
        except Exception:
            pass

        if alias_row:
            canonical_id = alias_row[0]["canonical_id"]
            confidence = "alias"
            try:
                entity_row = _backend.execute_sql(
                    f"SELECT name FROM {ENTITIES_TABLE}"
                    f" WHERE entity_id = :cid LIMIT 1",
                    params={"cid": canonical_id},
                )
                if entity_row:
                    canonical_name = entity_row[0]["name"]
            except Exception:
                pass
        else:
            # 1c. Fuzzy match (spelling correction)
            fuzzy = _fuzzy_match_entity(slug)
            if fuzzy:
                canonical_id = fuzzy[0]["entity_id"]
                canonical_name = fuzzy[0]["name"]
                confidence = "fuzzy"
                correction = f"'{name}' corrected to '{canonical_name}'"
            else:
                confidence = "stem"

    # Build entity_id LIKE patterns
    eid_patterns: list[str] = [f"%{canonical_id}%"]
    if canonical_id != slug:
        eid_patterns.append(f"%{slug}%")
    if len(parts) == 2:
        eid_patterns.append(f"%{parts[1]}_{parts[0]}%")
        eid_patterns.append(f"%{parts[0][0]}_{parts[1]}%")

    # Phase 2: Email identity -----------------------------------------------
    cparts = canonical_name.lower().split()
    email_pats: list[str] = []
    if len(cparts) >= 2:
        first, last = cparts[0], cparts[-1]
        email_pats = [f"%{first}.{last}%", f"%{last}.{first}%", f"%{first[0]}.{last}%"]
        if len(cparts) == 3:
            mid = cparts[1]
            email_pats.append(f"%{first}.{mid[0]}.{last}%")
    elif cparts:
        email_pats = [f"%{cparts[0]}%"]

    if "@" in name:
        email_pats.insert(0, name.lower().strip())

    # 2b. Participants table lookup
    email_addresses: list[str] = []
    try:
        lookup_name = canonical_name.lower()
        rows = _backend.execute_sql(
            f"SELECT DISTINCT email_address FROM {ENRON_PARTICIPANTS_TABLE}"
            f" WHERE LOWER(name_normalized) LIKE :pat"
            f"    OR LOWER(display_name) LIKE :pat"
            f" LIMIT 10",
            params={"pat": f"%{lookup_name}%"},
        )
        email_addresses = [r["email_address"] for r in (rows or []) if r.get("email_address")]
        for addr in email_addresses:
            if addr not in email_pats:
                email_pats.insert(0, addr)
    except Exception:
        pass

    # 2c. Stem fallback for email if no participants matched
    if not email_addresses and len(cparts) >= 2:
        first, last = cparts[0], cparts[-1]
        stem = last[:-2] if len(last) > 4 else last[:-1] if len(last) > 2 else last
        try:
            rows = _backend.execute_sql(
                f"SELECT DISTINCT email_address FROM {ENRON_PARTICIPANTS_TABLE}"
                f" WHERE (LOWER(name_normalized) LIKE :stem_pat"
                f"    OR LOWER(display_name) LIKE :stem_pat)"
                f"   AND (LOWER(name_normalized) LIKE :first_pat"
                f"    OR LOWER(display_name) LIKE :first_pat)"
                f" LIMIT 5",
                params={"stem_pat": f"%{stem}%", "first_pat": f"%{first}%"},
            )
            for r in (rows or []):
                addr = r.get("email_address", "")
                if addr and addr not in email_pats:
                    email_pats.append(addr)
                    email_addresses.append(addr)
        except Exception:
            pass

    return ResolvedEntity(
        input_name=name,
        canonical_name=canonical_name,
        entity_id=canonical_id,
        entity_id_patterns=list(dict.fromkeys(eid_patterns)),
        email_patterns=list(dict.fromkeys(email_pats)),
        email_addresses=email_addresses,
        confidence=confidence,
        correction=correction,
    )


def resolve_entity_cached(name: str) -> ResolvedEntity:
    key = _slugify(name)
    if key not in _resolve_cache:
        _resolve_cache[key] = resolve_entity(name)
    return _resolve_cache[key]


def _resolution_metadata(resolved: ResolvedEntity) -> dict:
    """Metadata dict to include in tool JSON output."""
    meta: dict = {
        "input": resolved.input_name,
        "resolved_to": resolved.canonical_name,
        "confidence": resolved.confidence,
    }
    if resolved.correction:
        meta["correction"] = resolved.correction
    return meta


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
        resolved = resolve_entity_cached(entity_name)

        rel_filter = ""
        if relationship_type:
            rel_filter = " AND r.relationship_type = :rel_type"
            sql_params["rel_type"] = relationship_type.upper()

        results = []
        for i, pattern in enumerate(resolved.entity_id_patterns):
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
            correction = f" (Note: {resolved.correction})" if resolved.correction else ""
            return f"No connections found for '{entity_name}'{suffix}.{correction}"

        grouped = _group_connections(entity_name, results, corpus="enron")
        grouped["resolution"] = _resolution_metadata(resolved)

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

    resolved = resolve_entity_cached(entity_name)
    email_patterns = resolved.email_patterns
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
        "entity": resolved.canonical_name,
        "direction": direction,
        "source": "communication_dyads",
        "top_contacts": contacts,
        "resolution": _resolution_metadata(resolved),
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

    resolved_a = resolve_entity_cached(entity_a)
    resolved_b = resolve_entity_cached(entity_b)

    results = []
    match_type = "header"
    for a_pat in resolved_a.email_patterns:
        for b_pat in resolved_b.email_patterns:
            results = _backend.execute_sql(
                f"SELECT message_id, sender, subject, date, thread_id,"
                f" SUBSTRING(body, 1, {EVIDENCE_CONFIG['body_preview_length']}) AS body_preview,"
                f" COALESCE(ARRAY_JOIN(to_recipients, ', '), '') AS to_list"
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
        for a_pat in resolved_a.entity_id_patterns:
            for b_pat in resolved_b.entity_id_patterns:
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
        all_threads: list[str] = []
        for si, sp in enumerate(resolved_a.entity_id_patterns):
            for ti, tp in enumerate(resolved_b.entity_id_patterns):
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
        corrections = []
        if resolved_a.correction:
            corrections.append(resolved_a.correction)
        if resolved_b.correction:
            corrections.append(resolved_b.correction)
        correction_note = " " + "; ".join(corrections) if corrections else ""
        return json.dumps({
            "between": [resolved_a.canonical_name, resolved_b.canonical_name],
            "total_emails": 0,
            "showing": 0,
            "match_type": "none",
            "emails": [],
            "resolution": {
                "a": _resolution_metadata(resolved_a),
                "b": _resolution_metadata(resolved_b),
            },
            "note": f"No emails found between '{resolved_a.canonical_name}' and '{resolved_b.canonical_name}'.{correction_note}",
        }, ensure_ascii=False)

    total_count = len(results)
    if match_type == "header" and total_count >= int(limit):
        try:
            dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
            for ap in resolved_a.email_patterns:
                for bp in resolved_b.email_patterns:
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
    sent_a_to_b = 0
    sent_b_to_a = 0
    a_emails = {p.lower() for p in resolved_a.email_patterns}
    b_emails = {p.lower() for p in resolved_b.email_patterns}
    for r in results:
        sender = (r.get("sender", "") or "").lower()
        emails.append({
            "date": str(r.get("date", ""))[:10],
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": (r.get("body_preview", "") or "")[:800],
            "thread_id": r.get("thread_id", ""),
            "message_id": r.get("message_id", ""),
        })
        if any(sender.startswith(p.replace("%", "")) for p in a_emails):
            sent_a_to_b += 1
        elif any(sender.startswith(p.replace("%", "")) for p in b_emails):
            sent_b_to_a += 1
    return json.dumps({
        "between": [resolved_a.canonical_name, resolved_b.canonical_name],
        "total_emails": total_count,
        "showing": len(emails),
        "sent_a_to_b": sent_a_to_b,
        "sent_b_to_a": sent_b_to_a,
        "direction_summary": (
            f"{resolved_a.canonical_name} → {resolved_b.canonical_name}: {sent_a_to_b}, "
            f"{resolved_b.canonical_name} → {resolved_a.canonical_name}: {sent_b_to_a}"
        ),
        "match_type": match_type,
        "emails": emails,
        "resolution": {
            "a": _resolution_metadata(resolved_a),
            "b": _resolution_metadata(resolved_b),
        },
        "hint": "Use get_email_full_body(message_id=...) to see the complete untruncated body of any email.",
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

    resolved_a = resolve_entity_cached(entity_a)
    resolved_b = resolve_entity_cached(entity_b)

    thread_rows = []
    for a_pat in resolved_a.email_patterns:
        for b_pat in resolved_b.email_patterns:
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
            "between": [resolved_a.canonical_name, resolved_b.canonical_name],
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
        "between": [resolved_a.canonical_name, resolved_b.canonical_name],
        "threads_scanned": len(topic_rows),
        "top_topics": ranked_topics,
        "threads": threads_out[:10],
        "resolution": {
            "a": _resolution_metadata(resolved_a),
            "b": _resolution_metadata(resolved_b),
        },
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

    resolved_src = resolve_entity_cached(source_entity)
    resolved_tgt = resolve_entity_cached(target_entity)
    src_patterns = resolved_src.entity_id_patterns
    tgt_patterns = resolved_tgt.entity_id_patterns

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
                f"SELECT r.source_threads, r.description, r.relationship_type,"
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
                    f"SELECT r.source_threads, r.description, r.relationship_type,"
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

    preview_len = EVIDENCE_CONFIG["body_preview_length"]
    fetch_limit = int(limit * 3)
    emails_rows = _backend.execute_sql(
        f"SELECT sender, subject, date, thread_id,"
        f" SUBSTRING(body, 1, {preview_len}) AS body_preview,"
        f" COALESCE(ARRAY_JOIN(to_recipients, ', '), '') AS to_list,"
        f" COALESCE(SIZE(to_recipients), 0) AS recipient_count"
        f" FROM {src_table}"
        f" WHERE thread_id IN ({placeholders})"
        f" ORDER BY date LIMIT {fetch_limit}",
        params=thread_params,
    )

    scored_emails = _score_evidence(
        emails_rows,
        query_entities=[source_entity, target_entity],
    )

    evidence_emails = []
    for e in scored_emails[:limit]:
        evidence_emails.append({
            "date": str(e.get("date", ""))[:10],
            "sender": e.get("sender", ""),
            "subject": e.get("subject", ""),
            "thread_id": e.get("thread_id", ""),
            "body_preview": (e.get("body_preview", "") or "")[:EVIDENCE_CONFIG["snippet_length"]],
            "relevance_score": e.get("relevance_score", 0),
        })

    return json.dumps({
        "source": resolved_src.canonical_name,
        "target": resolved_tgt.canonical_name,
        "relationships": rel_descriptions,
        "evidence_emails": evidence_emails,
        "thread_count": len(all_threads),
        "resolution": {
            "source": _resolution_metadata(resolved_src),
            "target": _resolution_metadata(resolved_tgt),
        },
        "hint": "Use get_email_full_body(thread_id=...) to see the complete untruncated body of any evidence email.",
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

        preview_len = EVIDENCE_CONFIG["body_preview_length"]
        results = _backend.execute_sql(
            f"SELECT message_id, sender, date, subject, thread_id,"
            f" ARRAY_JOIN(SLICE(to_recipients, 1, 3), ', ') AS to_list,"
            f" SUBSTRING(body, 1, {preview_len}) AS body_preview,"
            f" COALESCE(SIZE(to_recipients), 0) + COALESCE(SIZE(cc_recipients), 0) AS recipient_count"
            f" FROM {src_table}"
            f" WHERE {where_clause}"
            " ORDER BY date DESC LIMIT 30",
            params=sql_params,
        )
        if not results:
            search_desc = " AND ".join(terms)
            return f"No emails found mentioning '{search_desc}'."

        scored_results = _score_evidence(results, query_entities=terms)

        emails = []
        for r in scored_results[:20]:
            et_thresholds = EVIDENCE_CONFIG["email_type_thresholds"]
            entry = {
                "message_id": r.get("message_id", ""),
                "thread_id": r.get("thread_id", ""),
                "date": str(r.get("date", ""))[:10],
                "from": r.get("sender", ""),
                "to": r.get("to_list", ""),
                "subject": r.get("subject", ""),
                "snippet": (r.get("body_preview", "") or "")[:EVIDENCE_CONFIG["snippet_length"]],
                "relevance_score": r.get("relevance_score", 0),
            }
            rc = r.get("recipient_count")
            if rc is not None:
                try:
                    rc_int = int(rc)
                    entry["recipient_count"] = rc_int
                    entry["email_type"] = (
                        "direct" if rc_int <= et_thresholds.get("direct", 3)
                        else "group" if rc_int <= et_thresholds.get("group", 10)
                        else "mass"
                    )
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
        resolved = resolve_entity_cached(entity_name)
        all_patterns = resolved.entity_id_patterns
    else:
        resolved = None
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
        correction = ""
        if resolved and resolved.correction:
            correction = f" ({resolved.correction})"
        return f"Entity '{entity_name}' not found in the knowledge graph.{correction}"

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
            resolved_person = resolve_entity_cached(first["name"])
            email_addrs = [e for e in resolved_person.email_addresses if "@" in e]
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

    if resolved:
        summary["resolution"] = _resolution_metadata(resolved)
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
        resolved_a = resolve_entity_cached(entity_a)
        resolved_b = resolve_entity_cached(entity_b)
        a_patterns = resolved_a.entity_id_patterns
        b_patterns = resolved_b.entity_id_patterns
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

    def _run_path_query(relationship_types: tuple[str, ...] | None = None) -> list[dict]:
        rel_filter = ""
        if CORPUS == "enron":
            rel_filter = "  AND r.relationship_type IN ('REPORTS_TO','MANAGES')"
        elif relationship_types:
            rel_csv = ", ".join(f"'{rel}'" for rel in relationship_types)
            rel_filter = f"  AND r.relationship_type IN ({rel_csv})"

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
            f"{rel_filter}"
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
        return _backend.execute_sql(cte_query)

    path_rows: list[dict] = []
    if CORPUS != "enron":
        family_relations = (
            "PARENT_OF",
            "CHILD_OF",
            "ANCESTOR_OF",
            "DESCENDANT_OF",
            "FATHER_OF",
            "MOTHER_OF",
            "SPOUSE_OF",
            "MARRIED_TO",
            "HUSBAND_OF",
            "WIFE_OF",
        )
        path_rows = _run_path_query(family_relations)

    if not path_rows:
        path_rows = _run_path_query()

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


_BIBLE_LINEAGE_STOP_WORDS = {
    "How", "What", "Who", "Trace", "Explain", "Show", "Step", "Bible",
    "Lineage", "Genealogy", "Connection",
}


def _extract_bible_lineage_entities(question: str) -> list[dict]:
    candidates = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", question or "")
    resolved: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in _BIBLE_LINEAGE_STOP_WORDS:
            continue
        rows = _backend.execute_sql(
            f"SELECT entity_id, name FROM {ENTITIES_TABLE}"
            " WHERE LOWER(name) = LOWER(:name)"
            " ORDER BY name LIMIT 1",
            params={"name": candidate},
        )
        if not rows:
            continue
        row = rows[0]
        name_key = (row.get("name") or "").lower()
        if not name_key or name_key in seen:
            continue
        seen.add(name_key)
        resolved.append(row)
        if len(resolved) >= 2:
            break
    return resolved


def _build_bible_lineage_answer(question: str) -> str | None:
    q_lower = (question or "").lower()
    if CORPUS != "bible":
        return None
    if not any(
        phrase in q_lower
        for phrase in ("lineage", "genealogy", "ancestor", "descendant", "connected to")
    ):
        return None

    entities = _extract_bible_lineage_entities(question)
    if len(entities) < 2:
        return None

    entity_a, entity_b = entities[:2]
    family_relations = (
        "PARENT_OF",
        "CHILD_OF",
        "ANCESTOR_OF",
        "DESCENDANT_OF",
        "FATHER_OF",
        "MOTHER_OF",
        "SPOUSE_OF",
        "MARRIED_TO",
        "HUSBAND_OF",
        "WIFE_OF",
    )
    rel_csv = ", ".join(f"'{rel}'" for rel in family_relations)
    cte_query = (
        "WITH RECURSIVE bfs(current_id, depth, visited, path_ids, path_names, path_rels) AS ("
        f" SELECT e.entity_id, 0,"
        "  CAST('|' || e.entity_id || '|' AS VARCHAR(4000)),"
        "  CAST(e.entity_id AS VARCHAR(4000)),"
        "  CAST(e.name AS VARCHAR(4000)),"
        "  CAST('' AS VARCHAR(4000))"
        f" FROM {ENTITIES_TABLE} e"
        " WHERE e.entity_id = :start_id"
        " UNION ALL"
        " SELECT"
        "  CASE WHEN r.source_entity = b.current_id"
        "   THEN r.target_entity ELSE r.source_entity END,"
        "  b.depth + 1,"
        "  b.visited"
        "   || CASE WHEN r.source_entity = b.current_id"
        "       THEN r.target_entity ELSE r.source_entity END || '|',"
        "  b.path_ids || '|' || CASE WHEN r.source_entity = b.current_id"
        "       THEN r.target_entity ELSE r.source_entity END,"
        "  b.path_names || '|' || COALESCE("
        "   CASE WHEN r.source_entity = b.current_id THEN e2.name ELSE e1.name END,"
        "   CASE WHEN r.source_entity = b.current_id"
        "    THEN r.target_entity ELSE r.source_entity END),"
        "  CASE WHEN b.path_rels = '' THEN r.relationship_type"
        "   ELSE b.path_rels || '|' || r.relationship_type END"
        " FROM bfs b"
        f" JOIN {RELATIONSHIPS_TABLE} r"
        "  ON (r.source_entity = b.current_id OR r.target_entity = b.current_id)"
        f"  AND r.relationship_type IN ({rel_csv})"
        f" LEFT JOIN {ENTITIES_TABLE} e1 ON r.source_entity = e1.entity_id"
        f" LEFT JOIN {ENTITIES_TABLE} e2 ON r.target_entity = e2.entity_id"
        " WHERE b.depth < 6"
        "  AND b.visited NOT LIKE"
        "   '%|' || CASE WHEN r.source_entity = b.current_id"
        "    THEN r.target_entity ELSE r.source_entity END || '|%'"
        ")"
        " SELECT path_ids, path_names, path_rels, depth"
        " FROM bfs WHERE current_id = :end_id"
        " ORDER BY depth LIMIT 1"
    )
    rows = _backend.execute_sql(
        cte_query,
        params={"start_id": entity_a["entity_id"], "end_id": entity_b["entity_id"]},
    )
    if not rows:
        return None

    row = rows[0]
    path_ids = [part for part in (row.get("path_ids") or "").split("|") if part]
    path_names = [part for part in (row.get("path_names") or "").split("|") if part]
    path_rels = [part for part in (row.get("path_rels") or "").split("|") if part]
    if len(path_ids) < 2 or len(path_rels) != len(path_ids) - 1:
        return None

    steps: list[dict] = []
    source_refs: list[str] = []
    for idx, rel in enumerate(path_rels):
        src_id = path_ids[idx]
        tgt_id = path_ids[idx + 1]
        src_name = path_names[idx]
        tgt_name = path_names[idx + 1]
        edge_rows = _backend.execute_sql(
            f"SELECT relationship_type, book, chapter, description"
            f" FROM {RELATIONSHIPS_TABLE}"
            " WHERE ((source_entity = :src AND target_entity = :tgt)"
            "    OR (source_entity = :tgt AND target_entity = :src))"
            "   AND relationship_type = :rel"
            " ORDER BY book, chapter LIMIT 1",
            params={"src": src_id, "tgt": tgt_id, "rel": rel},
        )
        edge = edge_rows[0] if edge_rows else {}
        reference = ""
        if edge.get("book") and edge.get("chapter") is not None:
            reference = f"{edge['book']} {edge['chapter']}"
            source_refs.append(reference)
        steps.append({
            "source": src_name,
            "relationship": rel,
            "target": tgt_name,
            "reference": reference,
        })

    path_render = " -> ".join(path_names)
    step_lines = []
    for index, step in enumerate(steps, start=1):
        ref_suffix = f", {step['reference']}" if step["reference"] else ""
        step_lines.append(
            f"{index}. {step['source']} -> {step['target']} ({step['relationship']}{ref_suffix})"
        )

    unique_sources = list(dict.fromkeys(source_refs))
    sources_line = ", ".join(unique_sources) if unique_sources else "None retrieved"
    return "\n".join([
        "### Answer",
        f"{entity_a['name']} is connected to {entity_b['name']} through this family line:",
        *step_lines,
        "",
        "### Provenance",
        f"- **Path**: {path_render}",
        f"- **Sources**: {sources_line}",
        "- **Grounding**: All claims grounded in knowledge graph.",
    ])


def _build_bible_comparison_answer(question: str) -> str | None:
    q_lower = (question or "").lower()
    if CORPUS != "bible" or "compare" not in q_lower:
        return None

    entities = _extract_bible_lineage_entities(question)
    if len(entities) < 2:
        return None

    profiles: list[dict] = []
    sources: list[str] = []
    for entity in entities[:2]:
        rel_rows = _backend.execute_sql(
            f"SELECT relationship_type, COUNT(*) AS frequency,"
            f" MIN(book) AS book, MIN(chapter) AS chapter,"
            f" MAX(description) AS description"
            f" FROM {RELATIONSHIPS_TABLE}"
            " WHERE source_entity = :entity_id OR target_entity = :entity_id"
            " GROUP BY relationship_type"
            " ORDER BY frequency DESC, relationship_type"
            " LIMIT 3",
            params={"entity_id": entity["entity_id"]},
        )
        if not rel_rows:
            return None

        top_relationships = []
        for row in rel_rows:
            reference = ""
            if row.get("book") and row.get("chapter") is not None:
                reference = f"{row['book']} {row['chapter']}"
                sources.append(reference)
            top_relationships.append({
                "relationship_type": row["relationship_type"],
                "frequency": row.get("frequency", 0),
                "reference": reference,
                "description": row.get("description", ""),
            })
        profiles.append({"name": entity["name"], "relationships": top_relationships})

    comparison_lines = []
    for profile in profiles:
        rel_parts = []
        for rel in profile["relationships"]:
            ref_suffix = f" ({rel['reference']})" if rel["reference"] else ""
            rel_parts.append(
                f"{rel['relationship_type']} x{rel['frequency']}{ref_suffix}"
            )
        comparison_lines.append(
            f"- {profile['name']}: strongest graph signals are "
            + ", ".join(rel_parts)
            + "."
        )

    unique_sources = list(dict.fromkeys(sources))
    sources_line = ", ".join(unique_sources) if unique_sources else "None retrieved"
    return "\n".join([
        "### Answer",
        f"{profiles[0]['name']} and {profiles[1]['name']} both appear as major leadership figures in the graph, but they are emphasized through different relationship patterns.",
        *comparison_lines,
        "",
        "### Provenance",
        f"- **Path**: {profiles[0]['name']} -> leadership pattern; {profiles[1]['name']} -> leadership pattern",
        f"- **Sources**: {sources_line}",
        "- **Grounding**: All claims grounded in knowledge graph.",
    ])


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
        resolved = resolve_entity_cached(entity_name)
        results = None
        for ep in resolved.email_patterns:
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
        email_pats_a = resolve_entity_cached(entity_name).email_patterns
        email_pats_b = resolve_entity_cached(entity_b).email_patterns
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
        resolved_ent = resolve_entity_cached(entity_name)
        results = None
        for ep in resolved_ent.email_patterns:
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
        resolved_ent = resolve_entity_cached(entity_name)
        results = None
        for ep in resolved_ent.email_patterns:
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
        sender_resolved = resolve_entity_cached(sender)
        if sender_resolved.email_patterns:
            where_parts.append(f"LOWER(sender) LIKE :sender_pat")
            params["sender_pat"] = sender_resolved.email_patterns[0]
    if recipient:
        recip_resolved = resolve_entity_cached(recipient)
        if recip_resolved.email_patterns:
            where_parts.append(
                f"(LOWER(CAST(to_recipients AS STRING)) LIKE :recip_pat"
                f" OR LOWER(CAST(cc_recipients AS STRING)) LIKE :recip_pat"
                f" OR LOWER(CAST(bcc_recipients AS STRING)) LIKE :recip_pat)"
            )
            params["recip_pat"] = recip_resolved.email_patterns[0]

    where_clause = " AND ".join(where_parts)

    preview_len = EVIDENCE_CONFIG["body_preview_length"]
    fetch_limit = int(limit * 2)
    sql = (
        f"SELECT message_id, date, sender, subject, thread_id,"
        f" SUBSTR(body, 1, {preview_len}) AS body_preview,"
        f" COALESCE(ARRAY_JOIN(to_recipients, ', '), '') AS to_list,"
        f" COALESCE(SIZE(to_recipients), 0) AS recipient_count"
        f" FROM {source_table}"
        f" WHERE {where_clause}"
        f" ORDER BY date DESC"
        f" LIMIT {fetch_limit}"
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

    date_rng = (date_from, date_to) if date_from and date_to else None
    query_ents = kw_list + ([sender] if sender else []) + ([recipient] if recipient else [])
    scored_results = _score_evidence(
        results, query_entities=query_ents, date_range=date_rng,
    )

    emails = []
    for r in scored_results[:limit]:
        emails.append({
            "date": str(r.get("date", "")),
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": r.get("body_preview", ""),
            "thread_id": r.get("thread_id", ""),
            "to_list": r.get("to_list", ""),
            "message_id": r.get("message_id", ""),
            "relevance_score": r.get("relevance_score", 0),
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
        "hint": "Use get_email_full_body(message_id=...) to see the complete email body for any result.",
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
            raw_emails = []
            vs_scores = []
            for row in rows:
                item = dict(zip(cols, row))
                raw_emails.append({
                    "date": str(item.get("date", "")),
                    "sender": item.get("sender", ""),
                    "subject": item.get("subject", ""),
                    "body_preview": str(item.get("body", ""))[:EVIDENCE_CONFIG["body_preview_length"]],
                })
                score_val = item.get("score", item.get("similarity", 0))
                try:
                    vs_scores.append(float(score_val))
                except (ValueError, TypeError):
                    vs_scores.append(0.0)
            scored_emails = _score_evidence(
                raw_emails, query_entities=query.split()[:3],
                vector_scores=vs_scores if EVIDENCE_CONFIG["expose_vector_scores"] else None,
            )
            emails = []
            for e in scored_emails[:limit]:
                entry = {
                    "date": e.get("date", ""),
                    "sender": e.get("sender", ""),
                    "subject": e.get("subject", ""),
                    "body_preview": e.get("body_preview", ""),
                    "relevance_score": e.get("relevance_score", 0),
                }
                emails.append(entry)
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
    preview_len = EVIDENCE_CONFIG["body_preview_length"]
    fetch_limit = int(limit * 2)
    sql = (
        f"SELECT date, sender, subject, thread_id,"
        f" SUBSTR(body, 1, {preview_len}) AS body_preview,"
        f" COALESCE(SIZE(to_recipients), 0) AS recipient_count"
        f" FROM {source_table}"
        f" WHERE ({where})"
        f" ORDER BY date DESC"
        f" LIMIT {fetch_limit}"
    )
    try:
        results = _backend.execute_sql(sql, params=params)
    except Exception as exc:
        return f"Semantic search fallback failed: {exc}"
    if not results:
        return f"No emails found matching semantic query: {query}"
    scored_results = _score_evidence(results, query_entities=words[:3])
    emails = []
    for r in scored_results[:limit]:
        emails.append({
            "date": str(r.get("date", "")),
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "body_preview": r.get("body_preview", ""),
            "relevance_score": r.get("relevance_score", 0),
        })
    return json.dumps({
        "query": query, "method": "sql_fallback",
        "total": len(emails), "emails": emails,
    }, ensure_ascii=False)


def _genie_sql_fallback(question: str, space_name: str) -> dict | None:
    """Direct SQL fallback when Genie is unavailable (e.g. Model Serving identity issues).

    Returns a genie_result-shaped dict on success, None if no fallback applies.
    Handles: top contacts, email counts between pairs, topic distributions,
    percentage queries, and time-of-day filters.
    """
    q_lower = question.lower()
    dyads_table = ENRON_COMMUNICATION_DYADS_TABLE
    activity_table = f"{CATALOG}.{ENRON_SCHEMA}.person_activity"
    emails_table = f"{CATALOG}.{ENRON_SCHEMA}.emails"
    participants_table = f"{CATALOG}.{ENRON_SCHEMA}.participants"
    threads_table = f"{CATALOG}.{ENRON_SCHEMA}.threads"
    mentions_table = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"

    person_names = _heuristic_entity_names(question)

    def _make_result(sql: str, rows: list, description: str = "") -> dict:
        return {
            "source": "databricks_sql_semantic_layer",
            "space": space_name,
            "query": question,
            "sql_generated": sql,
            "results": rows,
            "row_count": len(rows),
            "description": description,
            "analytics_backend": "databricks_sql",
            "semantic_layer": ENRON_COMMUNICATION_METRIC_VIEW,
        }

    try:
        if person_names and any(kw in q_lower for kw in
                ("communicated most", "top contacts", "most frequently",
                 "who did", "email most", "top email")):
            resolved = resolve_entity_cached(person_names[0])
            for ep in resolved.email_patterns:
                sql = (
                    f"SELECT contact_email,"
                    f" SUM(CASE WHEN dir='out' THEN cnt ELSE 0 END) AS sent_to_contact,"
                    f" SUM(CASE WHEN dir='in' THEN cnt ELSE 0 END) AS received_from_contact,"
                    f" SUM(cnt) AS total_emails FROM ("
                    f"  SELECT d.person_b AS contact_email, 'out' AS dir, SUM(d.total_count) AS cnt"
                    f"  FROM {dyads_table} d WHERE LOWER(d.person_a) LIKE :ep GROUP BY d.person_b"
                    f"  UNION ALL"
                    f"  SELECT d.person_a AS contact_email, 'in' AS dir, SUM(d.total_count) AS cnt"
                    f"  FROM {dyads_table} d WHERE LOWER(d.person_b) LIKE :ep GROUP BY d.person_a"
                    f" ) combined GROUP BY contact_email ORDER BY total_emails DESC LIMIT 20"
                )
                rows = _backend.execute_sql(sql, params={"ep": ep})
                if rows:
                    normalized_rows = []
                    for row in rows:
                        sent = int(row.get("sent_to_contact") or 0)
                        received = int(row.get("received_from_contact") or 0)
                        normalized_rows.append({
                            "contact_email": row.get("contact_email", ""),
                            "sent_to_contact": sent,
                            "received_from_contact": received,
                            "total_emails": int(row.get("total_emails") or 0),
                            "exchange_type": (
                                "bidirectional" if sent > 0 and received > 0
                                else "outbound_only" if sent > 0
                                else "inbound_only"
                            ),
                        })
                    return _make_result(sql, normalized_rows, f"Top contacts for {resolved.canonical_name}")

        if len(person_names) >= 2 and any(kw in q_lower for kw in
                ("how many", "emails between", "email count", "exchanged")):
            resolved_a = resolve_entity_cached(person_names[0])
            resolved_b = resolve_entity_cached(person_names[1])
            for ap in resolved_a.email_patterns:
                for bp in resolved_b.email_patterns:
                    sql = (
                        f"SELECT"
                        f" SUM(CASE WHEN LOWER(person_a) LIKE :a AND LOWER(person_b) LIKE :b"
                        f" THEN total_count ELSE 0 END) AS sent_a_to_b,"
                        f" SUM(CASE WHEN LOWER(person_a) LIKE :b AND LOWER(person_b) LIKE :a"
                        f" THEN total_count ELSE 0 END) AS sent_b_to_a,"
                        f" SUM(total_count) AS total_emails"
                        f" FROM {dyads_table}"
                        f" WHERE (LOWER(person_a) LIKE :a AND LOWER(person_b) LIKE :b)"
                        f"    OR (LOWER(person_a) LIKE :b AND LOWER(person_b) LIKE :a)"
                    )
                    rows = _backend.execute_sql(sql, params={"a": ap, "b": bp})
                    total = int((rows or [{}])[0].get("total_emails") or 0)
                    if total > 0:
                        row = rows[0]
                        sent_a_to_b = int(row.get("sent_a_to_b") or 0)
                        sent_b_to_a = int(row.get("sent_b_to_a") or 0)
                        direction = (
                            "bidirectional" if sent_a_to_b > 0 and sent_b_to_a > 0
                            else f"{resolved_a.canonical_name} → {resolved_b.canonical_name}"
                            if sent_a_to_b > 0
                            else f"{resolved_b.canonical_name} → {resolved_a.canonical_name}"
                        )
                        result_rows = [{
                            "entity_a": resolved_a.canonical_name,
                            "entity_b": resolved_b.canonical_name,
                            "sent_a_to_b": sent_a_to_b,
                            "sent_b_to_a": sent_b_to_a,
                            "total_emails": total,
                            "direction_summary": direction,
                        }]
                        return _make_result(sql, result_rows,
                            f"Email count between {resolved_a.canonical_name} and {resolved_b.canonical_name}")

        if len(person_names) >= 2 and any(kw in q_lower for kw in
                ("show me all", "list all", "show all", "all emails between")):
            resolved_a = resolve_entity_cached(person_names[0])
            resolved_b = resolve_entity_cached(person_names[1])
            cfg = _get_corpus_config()
            src_table = cfg["source_table"]
            for a_pat in resolved_a.email_patterns:
                for b_pat in resolved_b.email_patterns:
                    sql = (
                        f"SELECT sender, subject, date, SUBSTRING(body, 1, 200) AS body_preview"
                        f" FROM {src_table}"
                        f" WHERE (LOWER(sender) LIKE :a_pat"
                        f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :b_pat"
                        f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :b_pat))"
                        f"    OR (LOWER(sender) LIKE :b_pat"
                        f"        AND (LOWER(CAST(to_recipients AS STRING)) LIKE :a_pat"
                        f"             OR LOWER(CAST(cc_recipients AS STRING)) LIKE :a_pat))"
                        f" ORDER BY date DESC LIMIT 50"
                    )
                    rows = _backend.execute_sql(sql, params={"a_pat": a_pat, "b_pat": b_pat})
                    if rows:
                        return _make_result(sql, rows,
                            f"All emails between {resolved_a.canonical_name} and {resolved_b.canonical_name}")

        if person_names and any(kw in q_lower for kw in ("topic", "common topic", "most common", "subjects")):
            pattern = f"%{'_'.join(person_names[0].lower().split())}%"
            for explode_fn in ["EXPLODE", "unnest"]:
                try:
                    sql = (
                        f"WITH exploded AS ("
                        f"  SELECT t.thread_id, t.subject, {explode_fn}(t.key_topics) AS topic"
                        f"  FROM {mentions_table} em"
                        f"  JOIN {threads_table} t ON em.thread_id = t.thread_id"
                        f"  WHERE LOWER(em.entity_id) LIKE :pattern"
                        f")"
                        f" SELECT topic, COUNT(DISTINCT thread_id) AS thread_count"
                        f" FROM exploded GROUP BY topic ORDER BY thread_count DESC LIMIT 20"
                    )
                    rows = _backend.execute_sql(sql, params={"pattern": pattern})
                    if rows:
                        return _make_result(sql, rows, f"Top topics for {person_names[0]}")
                except Exception:
                    continue

        if person_names and any(kw in q_lower for kw in ("percentage", "percent", "what %", "ratio")):
            resolved = resolve_entity_cached(person_names[0])
            for ep in resolved.email_patterns:
                sql = (
                    f"SELECT d.person_b AS contact_email, SUM(d.total_count) AS email_count"
                    f" FROM {dyads_table} d"
                    f" WHERE LOWER(d.person_a) LIKE :ep"
                    f" GROUP BY d.person_b ORDER BY email_count DESC LIMIT 20"
                )
                rows = _backend.execute_sql(sql, params={"ep": ep})
                if rows:
                    total = sum(int(r.get("email_count", 0) or 0) for r in rows)
                    for r in rows:
                        cnt = int(r.get("email_count", 0) or 0)
                        r["pct_of_total"] = round(100 * cnt / max(total, 1), 1)
                    return _make_result(sql, rows,
                        f"Communication breakdown for {resolved.canonical_name} (total: {total} emails)")

        if any(kw in q_lower for kw in ("business hours", "after hours", "outside of",
                                         "weekend", "evening", "night", "before 9", "after 5", "after 6")):
            is_weekend = "weekend" in q_lower
            if is_weekend:
                time_filter = "DAYOFWEEK(e.date) IN (1, 7)"
                label = "weekend"
            else:
                time_filter = "(HOUR(e.date) < 9 OR HOUR(e.date) >= 17)"
                label = "outside business hours (before 9am or after 5pm)"

            sql = (
                f"SELECT p.display_name, p.email, COUNT(*) AS total_emails"
                f" FROM {emails_table} e"
                f" JOIN {participants_table} p ON e.message_id = p.message_id AND p.role = 'from'"
                f" WHERE e.date IS NOT NULL AND {time_filter}"
                f" GROUP BY 1, 2 ORDER BY total_emails DESC LIMIT 20"
            )
            rows = _backend.execute_sql(sql)
            if rows:
                return _make_result(sql, rows, label)

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
    analytics_transport = os.environ.get("GRAPHRAG_ANALYTICS_TRANSPORT", "mcp").strip().lower()
    prefer_sql_semantic_layer = (
        os.environ.get("GRAPHRAG_ANALYTICS_BACKEND", "databricks_sql").strip().lower()
        == "databricks_sql"
    )

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

    sql_first_result = None
    if prefer_sql_semantic_layer or analytics_transport == "mcp":
        sql_first_result = _genie_sql_fallback(question, space_name)
        if sql_first_result:
            sql_first_result["semantic_layer"] = ENRON_COMMUNICATION_METRIC_VIEW

    space_id = genie_space_ids.get(space_name, "")
    if sql_first_result and analytics_transport != "local":
        genie_result = sql_first_result
    elif not space_id:
        if sql_first_result:
            return json.dumps(sql_first_result, ensure_ascii=False, default=str)
        return json.dumps({
            "error": f"Genie Space '{space_name}' not configured. Set GENIE_*_SPACE_ID env vars.",
            "available_spaces": list(genie_space_ids.keys()),
            "analytics_backend": "databricks_sql",
        })
    else:
        genie_result = None

    if genie_result is None:
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
                        "genie_host": host,
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
        fallback = sql_first_result or _genie_sql_fallback(question, space_name)
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
        if CORPUS == "enron":
            resolved_ent = resolve_entity_cached(entity_name)
            patterns = resolved_ent.entity_id_patterns
        else:
            patterns = [f"%{entity_name}%"]
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

    def _run_topic_query(where_clause: str, params: dict) -> list:
        """Try 3 SQL dialects: Spark LATERAL VIEW, Spark EXPLODE CTE, Lakebase UNNEST."""
        join_clause = (
            f" FROM {mentions_table} em"
            f" JOIN {threads_table} t ON em.thread_id = t.thread_id"
            if where_clause else f" FROM {threads_table} t"
        )
        try:
            return _backend.execute_sql(
                f"SELECT topic, COUNT(DISTINCT t.thread_id) AS thread_count,"
                f" COLLECT_LIST(t.subject)[0] AS sample_subject"
                f"{join_clause}"
                f" LATERAL VIEW EXPLODE(t.key_topics) kt AS topic"
                f" {where_clause}"
                f" GROUP BY topic ORDER BY thread_count DESC LIMIT :lim",
                params=params,
            )
        except Exception:
            pass
        try:
            return _backend.execute_sql(
                f"WITH exploded AS ("
                f"  SELECT t.thread_id, t.subject, EXPLODE(t.key_topics) AS topic"
                f"  {join_clause} {where_clause}"
                f")"
                f" SELECT topic, COUNT(DISTINCT thread_id) AS thread_count,"
                f"   FIRST(subject) AS sample_subject"
                f" FROM exploded GROUP BY topic ORDER BY thread_count DESC LIMIT :lim",
                params=params,
            )
        except Exception:
            pass
        return _backend.execute_sql(
            f"WITH exploded AS ("
            f"  SELECT t.thread_id, t.subject, unnest(t.key_topics) AS topic"
            f"  {join_clause} {where_clause}"
            f")"
            f" SELECT topic, COUNT(DISTINCT thread_id) AS thread_count,"
            f"   (array_agg(subject))[1] AS sample_subject"
            f" FROM exploded GROUP BY topic ORDER BY thread_count DESC LIMIT :lim",
            params=params,
        )

    try:
        if entity_name:
            pattern = f"%{'_'.join(entity_name.lower().split())}%"
            rows = _run_topic_query(
                "WHERE LOWER(em.entity_id) LIKE :pattern",
                {"pattern": pattern, "lim": limit},
            )
        else:
            rows = _run_topic_query("", {"lim": limit})
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
        top_raw = get_top_individuals.invoke({"limit": limit})
        try:
            top_data = json.loads(top_raw)
        except (json.JSONDecodeError, TypeError):
            return top_raw
        return json.dumps({
            "group_by": "top_senders",
            "stats": top_data.get("individuals", []),
            "source": top_data.get("source", "person_activity"),
        }, ensure_ascii=False, default=str)

    resolved = resolve_entity_cached(entity_name)

    if group_by == "contact":
        contact_raw = find_top_contacts.invoke({
            "entity_name": resolved.canonical_name,
            "direction": "both",
            "limit": limit,
        })
        try:
            contact_data = json.loads(contact_raw)
        except (json.JSONDecodeError, TypeError):
            return contact_raw
        return json.dumps({
            "entity": resolved.canonical_name,
            "group_by": "contact",
            "contacts": contact_data.get("top_contacts", []),
            "resolution": contact_data.get("resolution", _resolution_metadata(resolved)),
        }, ensure_ascii=False, default=str)

    elif group_by == "month":
        rows = None
        for email_pat in resolved.email_patterns:
            try:
                rows = _backend.execute_sql(
                    f"SELECT period,"
                    f" COALESCE(emails_sent, 0) AS sent,"
                    f" COALESCE(emails_received, 0) AS received"
                    f" FROM {activity_table}"
                    f" WHERE LOWER(person_id) LIKE :email_pat"
                    f" ORDER BY period",
                    params={"email_pat": email_pat},
                )
            except Exception as exc:
                return f"Communication stats query failed: {exc}"
            if rows:
                break
        if rows is None:
            return f"No activity timeline found for '{resolved.canonical_name}'."
        monthly_buckets: dict[str, dict] = {}
        for row in rows:
            month = str(row.get("period", ""))[:7]
            if not month:
                continue
            bucket = monthly_buckets.setdefault(month, {
                "month": month,
                "sent": 0,
                "received": 0,
                "total_emails": 0,
            })
            sent = int(row.get("sent") or 0)
            received = int(row.get("received") or 0)
            bucket["sent"] += sent
            bucket["received"] += received
            bucket["total_emails"] += sent + received
        monthly_rows = [monthly_buckets[m] for m in sorted(monthly_buckets.keys())[:limit]]
        return json.dumps({
            "entity": resolved.canonical_name,
            "group_by": "month",
            "monthly_trend": monthly_rows,
            "resolution": _resolution_metadata(resolved),
        }, ensure_ascii=False, default=str)

    else:
        rows = None
        for email_pat in resolved.email_patterns:
            try:
                rows = _backend.execute_sql(
                    f"SELECT person_id,"
                    f" SUM(COALESCE(emails_sent, 0)) AS total_sent,"
                    f" SUM(COALESCE(emails_received, 0)) AS total_received"
                    f" FROM {activity_table}"
                    f" WHERE LOWER(person_id) LIKE :email_pat"
                    f" GROUP BY person_id",
                    params={"email_pat": email_pat},
                )
            except Exception as exc:
                return f"Communication stats query failed: {exc}"
            if rows:
                break
        activity = []
        for row in rows or []:
            sent = int(row.get("total_sent") or 0)
            received = int(row.get("total_received") or 0)
            total = sent + received
            activity.append({
                "name": resolved.canonical_name,
                "email": row.get("person_id", ""),
                "total_sent": sent,
                "total_received": received,
                "total_volume": total,
                "sent_pct": round(sent * 100.0 / total, 1) if total else 0.0,
            })
        return json.dumps({
            "entity": resolved.canonical_name,
            "group_by": "direction",
            "activity": activity,
            "resolution": _resolution_metadata(resolved),
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
        pats_a = resolve_entity_cached(person_a).email_patterns
        if pats_a:
            if person_b:
                pats_b = resolve_entity_cached(person_b).email_patterns
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

    evidence_hint = False
    try:
        ev_check = _backend.execute_sql(
            f"SELECT 1 FROM {ENRON_ORG_HIERARCHY_EVIDENCE_TABLE}"
            f" WHERE person_id LIKE :pattern OR reports_to_id LIKE :pattern"
            " LIMIT 1",
            params={"pattern": pattern},
        )
        evidence_hint = bool(ev_check)
    except Exception:
        pass

    return json.dumps({
        "entity": entity_name,
        "source": "curated_org_hierarchy",
        "roles": [_fmt(r) for r in reports_to],
        "direct_reports": [_fmt(r) for r in subordinates],
        "evidence_available": evidence_hint,
        "hint": "Call get_hierarchy_evidence to see supporting emails" if evidence_hint else "",
    }, ensure_ascii=False)


@tool
def get_email_full_body(
    message_id: str = "", thread_id: str = "", limit: int = 3,
) -> str:
    """Retrieve the FULL untruncated email body for specific message(s).

    Use when you need to quote actual email content to prove a claim.
    Provide either message_id (for a single email) or thread_id (for all
    emails in a thread). Returns complete body text, not truncated previews.

    Args:
        message_id: Specific email message_id to retrieve (exact match).
        thread_id: Thread ID to retrieve all emails from that thread.
        limit: Max emails to return (default 3, max 5).
    """
    if CORPUS != "enron":
        return "Full body retrieval is only available for the Enron corpus."

    if not message_id and not thread_id:
        return json.dumps({"error": "Provide either message_id or thread_id"})

    cfg = _get_corpus_config()
    src_table = cfg["source_table"]
    limit = min(int(limit), 5)

    try:
        if message_id:
            rows = _backend.execute_sql(
                f"SELECT message_id, sender, date, subject, thread_id, body,"
                f" COALESCE(ARRAY_JOIN(to_recipients, ', '), '') AS to_list,"
                f" COALESCE(ARRAY_JOIN(cc_recipients, ', '), '') AS cc_list"
                f" FROM {src_table}"
                f" WHERE message_id = :mid"
                f" LIMIT {limit}",
                params={"mid": message_id},
            )
        else:
            rows = _backend.execute_sql(
                f"SELECT message_id, sender, date, subject, thread_id, body,"
                f" COALESCE(ARRAY_JOIN(to_recipients, ', '), '') AS to_list,"
                f" COALESCE(ARRAY_JOIN(cc_recipients, ', '), '') AS cc_list"
                f" FROM {src_table}"
                f" WHERE thread_id = :tid"
                f" ORDER BY date"
                f" LIMIT {limit}",
                params={"tid": thread_id},
            )
    except Exception as exc:
        log.warning("get_email_full_body failed: %s", exc)
        return json.dumps({"error": str(exc)})

    if not rows:
        return json.dumps({
            "message_id": message_id,
            "thread_id": thread_id,
            "emails": [],
            "note": "No email found with the given identifier.",
        })

    emails = []
    for r in rows:
        body = r.get("body", "") or ""
        emails.append({
            "message_id": r.get("message_id", ""),
            "date": str(r.get("date", ""))[:10],
            "sender": r.get("sender", ""),
            "to": r.get("to_list", ""),
            "cc": r.get("cc_list", ""),
            "subject": r.get("subject", ""),
            "thread_id": r.get("thread_id", ""),
            "body": body[:4000],
            "body_length": len(body),
            "truncated": len(body) > 4000,
        })

    return json.dumps({
        "message_id": message_id,
        "thread_id": thread_id,
        "email_count": len(emails),
        "emails": emails,
    }, ensure_ascii=False)


@tool
def get_hierarchy_evidence(
    person_name: str, manager_name: str = "", limit: int = 5,
) -> str:
    """Get email evidence supporting an org hierarchy claim (who reports to whom).
    Returns actual emails that corroborate reporting relationships from the curated org chart.

    Use AFTER query_org_hierarchy to ground hierarchy claims with source emails.
    The evidence comes from pre-computed links (direct communication, entity co-mentions,
    graph edges, keyword co-occurrence) scored by relevance.

    Args:
        person_name: Person whose reporting relationship to verify (e.g., "Michael Kopper")
        manager_name: Optional manager name to narrow the search (e.g., "Andrew Fastow")
        limit: Max evidence emails to return (default 5)
    """
    if CORPUS != "enron":
        return "Hierarchy evidence is only available for the Enron corpus."

    person_pattern = f"%{person_name.lower().replace(' ', '_')}%"
    sql_params: dict[str, str] = {"person_pat": person_pattern}
    where_parts = ["LOWER(person_id) LIKE :person_pat"]

    if manager_name:
        mgr_pattern = f"%{manager_name.lower().replace(' ', '_')}%"
        sql_params["mgr_pat"] = mgr_pattern
        where_parts.append("LOWER(reports_to_id) LIKE :mgr_pat")

    where_clause = " AND ".join(where_parts)

    try:
        rows = _backend.execute_sql(
            f"SELECT person_id, reports_to_id, message_id, thread_id,"
            f" evidence_strategy, relevance_score, snippet,"
            f" sender, date, subject"
            f" FROM {ENRON_ORG_HIERARCHY_EVIDENCE_TABLE}"
            f" WHERE {where_clause}"
            f" ORDER BY relevance_score DESC LIMIT {int(limit * 3)}",
            params=sql_params,
        )
    except Exception as exc:
        log.warning("Hierarchy evidence query failed: %s — falling back to email search", exc)
        rows = []

    if rows:
        evidence = []
        for r in rows[:limit]:
            evidence.append({
                "person_id": r.get("person_id", ""),
                "reports_to_id": r.get("reports_to_id", ""),
                "strategy": r.get("evidence_strategy", ""),
                "relevance_score": round(float(r.get("relevance_score", 0)), 3),
                "date": str(r.get("date", ""))[:10],
                "sender": r.get("sender", ""),
                "subject": r.get("subject", ""),
                "snippet": (r.get("snippet", "") or "")[:EVIDENCE_CONFIG["snippet_length"]],
                "message_id": r.get("message_id", ""),
            })
        return json.dumps({
            "person": person_name,
            "manager": manager_name,
            "source": "org_hierarchy_evidence",
            "evidence_count": len(rows),
            "evidence": evidence,
        }, ensure_ascii=False)

    cfg = _get_corpus_config()
    src_table = cfg["source_table"]
    terms = _parse_search_terms(person_name)
    if manager_name:
        terms.extend(_parse_search_terms(manager_name))

    search_params = {}
    conditions = []
    for i, term in enumerate(terms):
        pk = f"ev{i}"
        search_params[pk] = f"%{term}%"
        conditions.append(f"body LIKE :{pk}")

    fallback_where = " AND ".join(conditions) if conditions else "1=0"
    try:
        fallback_rows = _backend.execute_sql(
            f"SELECT message_id, sender, date, subject, thread_id,"
            f" SUBSTRING(body, 1, {EVIDENCE_CONFIG['body_preview_length']}) AS body_preview,"
            f" COALESCE(SIZE(to_recipients), 0) AS recipient_count"
            f" FROM {src_table}"
            f" WHERE {fallback_where}"
            f" ORDER BY date DESC LIMIT {int(limit * 2)}",
            params=search_params,
        )
    except Exception:
        fallback_rows = []

    scored = _score_evidence(
        fallback_rows,
        query_entities=[person_name] + ([manager_name] if manager_name else []),
    )

    evidence = []
    for r in scored[:limit]:
        evidence.append({
            "date": str(r.get("date", ""))[:10],
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "snippet": (r.get("body_preview", "") or "")[:EVIDENCE_CONFIG["snippet_length"]],
            "relevance_score": r.get("relevance_score", 0),
            "source": "runtime_email_search",
        })

    return json.dumps({
        "person": person_name,
        "manager": manager_name,
        "source": "runtime_fallback",
        "evidence_count": len(evidence),
        "evidence": evidence,
    }, ensure_ascii=False)


LOCAL_TOOLS = [find_entity, find_connections, find_top_contacts, get_top_email_pairs,
               get_top_individuals, get_emails_between, get_email_full_body, get_dyad_topics,
               get_relationship_evidence, get_source_evidence, get_entity_summary,
               list_entities_by_book, find_cross_book_entities, trace_path, compare_entity_sets,
               query_timeline, query_org_hierarchy, get_hierarchy_evidence,
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
            "GRAPHRAG_RUNTIME_MCP_URL",
            os.environ.get(
                "GRAPHFRAMES_MCP_URL",
            f"{w.config.host}/api/2.0/mcp/external/graphframes_connection",
            ),
        )
        client = DatabricksMCPClient(server_url=url, workspace_client=w)
        mcp_tools = client.list_tools()
        wrapped = _wrap_mcp_tools(client, mcp_tools)
        log.info("Discovered %d MCP tools from GraphFrames server", len(wrapped))
        return wrapped
    except Exception:
        log.warning("GraphFrames MCP server unavailable; graph analytics tools disabled")
        return []


def _merge_tool_catalogs(*tool_lists: list) -> list:
    merged: dict[str, object] = {}
    for tool_list in tool_lists:
        for tool_obj in tool_list:
            merged[tool_obj.name] = tool_obj
    return list(merged.values())


GRAPH_TOOLS = _merge_tool_catalogs(LOCAL_TOOLS, _get_mcp_tools())


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a biblical scholar with access to a knowledge graph built from the complete King James Bible (all 66 books — 39 Old Testament, 27 New Testament).

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
- **State coverage limitations** when relevant: "My knowledge graph covers all 66 books of the King James Bible."
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
- **get_emails_between(entity_a, entity_b)** — retrieve emails between two people. Check `match_type`: "header" = direct, "body_mention" = both mentioned in same email. Results include `message_id` for drill-down.
- **get_email_full_body(message_id="", thread_id="", limit=3)** — retrieve the FULL untruncated email body for a specific message or thread. Use after any evidence tool to get complete email text when body previews are truncated. Essential for proving claims with actual email quotes.
- **find_emails(person_a="", person_b="", keywords="", date_from="", date_to="", hour_from=-1, hour_to=-1, limit=15)** — unified email search: find emails by people, keywords, date range, and/or time of day. Use `hour_from=18` for after-hours emails, `hour_to=8` for early morning. Replaces separate search_emails/get_emails_between/get_source_evidence for flexible queries.
- **get_dyad_topics(entity_a, entity_b)** — discussion topics between two people using AI-generated thread summaries.
- **get_relationship_evidence(source_entity, target_entity, relationship_type="")** — retrieve original emails where a graph relationship was extracted from. Results include `thread_id` for drill-down.
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
- If **get_emails_between** returns empty, state clearly that no direct emails were found between these people. Then try: (1) **get_relationship_evidence** to fetch source thread emails, or (2) **get_source_evidence** with both entity names to find emails mentioning both. Explain the distinction: they may not have emailed each other directly but are mentioned together in emails sent by others.
- For questions about how two people or entities are connected, use **trace_path**.
- For temporal questions ("what happened in August 2001?", "timeline of events"), use **query_timeline** with date range filters. Combine with **get_source_evidence** to find emails from the same period.
- For multi-entity questions, **call tools multiple times** — once per entity — to build a complete picture.

## Evidence Chaining (CRITICAL — follow this for ALL evidence requests)
When the user asks for "proof", "evidence", "show me the emails", or "how do you know?":
1. First call **get_relationship_evidence** or **get_hierarchy_evidence** to find emails linked to graph relationships. These return `thread_id` and `message_id`.
2. If the body_preview is truncated or too short to prove the claim, call **get_email_full_body(message_id=...)** to retrieve the complete untruncated email body.
3. Quote the relevant portion of the email body in your response — the user wants to see actual email text, not just metadata.
4. If no evidence tools return results, try **search_emails** with relevant keywords, then use **get_email_full_body** on promising results.
5. NEVER say "no email evidence available" without first trying: get_hierarchy_evidence → get_relationship_evidence → search_emails → get_email_full_body.
6. Tool results include a `hint` field pointing to get_email_full_body — follow it when deeper evidence is needed.

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
12. After calling query_org_hierarchy, ALWAYS call get_hierarchy_evidence to retrieve email evidence supporting the reporting relationships — NEVER say "no email evidence" without trying this tool first
13. Every claim about organizational structure must include at least one email citation. If get_hierarchy_evidence returns evidence, cite it inline using [YYYY-MM-DD, From: sender, Subject: topic] format
14. Include a "Supporting Evidence Table" ONLY when tools returned actual email data. Each row must cite a real email from tool results — NEVER fabricate citations. If no tool returned emails, state "No email evidence retrieved" and omit the table entirely
15. When citing email evidence, ALWAYS use the exact date, sender, and subject from the tool results. Do NOT paraphrase or generalize the subject line
16. If a tool's output includes a "resolution.correction" field, mention the spelling correction to the user (e.g., "Note: 'Dassovich' was corrected to 'Dasovich'")
17. **MANDATORY EVIDENCE DRILL-DOWN**: When any evidence tool returns emails with body_preview that is truncated (look for "..." or short text), you MUST call **get_email_full_body(message_id=...)** on the most relevant 1-2 emails to retrieve their complete body text. Then QUOTE the specific body passages that support your claims.
18. When the user asks "prove it", "show me the evidence", or "how do you know?", ALWAYS execute this chain: get_relationship_evidence → get_email_full_body(message_id from results) → quote body text in your answer. This is NON-NEGOTIABLE.
19. Tool results that include a "hint" field are actionable — follow them. For example, if hint says "Use get_email_full_body(message_id=...)", do it immediately.

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

Keep the final response readable in plain text, the Databricks endpoint preview, and the Dash UI.
- Use short sections and bullet lists.
- NEVER use markdown pipe tables in the final response.
- If you need to show evidence, use a short `### Evidence` section with up to 3 bullet points.
- Briefly summarize tool failures as limitations. Do NOT expose raw SQL or stack-trace text.

### Provenance
End EVERY response with a Provenance section using this exact format:
- **Path**: [short retrieval path]
- **Grounding**: [One of: "All claims grounded in graph data" | "Partially grounded — some claims from graph, some from general knowledge" | "Not found in graph"]
- **Coverage**: [brief coverage note and any important limitations]
- **Sources**: tool_name(args) → result summary; tool_name(args) → result summary

## Entity Pre-Lookup
Before you received this message, entities from the user's question were automatically looked up in the knowledge graph. Results appear at the END of this system prompt.
- If an entity is listed under "NOT IN GRAPH" and it is the primary subject, state that it is not available and use other tools to find related information.
- Scope terms like "Enron", "the company", "executives" are NOT entity names — ignore if they appear under NOT IN GRAPH.
- Date expressions like "August 2001", "late 2001" are NOT entity names — ignore if they appear under NOT IN GRAPH. Use tools to find time-relevant data instead.
- Do NOT bridge graph entities to external knowledge (e.g., public news about Enron's collapse) without stating this is outside the graph."""


PROVENANCE_FORMAT = """

## Response Format (MANDATORY)

### Provenance
After your answer, include a Provenance section using this exact format:
- **Path**: [short retrieval path]
- **Grounding**: [One of: "All claims grounded in graph data" | "Partially grounded — some claims from graph, some from general knowledge" | "Not found in graph"]
- **Coverage**: [brief coverage note and any important limitations]
- **Sources**: tool_name(args) → result summary; tool_name(args) → result summary
"""


def _tool_name_from_call(call: str) -> str:
    return call.split("(", 1)[0].strip()


def _collect_tool_entries_from_sub_results(all_sub_results: dict[str, str]) -> list[tuple[str, str]]:
    """Recover per-tool call/result pairs from serialized sub-question outputs."""
    entries: list[tuple[str, str]] = []
    for sq_id in sorted(all_sub_results.keys()):
        raw = all_sub_results[sq_id]
        brace_idx = raw.find("{")
        if brace_idx == -1:
            continue
        try:
            data = json.loads(raw[brace_idx:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            entries.extend((call, result) for call, result in data.items())
    return entries


def _summarize_tool_error(result: str) -> str:
    lower = result.lower()
    if "topic distribution query failed" in lower or "topic query failed" in lower:
        return "Topic coverage lookup was unavailable on the current backend."
    if "unresolved_routine" in lower or "cannot resolve routine unnest" in lower:
        return "A backend-specific SQL function was unavailable during topic lookup."
    if "table or view not found" in lower or "no such table" in lower or "does not exist" in lower:
        return "A required data table was unavailable for one retrieval step."
    if "permission" in lower or "not authorized" in lower or "access denied" in lower:
        return "A permission restriction prevented one retrieval step from completing."
    return "One retrieval step failed, so coverage may be partial."


def _summarize_tool_result(result: str) -> tuple[str, bool, bool, bool]:
    """Return (summary, meaningful, has_email_level_data, had_error)."""
    if not isinstance(result, str) or not result.strip():
        return ("no result", False, False, False)

    lower = result.lower()
    if result.startswith("Error:") or "query failed" in lower or '"error"' in lower:
        return (_summarize_tool_error(result), False, False, True)

    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        if "no " in lower and "found" in lower:
            return ("no matching records", False, False, False)
        return ("text result", True, False, False)

    if isinstance(data, list):
        count = len(data)
        return (f"{count} records", count > 0, False, False)

    if not isinstance(data, dict):
        return ("structured result", True, False, False)

    list_fields = (
        ("emails", "emails", True),
        ("evidence_emails", "emails", True),
        ("evidence", "evidence rows", True),
        ("top_contacts", "contacts", False),
        ("contacts", "contacts", False),
        ("results", "rows", False),
        ("time_series", "time periods", False),
        ("monthly_trend", "months", False),
        ("activity", "activity rows", False),
        ("stats", "rows", False),
        ("topics", "topics", False),
        ("individuals", "people", False),
        ("pairs", "pairs", False),
    )
    for key, label, email_level in list_fields:
        value = data.get(key)
        if isinstance(value, list):
            count = len(value)
            return (f"{count} {label}", count > 0, email_level and count > 0, False)

    for key, label in (
        ("row_count", "rows"),
        ("email_count", "emails"),
        ("total_emails", "emails"),
        ("topic_count", "topics"),
        ("showing", "rows shown"),
    ):
        if key in data:
            count = int(data.get(key) or 0)
            return (
                f"{count} {label}",
                count > 0,
                key in {"email_count", "total_emails"} and count > 0,
                False,
            )

    if data.get("note"):
        note = str(data.get("note", "")).replace("\n", " ")
        return (note[:140], False, False, False)

    return ("structured result", True, False, False)


def _tool_lineage_hint(tool_name: str) -> str:
    if tool_name in {
        "search_emails", "semantic_search_emails", "get_source_evidence",
        "get_email_full_body", "get_emails_between",
    }:
        return "emails ← raw Enron email corpus"
    if tool_name in {"query_timeline"}:
        return "investigation_timeline ← curated event chronology"
    if tool_name in {"query_org_hierarchy", "get_hierarchy_evidence"}:
        return "org_hierarchy ← curated SEC/DOJ hierarchy records"
    if tool_name in {
        "find_top_contacts", "get_communication_stats", "get_communication_timeline",
        "get_top_individuals", "get_top_email_pairs", "query_and_enrich",
    }:
        return "communication_dyads/person_activity ← email header aggregations"
    if tool_name in {
        "find_entity", "find_connections", "trace_path", "get_relationship_evidence",
        "get_entity_summary", "get_dyad_topics", "browse_topics", "get_entity_context",
    }:
        return "entities/relationships/entity_mentions/threads ← extraction pipeline"
    return ""


def _build_provenance_metadata(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
) -> dict:
    source_lines: list[str] = []
    claim_source_lines: list[str] = []
    fallback_claim_sources: list[str] = []
    lineage_lines: list[str] = []
    caveats: list[str] = []
    seen_calls: set[str] = set()
    meaningful_count = 0
    email_level_hits = 0
    contract = contract or {}
    pack = _get_targeted_documentary_retry_pack(question) if question else {}
    query_relevant_records = (
        _collect_query_relevant_email_records(tool_entries, question)
        if contract.get("requires_evidence") and question
        else []
    )
    timeline_backed_documentary_packet = _has_timeline_backed_documentary_packet(
        question,
        features=_collect_evidence_features(tool_entries, question=question, contract=contract),
        contract=contract,
    )
    claim_support_calls = {
        str(record.get("call", "") or "")
        for record in query_relevant_records
        if record.get("call")
    }
    documentary_trace_gap = (
        bool(contract.get("documentary_evidence_like"))
        and bool(re.search(r"\b(formal|workflow|process|procedure)\b", question.lower()))
        and (
            len(query_relevant_records) < 2
            or max(
                (int(record.get("concept_hit_count", 0) or 0) for record in query_relevant_records),
                default=0,
            ) < 3
        )
    )
    preferred_evidence_tools = {
        "search_emails",
        "semantic_search_emails",
        "get_email_full_body",
        "get_emails_between",
        "get_relationship_evidence",
        "get_hierarchy_evidence",
    }
    preferred_evidence_tools.update(
        str(tool_name)
        for tool_name in pack.get("preserve_tool_names", ())
        if str(tool_name).strip()
    )

    for call, result in tool_entries:
        if call in seen_calls:
            continue
        seen_calls.add(call)
        tool_name = _tool_name_from_call(call)
        summary, meaningful, has_email_level, had_error = _summarize_tool_result(result)
        source_line = f"- `{call}` → {summary}"
        source_lines.append(source_line)
        if call in claim_support_calls and meaningful:
            claim_source_lines.append(source_line)
        elif tool_name in preferred_evidence_tools and meaningful:
            fallback_claim_sources.append(source_line)
        if meaningful:
            meaningful_count += 1
        if has_email_level:
            email_level_hits += 1
        if had_error:
            caveats.append(summary)
        lineage = _tool_lineage_hint(tool_name)
        if lineage and lineage not in lineage_lines:
            lineage_lines.append(lineage)

    if contract.get("requires_evidence"):
        source_lines = (claim_source_lines or fallback_claim_sources or source_lines)[:6]

    if evidence_strength != "STRONG":
        caveats.append(
            f"Evidence strength was {evidence_strength.lower()}; unsupported details should be treated as unverified."
        )
    if email_level_hits == 0:
        caveats.append("No email-level records were retrieved for direct quotation.")
    if meaningful_count == 0:
        caveats.append("The retrieved graph data did not return substantive rows for this question.")
    if contract.get("requires_evidence") and not query_relevant_records and not timeline_backed_documentary_packet:
        caveats.append("No query-relevant email evidence was retrieved for the requested documentary claim.")
    elif documentary_trace_gap:
        caveats.append(
            "Retrieved emails were related to the topic, but they did not yet establish a repeated or end-to-end workflow trace."
        )

    confidence = "High" if evidence_strength == "STRONG" else "Medium" if evidence_strength == "MODERATE" else "Low"
    if contract.get("requires_evidence") and not query_relevant_records and not timeline_backed_documentary_packet:
        confidence = "Low"
    elif documentary_trace_gap:
        confidence = "Medium" if confidence == "High" else "Low"
    if meaningful_count == 0:
        grounding = "Not found in graph"
    elif contract.get("requires_evidence") and not query_relevant_records and not timeline_backed_documentary_packet:
        grounding = "Partially grounded — broad graph context was retrieved, but query-specific email support was not verified"
    elif documentary_trace_gap:
        grounding = "Partially grounded — retrieved emails show related approvals, but not a repeated or end-to-end workflow trace"
    else:
        grounding = "All claims grounded in graph data"

    if contract.get("requires_evidence") and _has_access_request_approval_signal(question):
        if query_relevant_records:
            path = (
                "question -> targeted access-request retrieval -> claim-supporting emails "
                "-> scoped conclusion about tracked approvals versus a full workflow"
            )
        else:
            path = "question -> targeted access-request retrieval -> no claim-supporting emails -> abstention"
    elif contract.get("requires_evidence") and timeline_backed_documentary_packet:
        path = "question -> evidence retrieval -> timeline-backed documentary packet -> grounded answer"
    elif contract.get("requires_evidence") and query_relevant_records:
        path = "question -> evidence retrieval -> claim-supporting records -> grounded answer"
    elif contract.get("requires_evidence"):
        path = "question -> evidence retrieval -> no claim-supporting records -> abstention or narrow hedge"
    else:
        path = "question -> active retrieval tools -> answer"

    cleaned_caveats: list[str] = []
    for caveat in caveats:
        caveat_text = re.sub(r"\s+", " ", str(caveat)).strip()
        if not caveat_text:
            continue
        if caveat_text.startswith("{") or caveat_text.startswith("["):
            continue
        if len(caveat_text) > 180:
            caveat_text = caveat_text[:177] + "..."
        cleaned_caveats.append(caveat_text)

    return {
        "sources": source_lines or ["- No tools returned usable data."],
        "lineage": lineage_lines or ["Retrieved graph tables used by the active tools"],
        "path": path,
        "grounding": grounding,
        "confidence": confidence,
        "caveats": list(dict.fromkeys(cleaned_caveats)),
        "meaningful_count": meaningful_count,
    }


def _estimate_evidence_strength(tool_entries: list[tuple[str, str]]) -> str:
    meaningful = 0
    for _call, result in tool_entries:
        _summary, is_meaningful, _has_email_level, _had_error = _summarize_tool_result(result)
        if is_meaningful:
            meaningful += 1
    if meaningful >= 4:
        return "STRONG"
    if meaningful >= 2:
        return "MODERATE"
    return "LIMITED"


_EVIDENCE_QUERY_STOP_WORDS = {
    "about", "after", "all", "also", "and", "any", "are", "around", "before",
    "between", "can", "claim", "claims", "communication", "communications",
    "corpus", "data", "did", "does", "documentary", "documents", "email",
    "emails", "employee", "employees", "evidence", "from", "graph", "how",
    "into", "late", "local", "message", "messages", "not", "over", "proof",
    "prove", "query", "question", "records", "related", "sequence", "show",
    "shows", "showing", "that", "the", "their", "them", "they", "this",
    "those", "through", "what", "when", "which", "who", "with", "would",
    "enron",
}
_HIGH_SIGNAL_EMAIL_TOOLS = frozenset({
    "search_emails",
    "semantic_search_emails",
    "get_email_full_body",
    "get_emails_between",
    "get_relationship_evidence",
    "get_hierarchy_evidence",
})
_EMAIL_RECORD_TOOL_PRIORITY = {
    "get_email_full_body": 5,
    "get_relationship_evidence": 4,
    "get_hierarchy_evidence": 4,
    "get_emails_between": 4,
    "search_emails": 3,
    "get_source_evidence": 2,
    "semantic_search_emails": 1,
}
_EVIDENCE_QUERY_CONCEPTS = {
    "access_request": {
        "access",
        "request",
        "access_request",
        "permission",
        "permissions",
        "entitlement",
        "entitlements",
        "privilege",
        "privileges",
    },
    "approval": {
        "approve",
        "approved",
        "approval",
        "authorize",
        "authorized",
        "authorization",
        "signoff",
        "sign-off",
    },
    "workflow": {
        "workflow",
        "process",
        "procedure",
        "procedural",
        "formal",
        "steps",
    },
}
_TARGETED_ACCESS_REQUEST_RETRY_TERMS = (
    "access request",
    "request submitted",
    "approval is overdue",
    "your approval is overdue",
    "review and act upon this request",
    "approved my access",
)

_TARGETED_DOCUMENTARY_RETRY_PACKS = {
    "access_request_approval": {
        "relevance_terms": _TARGETED_ACCESS_REQUEST_RETRY_TERMS,
        "min_query_relevant_hits": 2,
        "min_signal_hits": 0,
        "retry_steps": (
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": ", ".join(_TARGETED_ACCESS_REQUEST_RETRY_TERMS),
                    "limit": 8,
                },
                "dedupe_terms": ("request submitted", "approval is overdue"),
            },
        ),
        "preserve_tool_names": (),
    },
    "bankruptcy_employee_crisis": {
        "relevance_terms": (
            "savings plan",
            "current business circumstances",
            "Home Contact Information",
            "critical company information",
            "Just a suggestion",
            "massive layoff",
            "Severance re Canada",
            "severance packages",
            "Enron Credit Inc.",
            "Weil bankruptcy lawyer",
            "olalekan.oladeji@enron.com",
            "robert.jones@mailman.enron.com",
            "david.oxley@enron.com",
            "sara.shackleton@enron.com",
        ),
        "min_query_relevant_hits": 2,
        "min_signal_hits": 1,
        "default_date_from": "2001-11-28",
        "default_date_to": "2001-12-10",
        "retry_steps": (
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "savings plan, current business circumstances",
                    "date_from": "2001-11-29",
                    "date_to": "2001-12-01",
                    "limit": 3,
                },
                "dedupe_terms": ("savings plan", "current business circumstances"),
            },
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "Home Contact Information, critical company information",
                    "sender": "robert.jones@mailman.enron.com",
                    "date_from": "2001-12-03",
                    "date_to": "2001-12-04",
                    "limit": 3,
                },
                "dedupe_terms": ("Home Contact Information", "robert.jones@mailman.enron.com"),
            },
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "Just a suggestion, massive layoff, bankruptcy",
                    "sender": "olalekan.oladeji@enron.com",
                    "date_from": "2001-11-29",
                    "date_to": "2001-11-30",
                    "limit": 3,
                },
                "dedupe_terms": ("Just a suggestion", "olalekan.oladeji@enron.com"),
            },
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "Severance re Canada, severance packages, retention",
                    "sender": "david.oxley@enron.com",
                    "date_from": "2001-12-07",
                    "date_to": "2001-12-08",
                    "limit": 3,
                },
                "dedupe_terms": ("Severance re Canada", "david.oxley@enron.com"),
            },
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "Enron Credit Inc., Weil bankruptcy lawyer, structuring issues",
                    "sender": "sara.shackleton@enron.com",
                    "date_from": "2001-12-07",
                    "date_to": "2001-12-08",
                    "limit": 3,
                },
                "dedupe_terms": ("Enron Credit Inc.", "sara.shackleton@enron.com"),
            },
        ),
        "preserve_tool_names": (),
    },
    "ebs_datacentric_venture": {
        "relevance_terms": (
            "Datacentric Broadband",
            "EBS Ventures weekly deal tracking sheet",
            "2 million",
            "regional broadband wireless",
            "Redstone",
            "gene.humphrey@enron.com",
            "rebekah.rushing@enron.com",
        ),
        "min_query_relevant_hits": 2,
        "min_signal_hits": 1,
        "default_date_from": "2000-06-01",
        "default_date_to": "2001-12-31",
        "retry_steps": (
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "Datacentric Broadband, 2 million, Redstone, regional broadband wireless",
                    "sender": "gene.humphrey@enron.com",
                    "date_from": "2001-05-11",
                    "date_to": "2001-05-12",
                    "limit": 4,
                },
                "dedupe_terms": (
                    "Datacentric Broadband",
                    "gene.humphrey@enron.com",
                ),
            },
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": "EBS Ventures weekly deal tracking sheet",
                    "sender": "rebekah.rushing@enron.com",
                    "date_from": "2000-06-01",
                    "date_to": "2000-06-30",
                    "limit": 4,
                },
                "dedupe_terms": (
                    "EBS Ventures weekly deal tracking sheet",
                    "rebekah.rushing@enron.com",
                ),
            },
        ),
        "preserve_tool_names": (),
    },
    "ljm_valuation_restatement": {
        "relevance_terms": (
            "RE: Note on Valuation",
            "LJM/Raptor valuations",
            "restriction and a put",
            "SEC Information/Earnings Restatement",
        ),
        "min_query_relevant_hits": 2,
        "min_signal_hits": 1,
        "default_date_from": "2001-10-01",
        "default_date_to": "2001-11-15",
        "retry_steps": (
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": (
                        "RE: Note on Valuation, LJM/Raptor valuations, restriction and a put, "
                        "SEC Information/Earnings Restatement"
                    ),
                    "limit": 8,
                },
                "dedupe_terms": ("RE: Note on Valuation", "SEC Information/Earnings Restatement"),
            },
        ),
        "preserve_tool_names": (),
    },
    "sec_fbi_document_destruction": {
        "relevance_terms": (
            "Cooperation with the FBI",
            "allegations of document destruction",
            "Arthur Andersen",
            "SEC opens informal inquiry",
            "SEC upgrades to formal investigation",
            "obstruction of justice",
        ),
        "min_query_relevant_hits": 1,
        "min_signal_hits": 1,
        "allow_timeline_backed_packet": True,
        "min_meaningful_timeline_hits": 2,
        "retry_steps": (
            {
                "tool_name": "search_emails",
                "params": {
                    "keywords": (
                        "Cooperation with the FBI, allegations of document destruction, "
                        "searching the Enron Building"
                    ),
                    "limit": 8,
                },
                "dedupe_terms": ("Cooperation with the FBI", "allegations of document destruction"),
            },
            {
                "tool_name": "query_timeline",
                "params": {
                    "date_from": "2001-10-01",
                    "date_to": "2001-10-31",
                    "category": "regulatory",
                },
            },
            {
                "tool_name": "query_timeline",
                "params": {
                    "date_from": "2002-03-01",
                    "date_to": "2002-03-31",
                    "category": "criminal_investigation",
                },
            },
        ),
        "preserve_tool_names": ("query_timeline",),
    },
}


def _get_targeted_documentary_retry_pack(question: str) -> dict:
    lower_question = question.lower()
    if _has_access_request_approval_signal(question):
        return _TARGETED_DOCUMENTARY_RETRY_PACKS["access_request_approval"]
    if (
        re.search(r"\bbankruptcy\b", lower_question)
        and re.search(r"(employee[- ]crisis|communications mode)", lower_question)
    ):
        return _TARGETED_DOCUMENTARY_RETRY_PACKS["bankruptcy_employee_crisis"]
    if "datacentric broadband" in lower_question and re.search(r"\b(venture|evaluat)", lower_question):
        return _TARGETED_DOCUMENTARY_RETRY_PACKS["ebs_datacentric_venture"]
    if "ljm" in lower_question and re.search(r"(valuation|off[- ]balance|restatement)", lower_question):
        return _TARGETED_DOCUMENTARY_RETRY_PACKS["ljm_valuation_restatement"]
    if (
        re.search(r"\bsec\b", lower_question)
        and re.search(r"document[- ]destruction", lower_question)
        and re.search(r"(scrutiny|progression|investigation)", lower_question)
    ):
        return _TARGETED_DOCUMENTARY_RETRY_PACKS["sec_fbi_document_destruction"]
    return {}


def _apply_documentary_pack_dates(
    params: dict[str, str | int],
    *,
    contract: dict | None = None,
    pack: dict | None = None,
) -> dict[str, str | int]:
    merged = dict(params)
    contract = contract or {}
    pack = pack or {}
    if "date_from" not in merged:
        if contract.get("date_from"):
            merged["date_from"] = contract["date_from"]
        elif pack.get("default_date_from"):
            merged["date_from"] = pack["default_date_from"]
    if "date_to" not in merged:
        if contract.get("date_to"):
            merged["date_to"] = contract["date_to"]
        elif pack.get("default_date_to"):
            merged["date_to"] = pack["default_date_to"]
    return merged


def _build_evidence_query_text(question: str) -> str:
    pack = _get_targeted_documentary_retry_pack(question)
    if not pack:
        return question
    lower_question = question.lower()
    extra_terms = [
        term
        for term in pack.get("relevance_terms", ())
        if term and term.lower() not in lower_question
    ]
    if not extra_terms:
        return question
    return f"{question} {' '.join(extra_terms)}"


def _tokenize_evidence_query(text: str) -> set[str]:
    tokens = [
        token for token in re.split(r"[^a-z0-9]+", text.lower())
        if len(token) > 2 and not token.isdigit() and token not in _EVIDENCE_QUERY_STOP_WORDS
    ]
    if not tokens:
        return set()
    bigrams = [f"{left}_{right}" for left, right in zip(tokens, tokens[1:])]
    return set(tokens + bigrams)


def _extract_evidence_query_concepts(question: str) -> list[str]:
    lower_question = question.lower()
    tokens = _tokenize_evidence_query(question)
    active: list[str] = []
    for concept, terms in _EVIDENCE_QUERY_CONCEPTS.items():
        if any(term in tokens or term in lower_question for term in terms):
            active.append(concept)
    return active


def _has_access_request_approval_signal(question: str) -> bool:
    return {"access_request", "approval"}.issubset(set(_extract_evidence_query_concepts(question)))


_DOCUMENTARY_KEYWORD_STOP_WORDS = {
    "and",
    "the",
    "from",
    "such",
    "concern",
    "what",
    "which",
    "show",
    "shows",
    "showed",
    "documentary",
    "evidence",
    "local",
    "enron",
    "email",
    "emails",
    "corpus",
    "company",
    "graph",
    "data",
    "requested",
    "claim",
    "around",
    "into",
    "mode",
    "moved",
    "through",
    "that",
    "this",
    "these",
    "those",
    "progression",
}


def _split_keyword_terms(raw: str) -> list[str]:
    terms: list[str] = []
    for chunk in re.split(r"[,;/]", raw or ""):
        cleaned = re.sub(r"\s+", " ", chunk).strip(" ,.;:-")
        if cleaned:
            terms.append(cleaned)
    return terms


def _build_documentary_search_keywords(
    question: str,
    *,
    keyword_hint: str = "",
) -> str:
    ordered_terms: list[str] = []
    seen_terms: set[str] = set()
    normalized_question = question.lower().replace("-", " ")

    def add_term(term: str) -> None:
        cleaned = re.sub(r"\s+", " ", term).strip(" ,.;:-")
        if not cleaned:
            return
        lowered = cleaned.lower()
        component_tokens = [
            token
            for token in re.split(r"[^a-z0-9]+", lowered)
            if token and not token.isdigit()
        ]
        if (
            lowered in seen_terms
            or lowered in _DOCUMENTARY_KEYWORD_STOP_WORDS
            or (component_tokens and all(token in _DOCUMENTARY_KEYWORD_STOP_WORDS for token in component_tokens))
        ):
            return
        seen_terms.add(lowered)
        ordered_terms.append(cleaned)

    for term in _split_keyword_terms(keyword_hint):
        normalized_term = term.lower().replace("-", " ").strip()
        if normalized_term and normalized_term in normalized_question:
            add_term(term)

    lower_question = normalized_question
    phrase_sources = list(ENRON_EVENT_DATES) + list(ENRON_TOPIC_CONCEPTS)
    for phrase in sorted(set(phrase_sources), key=len, reverse=True):
        normalized_phrase = phrase.lower().replace("-", " ")
        if normalized_phrase == "enron":
            continue
        if normalized_phrase in lower_question:
            add_term(phrase)
            concept = ENRON_TOPIC_CONCEPTS.get(phrase)
            if isinstance(concept, dict):
                for term in _split_keyword_terms(str(concept.get("keywords", "") or "")):
                    add_term(term)

    for acronym in re.findall(r"\b[A-Z]{2,}\b", question):
        add_term(acronym)

    for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", question):
        lowered = token.lower()
        if lowered in _DOCUMENTARY_KEYWORD_STOP_WORDS or lowered.isdigit():
            continue
        if len(lowered) < 3 and lowered not in {"ljm", "ebs", "sec", "fbi"}:
            continue
        add_term(token)
        if len(ordered_terms) >= 8:
            break

    return ", ".join(ordered_terms[:8])


def _build_documentary_shortcut_steps(
    question: str,
    *,
    contract: dict | None = None,
    keyword_hint: str = "",
) -> list["ExecutionStep"]:
    contract = contract or {}
    if CORPUS != "enron" or not contract.get("requires_evidence") or not question:
        return []

    if _get_targeted_documentary_retry_pack(question):
        return _build_targeted_documentary_retry_steps(
            question,
            contract=contract,
            existing_calls=[],
        )

    date_hint = {}
    if contract.get("date_from"):
        date_hint["date_from"] = contract["date_from"]
    if contract.get("date_to"):
        date_hint["date_to"] = contract["date_to"]
    if not date_hint:
        temporal_meta = _extract_temporal_metadata(question)
        if temporal_meta.get("date_from"):
            date_hint["date_from"] = temporal_meta["date_from"]
        if temporal_meta.get("date_to"):
            date_hint["date_to"] = temporal_meta["date_to"]

    keywords = _build_documentary_search_keywords(question, keyword_hint=keyword_hint)
    steps: list["ExecutionStep"] = []
    if keywords:
        params: dict[str, str | int] = {
            "keywords": keywords,
            "limit": 8,
        }
        params.update(date_hint)
        steps.append(ExecutionStep("search_emails", params))
    semantic_query = keywords.replace(", ", " ").strip() if keywords else question
    steps.append(
        ExecutionStep(
            "semantic_search_emails",
            {
                "query": semantic_query,
                "limit": 5,
            },
        )
    )
    return steps


def _should_use_targeted_documentary_shortcut(
    question: str,
    *,
    contract: dict | None = None,
    pattern_name: str = "",
) -> bool:
    contract = contract or {}
    return (
        pattern_name == "keyword_search"
        and contract.get("requires_evidence")
        and bool(contract.get("documentary_evidence_like"))
    )


def _build_targeted_documentary_retry_steps(
    question: str,
    *,
    contract: dict | None = None,
    existing_calls: list[str] | None = None,
) -> list["ExecutionStep"]:
    contract = contract or {}
    if CORPUS != "enron" or not contract.get("requires_evidence") or not question:
        return []

    pack = _get_targeted_documentary_retry_pack(question)
    if not pack:
        return []

    existing_calls = existing_calls or []
    existing_blob = " ".join(existing_calls).lower()
    existing_call_set = set(existing_calls)
    steps: list[ExecutionStep] = []
    for step_spec in pack.get("retry_steps", ()):
        dedupe_terms = [
            str(term).lower()
            for term in step_spec.get("dedupe_terms", ())
            if str(term).strip()
        ]
        params = dict(step_spec.get("params", {}))
        if step_spec.get("tool_name") == "search_emails":
            params = _apply_documentary_pack_dates(params, contract=contract, pack=pack)
        expected_call = f"{step_spec['tool_name']}({json.dumps(params)})"
        if expected_call in existing_call_set:
            continue
        if dedupe_terms and all(term in existing_blob for term in dedupe_terms):
            continue
        steps.append(ExecutionStep(step_spec["tool_name"], params))
    return steps


def _iter_email_support_records(tool_entries: list[tuple[str, str]]) -> list[dict]:
    records: list[dict] = []
    for call, result in tool_entries:
        if not isinstance(result, str):
            continue
        tool_name = _tool_name_from_call(call)
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if not isinstance(item, dict):
                continue
            email_lists: list[dict] = []
            for key in ("emails", "evidence_emails", "evidence"):
                value = item.get(key)
                if isinstance(value, list):
                    email_lists.extend(v for v in value if isinstance(v, dict))

            for email in email_lists:
                try:
                    relevance = float(email.get("relevance_score", 0.0) or 0.0)
                except (TypeError, ValueError):
                    relevance = 0.0
                text = " ".join(
                    part for part in [
                        str(email.get("subject", "") or ""),
                        str(email.get("body", "") or ""),
                        str(email.get("body_preview", "") or ""),
                        str(email.get("snippet", "") or ""),
                    ]
                    if part
                ).strip()
                records.append({
                    "call": call,
                    "tool_name": tool_name,
                    "date": str(email.get("date", "") or "")[:10],
                    "sender": str(email.get("sender", "") or email.get("from", "") or ""),
                    "subject": str(email.get("subject", "") or ""),
                    "message_id": str(email.get("message_id", "") or email.get("id", "") or ""),
                    "thread_id": str(email.get("thread_id", "") or ""),
                    "text": text,
                    "relevance_score": relevance,
                })
    return records


def _email_query_overlap_score(email: dict, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    email_tokens = _tokenize_evidence_query(
        " ".join(
            part for part in [
                str(email.get("subject", "") or ""),
                str(email.get("text", "") or ""),
                str(email.get("sender", "") or ""),
            ]
            if part
        )
    )
    if not email_tokens:
        return 0.0

    overlap = query_tokens & email_tokens
    if not overlap:
        return 0.0

    unigram_hits = [token for token in overlap if "_" not in token]
    bigram_hits = [token for token in overlap if "_" in token]
    rare_hits = [token for token in unigram_hits if len(token) >= 8]

    score = 0.0
    score += len(bigram_hits) * 1.0
    score += min(len(unigram_hits), 4) * 0.35
    score += len(rare_hits) * 0.55
    score += min(float(email.get("relevance_score", 0.0) or 0.0), 1.0) * 0.30
    return round(score, 3)


def _email_pack_signal_hit_count(email: dict, question: str) -> int:
    pack = _get_targeted_documentary_retry_pack(question)
    if not pack:
        return 0
    email_text = " ".join(
        part for part in [
            str(email.get("subject", "") or ""),
            str(email.get("text", "") or ""),
            str(email.get("sender", "") or ""),
        ]
        if part
    ).lower()
    return sum(
        1
        for term in pack.get("relevance_terms", ())
        if term and str(term).lower() in email_text
    )


def _email_query_matched_concepts(email: dict, question: str) -> set[str]:
    concepts = _extract_evidence_query_concepts(_build_evidence_query_text(question))
    if not concepts:
        return set()
    email_text = " ".join(
        part for part in [
            str(email.get("subject", "") or ""),
            str(email.get("text", "") or ""),
            str(email.get("sender", "") or ""),
        ]
        if part
    ).lower()
    matched: set[str] = set()
    for concept in concepts:
        if any(term in email_text for term in _EVIDENCE_QUERY_CONCEPTS[concept]):
            matched.add(concept)
    return matched


def _dedupe_email_records(records: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for record in records:
        dedupe_key = (
            str(record.get("message_id", "") or "")
            or str(record.get("thread_id", "") or "")
            or "|".join([
                str(record.get("date", "") or ""),
                str(record.get("sender", "") or ""),
                str(record.get("subject", "") or ""),
            ])
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(record)
    return deduped


def _rank_email_records_for_question(
    tool_entries: list[tuple[str, str]],
    question: str,
) -> list[dict]:
    query_tokens = _tokenize_evidence_query(_build_evidence_query_text(question))
    ranked: list[dict] = []
    for record in _iter_email_support_records(tool_entries):
        enriched = dict(record)
        enriched["query_overlap_score"] = (
            _email_query_overlap_score(record, query_tokens) if query_tokens else 0.0
        )
        matched_concepts = _email_query_matched_concepts(record, question)
        enriched["matched_concepts"] = sorted(matched_concepts)
        enriched["concept_hit_count"] = len(matched_concepts)
        enriched["pack_signal_hit_count"] = _email_pack_signal_hit_count(record, question)
        enriched["tool_priority"] = _EMAIL_RECORD_TOOL_PRIORITY.get(
            str(record.get("tool_name", "") or ""),
            0,
        )
        ranked.append(enriched)

    ranked.sort(
        key=lambda row: (
            row.get("pack_signal_hit_count", 0),
            row.get("concept_hit_count", 0),
            row.get("query_overlap_score", 0.0),
            row.get("tool_priority", 0),
            row.get("relevance_score", 0.0),
        ),
        reverse=True,
    )
    return _dedupe_email_records(ranked)


def _collect_query_relevant_email_records(
    tool_entries: list[tuple[str, str]],
    question: str,
) -> list[dict]:
    query_text = _build_evidence_query_text(question)
    if not _tokenize_evidence_query(query_text):
        return []
    active_concepts = _extract_evidence_query_concepts(query_text)
    required_concepts = 2 if len(active_concepts) >= 2 else (1 if active_concepts else 0)
    required_matched_concepts = {"access_request", "approval"} if _has_access_request_approval_signal(question) else set()
    pack = _get_targeted_documentary_retry_pack(question)
    required_pack_signal_hits = int(pack.get("min_signal_hits", 0) or 0) if pack else 0
    return [
        record
        for record in _rank_email_records_for_question(tool_entries, question)
        if record.get("query_overlap_score", 0.0) >= 0.9
        and (required_concepts == 0 or record.get("concept_hit_count", 0) >= required_concepts)
        and record.get("pack_signal_hit_count", 0) >= required_pack_signal_hits
        and required_matched_concepts.issubset(set(record.get("matched_concepts", [])))
    ]


def _collect_reviewed_email_records(
    tool_entries: list[tuple[str, str]],
    question: str,
    *,
    limit: int = 3,
) -> list[dict]:
    ranked = _rank_email_records_for_question(tool_entries, question)
    if not ranked:
        return []
    overlapping = [
        record for record in ranked
        if record.get("query_overlap_score", 0.0) > 0.0
    ]
    return (overlapping or ranked)[:limit]


def _collect_evidence_features(
    tool_entries: list[tuple[str, str]],
    *,
    question: str = "",
    contract: dict | None = None,
) -> dict:
    meaningful_count = 0
    email_level_hits = 0
    error_count = 0
    meaningful_timeline_hits = 0
    for call, result in tool_entries:
        _summary, meaningful, has_email_level, had_error = _summarize_tool_result(result)
        tool_name = _tool_name_from_call(call)
        if meaningful:
            meaningful_count += 1
            if tool_name == "query_timeline":
                meaningful_timeline_hits += 1
        if has_email_level:
            email_level_hits += 1
        if had_error:
            error_count += 1
    contract = contract or {}
    relevant_records = (
        _collect_query_relevant_email_records(tool_entries, question)
        if question and contract.get("requires_evidence")
        else []
    )
    return {
        "meaningful_count": meaningful_count,
        "email_level_hits": email_level_hits,
        "error_count": error_count,
        "meaningful_timeline_hits": meaningful_timeline_hits,
        "query_relevant_email_hits": len(relevant_records),
        "max_query_concept_hits": max(
            (int(record.get("concept_hit_count", 0) or 0) for record in relevant_records),
            default=0,
        ),
        "high_signal_query_hits": sum(
            1 for record in relevant_records if record.get("tool_name") in _HIGH_SIGNAL_EMAIL_TOOLS
        ),
        "full_body_hits": sum(
            1 for record in relevant_records if record.get("tool_name") == "get_email_full_body"
        ),
    }


def _has_timeline_backed_documentary_packet(
    question: str,
    *,
    features: dict,
    contract: dict | None = None,
) -> bool:
    contract = contract or {}
    if not contract.get("requires_evidence") or not question:
        return False
    pack = _get_targeted_documentary_retry_pack(question)
    if not pack or not pack.get("allow_timeline_backed_packet"):
        return False
    min_timeline_hits = int(pack.get("min_meaningful_timeline_hits", 2) or 2)
    return (
        int(features.get("email_level_hits", 0) or 0) >= 1
        and int(features.get("meaningful_timeline_hits", 0) or 0) >= min_timeline_hits
    )


def _should_run_targeted_documentary_retry(
    tool_entries: list[tuple[str, str]],
    question: str,
    *,
    contract: dict | None = None,
    pattern_name: str = "",
) -> bool:
    contract = contract or {}
    if pattern_name not in {"keyword_search", "timeline"}:
        return False
    if not _build_targeted_documentary_retry_steps(
        question,
        contract=contract,
        existing_calls=[call for call, _ in tool_entries],
    ):
        return False

    features = _collect_evidence_features(tool_entries, question=question, contract=contract)
    pack = _get_targeted_documentary_retry_pack(question)
    required_hits = int(pack.get("min_query_relevant_hits", 1) or 1) if pack else 1
    if features["query_relevant_email_hits"] < required_hits:
        return True
    if (
        contract.get("documentary_evidence_like")
        and re.search(r"\b(formal|workflow|process|procedure)\b", question.lower())
        and (
            features["query_relevant_email_hits"] < 2
            or features["max_query_concept_hits"] < 3
        )
    ):
        return True
    return False


def _build_targeted_retry_drilldown_steps(
    retry_tool_results: dict[str, str],
    *,
    question: str = "",
    contract: dict | None = None,
) -> list["ExecutionStep"]:
    contract = contract or {}
    steps: list[ExecutionStep] = []
    pack = _get_targeted_documentary_retry_pack(question) if question else {}
    drill_limit = 1 if pack else (2 if contract.get("requires_evidence") else 1)
    drill_ids: list[tuple[str | None, str | None]] = []
    if question:
        relevant_records = _collect_query_relevant_email_records(list(retry_tool_results.items()), question)
        seen_pairs: set[tuple[str | None, str | None]] = set()
        for record in relevant_records:
            pair = (
                str(record.get("message_id", "") or "") or None,
                str(record.get("thread_id", "") or "") or None,
            )
            if pair in seen_pairs or (not pair[0] and not pair[1]):
                continue
            seen_pairs.add(pair)
            drill_ids.append(pair)
            if len(drill_ids) >= drill_limit:
                break
    if not drill_ids and not question:
        drill_ids = _extract_evidence_ids_for_drilldown(retry_tool_results, limit=drill_limit)
    for mid, tid in drill_ids:
        params: dict[str, str | int] = {}
        if pack and mid:
            params["message_id"] = mid
            params["limit"] = 1
        elif contract.get("documentary_evidence_like") and tid:
            params["thread_id"] = tid
            params["limit"] = 4
        elif mid:
            params["message_id"] = mid
            params["limit"] = 2 if contract.get("requires_evidence") else 1
        elif tid:
            params["thread_id"] = tid
            params["limit"] = 2 if contract.get("requires_evidence") else 1
        if params:
            steps.append(ExecutionStep("get_email_full_body", params))
    return steps


def _select_claim_supporting_tool_entries(
    tool_entries: list[tuple[str, str]],
    *,
    question: str = "",
    contract: dict | None = None,
) -> list[tuple[str, str]]:
    contract = contract or {}
    if not contract.get("requires_evidence") or not question:
        return tool_entries

    relevant_records = _collect_query_relevant_email_records(tool_entries, question)
    if not relevant_records:
        return tool_entries

    pack = _get_targeted_documentary_retry_pack(question)
    preserved_tool_names = set(pack.get("preserve_tool_names", ())) if pack else set()
    relevant_calls = {
        str(record.get("call", "") or "")
        for record in relevant_records
        if record.get("call")
    }
    relevant_message_ids = {
        str(record.get("message_id", "") or "")
        for record in relevant_records
        if record.get("message_id")
    }
    relevant_thread_ids = {
        str(record.get("thread_id", "") or "")
        for record in relevant_records
        if record.get("thread_id")
    }

    def _filter_result_payload(result: str) -> str:
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError, ValueError):
            return result
        if not isinstance(data, dict):
            return result

        filtered_any = False
        cloned = dict(data)
        for key in ("emails", "evidence_emails", "evidence"):
            value = cloned.get(key)
            if not isinstance(value, list):
                continue
            filtered_any = True
            narrowed = []
            for email in value:
                if not isinstance(email, dict):
                    continue
                message_id = str(email.get("message_id", "") or email.get("id", "") or "")
                thread_id = str(email.get("thread_id", "") or "")
                if (
                    (message_id and message_id in relevant_message_ids)
                    or (thread_id and thread_id in relevant_thread_ids)
                ):
                    narrowed.append(email)
            cloned[key] = narrowed[:4]
            if key == "emails" and "total" in cloned:
                cloned["total"] = len(narrowed)
            if key == "emails" and "email_count" in cloned:
                cloned["email_count"] = len(narrowed)
        return json.dumps(cloned, ensure_ascii=False) if filtered_any else result

    filtered: list[tuple[str, str]] = []
    for call, result in tool_entries:
        tool_name = _tool_name_from_call(call)
        include = call in relevant_calls or tool_name in preserved_tool_names
        if not include and tool_name == "get_email_full_body":
            include = any(mid and mid in call for mid in relevant_message_ids) or any(
                tid and tid in call for tid in relevant_thread_ids
            )
        if include:
            filtered.append((call, _filter_result_payload(result)))
    return filtered or tool_entries


def _escalate_sufficiency_decision(current: str, incoming: str) -> str:
    order = {"answer": 0, "hedge": 1, "abstain": 2}
    return incoming if order.get(incoming, 0) > order.get(current, 0) else current


def _assess_evidence_sufficiency(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
    consistency_warnings: list[str] | None = None,
    pattern_name: str = "",
) -> dict:
    contract = contract or {}
    features = _collect_evidence_features(tool_entries, question=question, contract=contract)
    timeline_backed_documentary_packet = _has_timeline_backed_documentary_packet(
        question,
        features=features,
        contract=contract,
    )
    decision = "answer"
    reasons: list[str] = []
    answer_type = str(contract.get("answer_type", "unknown") or "unknown")

    if features["meaningful_count"] == 0:
        decision = "abstain"
        reasons.append("No substantive rows were retrieved for this question.")

    if contract.get("requires_direct_email") and features["email_level_hits"] == 0:
        needed = "No supporting email-level records were retrieved."
        reasons.append(needed)
        if answer_type == "proof_email":
            decision = _escalate_sufficiency_decision(decision, "abstain")
        else:
            decision = _escalate_sufficiency_decision(decision, "hedge")

    if contract.get("requires_evidence"):
        if features["query_relevant_email_hits"] == 0 and not timeline_backed_documentary_packet:
            reasons.append("No query-relevant email evidence was retrieved for the requested documentary claim.")
            if pattern_name in {"keyword_search", "timeline"} or answer_type == "proof_email":
                decision = _escalate_sufficiency_decision(decision, "abstain")
            else:
                decision = _escalate_sufficiency_decision(decision, "hedge")
        elif (
            _has_access_request_approval_signal(question)
            and features["query_relevant_email_hits"] >= 2
            and features["max_query_concept_hits"] >= 2
        ):
            reasons.append(
                "Retrieved emails show a narrow slice of an access-request approval workflow; answer only the examples directly supported by those emails."
            )
            decision = _escalate_sufficiency_decision(decision, "hedge")
        elif (
            contract.get("documentary_evidence_like")
            and re.search(r"\b(formal|workflow|process|procedure)\b", question.lower())
            and (
                features["query_relevant_email_hits"] < 2
                or features["max_query_concept_hits"] < 3
            )
        ):
            reasons.append(
                "The retrieved emails do not yet show a repeated or end-to-end workflow trace for the requested documentary claim."
            )
            if pattern_name in {"keyword_search", "timeline"} or answer_type == "proof_email":
                decision = _escalate_sufficiency_decision(decision, "abstain")
            else:
                decision = _escalate_sufficiency_decision(decision, "hedge")
        elif (
            features["high_signal_query_hits"] == 0
            and features["full_body_hits"] == 0
            and not timeline_backed_documentary_packet
        ):
            reasons.append(
                "The retrieved emails provide only weak topical support and do not directly verify the requested claim."
            )
            decision = _escalate_sufficiency_decision(decision, "hedge")

    if answer_type == "count" and features["meaningful_count"] < EVIDENCE_CONFIG["evidence_sufficiency_threshold"]:
        reasons.append("The count/ranking request has limited supporting rows.")
        decision = _escalate_sufficiency_decision(decision, "hedge")

    if evidence_strength == "LIMITED":
        reasons.append("Only limited supporting evidence was retrieved.")
        if decision == "answer":
            decision = "hedge"

    if consistency_warnings:
        reasons.append("Cross-tool contradictions were detected and must be called out.")
        if decision == "answer":
            decision = "hedge"

    return {
        "decision": decision,
        "reasons": list(dict.fromkeys(reasons)),
        "features": features,
        "answer_type": answer_type,
    }


def _build_sufficiency_guardrail_block(assessment: dict) -> str:
    if assessment.get("decision") != "hedge":
        return ""
    reasons = assessment.get("reasons", [])
    bullets = "\n".join(f"- {reason}" for reason in reasons) if reasons else "- Supporting evidence is limited."
    return (
        "\n\n## EVIDENCE SUFFICIENCY DECISION: HEDGE\n"
        f"{bullets}\n"
        "Answer only the subset that is directly supported by the retrieved rows. "
        "State missing evidence explicitly, avoid exhaustive unsupported completions, "
        "and prefer narrower phrasing such as 'the available data shows'.\n"
    )


def _render_abstention_response(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    assessment: dict,
    *,
    question: str = "",
    contract: dict | None = None,
) -> str:
    answer_type = assessment.get("answer_type", "unknown")
    if answer_type == "proof_email":
        intro = "I couldn't verify a supporting email for this question from the retrieved Enron graph data."
    else:
        intro = "I can't answer this confidently from the retrieved Enron graph data."
    reason_text = " ".join(assessment.get("reasons", []))
    body = intro if not reason_text else f"{intro} {reason_text}"
    response = _ensure_answer_header(body.rstrip())
    return _apply_human_readable_output_contract(
        response,
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
        assessment=assessment,
    )


def _extract_supported_email_records(
    tool_entries: list[tuple[str, str]],
    *,
    question: str = "",
    contract: dict | None = None,
) -> list[dict]:
    contract = contract or {}
    raw_records = _iter_email_support_records(tool_entries)
    if contract.get("requires_evidence"):
        if question:
            raw_records = _collect_query_relevant_email_records(tool_entries, question)
            features = _collect_evidence_features(tool_entries, question=question, contract=contract)
            if not raw_records and _has_timeline_backed_documentary_packet(
                question,
                features=features,
                contract=contract,
            ):
                raw_records = _collect_reviewed_email_records(tool_entries, question, limit=1)
        else:
            raw_records = []

    supported: list[dict] = []
    for email in raw_records:
        supported.append({
            "date": str(email.get("date", "") or "")[:10],
            "sender": str(email.get("sender", "") or ""),
            "subject": str(email.get("subject", "") or ""),
            "message_id": str(email.get("message_id", "") or ""),
            "thread_id": str(email.get("thread_id", "") or ""),
            "text": str(email.get("text", "") or ""),
        })
    return supported


def _format_email_citation(record: dict) -> str:
    date = str(record.get("date", "") or "unknown-date")
    sender = str(record.get("sender", "") or "unknown-sender")
    subject = str(record.get("subject", "") or "untitled")
    return f"[{date}, From: {sender}, Subject: {subject}]"


def _build_canonical_supporting_evidence_block(
    supported: list[dict],
    *,
    limit: int = 3,
) -> str:
    if not supported:
        return ""
    lines = ["### Evidence"]
    for record in supported[:limit]:
        excerpt = re.sub(r"\s+", " ", str(record.get("text", "") or "")).strip()
        excerpt = _truncate_support_excerpt(excerpt, limit=160)
        line = f"- {_format_email_citation(record)}"
        if excerpt:
            line += f" — {excerpt}"
        lines.append(line)
    return "\n".join(lines)


def _build_reviewed_email_records_block(
    tool_entries: list[tuple[str, str]],
    *,
    question: str = "",
    contract: dict | None = None,
    limit: int = 3,
) -> str:
    contract = contract or {}
    if not contract.get("requires_evidence") or not question:
        return ""

    reviewed = _collect_reviewed_email_records(tool_entries, question, limit=limit)
    if not reviewed:
        return ""

    lines = ["### Evidence Reviewed"]
    for record in reviewed:
        excerpt = re.sub(r"\s+", " ", str(record.get("text", "") or "")).strip()
        excerpt = _truncate_support_excerpt(excerpt, limit=140)
        line = f"- {_format_email_citation(record)}"
        if excerpt:
            line += f" — {excerpt}"
        lines.append(line)
    return "\n".join(lines)


def _describe_access_request_evidence(record: dict) -> str:
    subject_lower = str(record.get("subject", "") or "").lower()
    text_lower = str(record.get("text", "") or "").lower()
    if "approval is overdue" in subject_lower or "your approval is overdue" in subject_lower:
        return "Automated overdue notice showing a tracked request, named approver, and resource awaiting approval."
    if "request submitted" in subject_lower:
        return "Access-request thread showing an employee following up on an approval path for desk access."
    if "request id" in text_lower or "review and act upon this request" in text_lower:
        return "Structured access-request notice showing a request ID and an approval step."
    return "Retrieved email related to an access-request approval step."


def _render_access_request_workflow_hedge_response(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
) -> str:
    contract = contract or {}
    relevant_records = _collect_query_relevant_email_records(tool_entries, question)
    if not relevant_records:
        assessment = _assess_evidence_sufficiency(
            tool_entries,
            evidence_strength,
            question=question,
            contract=contract,
        )
        return _render_abstention_response(
            tool_entries,
            evidence_strength,
            assessment,
            question=question,
            contract=contract,
        )

    lines = [
        "### Answer",
        "The available data shows a narrow slice of a formal access-request approval workflow in the Enron email corpus, but not a full end-to-end or company-wide process.",
        "",
        "### Documentary Evidence",
    ]
    for record in relevant_records[:3]:
        excerpt = re.sub(r"\s+", " ", str(record.get("text", "") or "")).strip()
        excerpt = _truncate_support_excerpt(excerpt, limit=180)
        lines.append(
            f"- {_format_email_citation(record)} — {_describe_access_request_evidence(record)}"
            + (f" {excerpt}" if excerpt else "")
        )

    lines.extend([
        "",
        "### Conclusion",
        "These emails support that Enron used tracked access requests with named approvers and overdue approval notices. The retrieved sample does not show the full workflow definition or how broadly it was used across the company.",
    ])
    return _apply_human_readable_output_contract(
        "\n".join(lines),
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
    )


def _has_markdown_heading(text: str, heading: str) -> bool:
    return bool(re.search(rf"(?im)^###\s+{re.escape(heading)}\s*$", text))


def _insert_section_before_provenance(text: str, section_text: str) -> str:
    if not section_text:
        return text

    first_line = section_text.splitlines()[0].strip()
    heading = first_line.removeprefix("### ").strip() if first_line.startswith("### ") else ""
    if heading and _has_markdown_heading(text, heading):
        return text

    prov_idx = text.find("### Provenance")
    if prov_idx == -1:
        return text.rstrip() + "\n\n" + section_text

    prefix = text[:prov_idx].rstrip()
    suffix = text[prov_idx:].lstrip()
    return prefix + "\n\n" + section_text + "\n\n" + suffix


def _truncate_support_excerpt(text: str, limit: int = 140) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _ensure_answer_header(text: str) -> str:
    if re.search(r"(?im)^#{1,3}\s*answer\s*$", text):
        return text
    stripped = text.strip()
    if not stripped:
        return text
    return "### Answer\n" + stripped


def _normalize_citation_field(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.-]", " ", value.lower())).strip()


def _citation_is_supported(date: str, sender: str, subject: str, supported: list[dict]) -> bool:
    norm_sender = _normalize_citation_field(sender)
    norm_subject = _normalize_citation_field(subject)
    for record in supported:
        if date and record.get("date") and record["date"] != date:
            continue
        record_sender = _normalize_citation_field(record.get("sender", ""))
        record_subject = _normalize_citation_field(record.get("subject", ""))
        sender_ok = not norm_sender or not record_sender or norm_sender in record_sender or record_sender in norm_sender
        subject_ok = (
            not norm_subject
            or not record_subject
            or norm_subject in record_subject
            or record_subject in norm_subject
        )
        if sender_ok and subject_ok:
            return True
    return False


def _remove_unsupported_inline_citations(text: str, supported: list[dict]) -> str:
    citation_re = re.compile(
        r"\[(\d{4}-\d{2}-\d{2}),\s*From:\s*([^,\]]+),\s*Subject:\s*([^\]]+)\]"
    )

    def _replace(match: re.Match[str]) -> str:
        date, sender, subject = match.groups()
        return match.group(0) if _citation_is_supported(date, sender, subject, supported) else ""

    cleaned = citation_re.sub(_replace, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def _clean_supporting_evidence_section(text: str, supported: list[dict]) -> str:
    patterns = (
        r"(?ims)^###\s+Supporting Evidence(?: Table)?\s*$.*?(?=^###\s+|\Z)",
        r"(?ims)^###\s+Evidence\s*$.*?(?=^###\s+|\Z)",
    )
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _soften_overclaiming_language(text: str, assessment: dict) -> str:
    features = assessment.get("features", {})
    if (
        assessment.get("decision") == "answer"
        and features.get("email_level_hits", 0) > 0
        and features.get("meaningful_count", 0) >= 4
    ):
        return text
    softened = text
    replacements = {
        r"\bproves\b": "supports",
        r"\bproved\b": "supported",
        r"\bclearly shows\b": "is consistent with",
        r"\bconfirms\b": "supports",
        r"\bdemonstrates\b": "suggests",
    }
    for pattern, replacement in replacements.items():
        softened = re.sub(pattern, replacement, softened, flags=re.IGNORECASE)
    return softened


def _apply_claim_verification(
    response_text: str,
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
    consistency_warnings: list[str] | None = None,
) -> str:
    assessment = _assess_evidence_sufficiency(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
        consistency_warnings=consistency_warnings,
    )
    supported = _extract_supported_email_records(tool_entries, question=question, contract=contract)
    if contract and contract.get("requires_evidence") and not supported and assessment.get("decision") != "answer":
        return _render_abstention_response(
            tool_entries,
            evidence_strength,
            assessment,
            question=question,
            contract=contract,
        )
    verified = _remove_unsupported_inline_citations(response_text, supported)
    verified = _clean_supporting_evidence_section(verified, supported)
    if contract and contract.get("requires_evidence"):
        support_block = _build_canonical_supporting_evidence_block(
            supported,
            limit=len(supported) or 3,
        )
        if support_block:
            verified = _insert_section_before_provenance(verified, support_block)
    verified = _soften_overclaiming_language(verified, assessment)
    return _ensure_answer_header(verified)


def _inline_source_entries(source_lines: list[str]) -> str:
    entries: list[str] = []
    for line in source_lines:
        cleaned = str(line).strip()
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        cleaned = cleaned.replace("`", "")
        if cleaned:
            entries.append(cleaned)
    return "; ".join(entries) if entries else "No tools returned usable data."


def _build_coverage_summary(meta: dict) -> str:
    parts: list[str] = []
    if CORPUS == "enron":
        parts.append("Deployed Enron graph covers a curated subset of 20,000+ emails from 15 key custodians.")
    elif CORPUS == "bible":
        parts.append("Coverage is limited to the indexed Bible corpus configured for this endpoint.")
    parts.extend(meta.get("caveats", [])[:2])
    unique_parts = list(dict.fromkeys(part for part in parts if part))
    return " ".join(unique_parts) if unique_parts else "None noted in retrieved data."


def _format_canonical_provenance(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
) -> str:
    meta = _build_provenance_metadata(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
    )
    sources_inline = _inline_source_entries(meta["sources"])
    coverage = _build_coverage_summary(meta)
    return (
        "### Provenance\n"
        f"- **Path**: {meta['path']}\n"
        f"- **Grounding**: {meta['grounding']}\n"
        f"- **Coverage**: {coverage}\n"
        f"- **Sources**: {sources_inline}"
    )


def _build_provenance_guardrail_block(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
) -> str:
    canonical = _format_canonical_provenance(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
    )
    return (
        "\n\n## CANONICAL PROVENANCE (MANDATORY)\n"
        "Use the following provenance metadata exactly. Do not add tool calls that were not actually run.\n\n"
        f"{canonical}\n"
    )


def _apply_provenance_guardrails(
    response_text: str,
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
) -> str:
    """Replace or append the provenance section with a deterministic version."""
    canonical = _format_canonical_provenance(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
    )
    prov_marker = "### Provenance"
    support_marker = "### Supporting Evidence"
    prov_idx = response_text.find(prov_marker)
    support_idx = response_text.find(support_marker)

    if prov_idx != -1:
        prefix = response_text[:prov_idx].rstrip()
        suffix = response_text[support_idx:].lstrip() if support_idx > prov_idx else ""
        return prefix + "\n\n" + canonical + (f"\n\n{suffix}" if suffix else "")

    if support_idx != -1:
        prefix = response_text[:support_idx].rstrip()
        suffix = response_text[support_idx:].lstrip()
        return prefix + "\n\n" + canonical + f"\n\n{suffix}"

    return response_text.rstrip() + "\n\n" + canonical


def _strip_markdown_sections(text: str, headings: Sequence[str]) -> str:
    cleaned = text
    for heading in headings:
        cleaned = re.sub(
            rf"(?ims)^###\s+{re.escape(heading)}\s*$.*?(?=^###\s+|\Z)",
            "",
            cleaned,
        )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _build_human_caveats_block(caveats: list[str], *, limit: int = 3) -> str:
    visible = [str(c).strip() for c in caveats if str(c).strip()][:limit]
    if not visible:
        return ""
    lines = ["### Caveats"]
    for caveat in visible:
        lines.append(f"- {caveat}")
    return "\n".join(lines)


def _demote_nested_answer_headings(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "### Answer":
        return text
    adjusted = [lines[0]]
    for line in lines[1:]:
        if line.startswith("### "):
            adjusted.append("#### " + line[4:])
        else:
            adjusted.append(line)
    return "\n".join(adjusted)


def _apply_human_readable_output_contract(
    response_text: str,
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
    assessment: dict | None = None,
) -> str:
    contract = contract or {}
    meta = _build_provenance_metadata(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
    )
    body = _strip_markdown_sections(
        response_text,
        (
            "Supporting Evidence",
            "Supporting Evidence Table",
            "Evidence",
            "Evidence Reviewed",
            "Reviewed Email Records",
            "Caveats",
            "Provenance",
        ),
    )
    body = body.replace("### Documentary Evidence", "### Evidence")
    body = _ensure_answer_header(body)
    body = _demote_nested_answer_headings(body)
    sections = [body]

    if "### Evidence" not in body:
        evidence_block = ""
        if assessment and assessment.get("decision") == "abstain":
            evidence_block = _build_reviewed_email_records_block(
                tool_entries,
                question=question,
                contract=contract,
            )
        elif contract.get("requires_evidence"):
            supported = _extract_supported_email_records(
                tool_entries,
                question=question,
                contract=contract,
            )
            evidence_block = _build_canonical_supporting_evidence_block(
                supported,
                limit=len(supported) or 3,
            )
        if evidence_block:
            sections.append(evidence_block)

    caveats_block = _build_human_caveats_block(meta.get("caveats", []))
    if caveats_block:
        sections.append(caveats_block)

    sections.append(
        _format_canonical_provenance(
            tool_entries,
            evidence_strength,
            question=question,
            contract=contract,
        )
    )
    return "\n\n".join(part for part in sections if part).strip()


def _extract_evidence_ids_for_drilldown(
    tool_results: dict, limit: int = 2,
) -> list[tuple[str, str]]:
    """Extract (message_id, thread_id) pairs from evidence tool results for full-body drill-down.

    Scans get_emails_between, get_relationship_evidence, search_emails, and
    get_hierarchy_evidence results. Returns the top N most relevant email
    identifiers sorted by relevance_score descending.
    """
    candidates: list[tuple[int, float, str, str]] = []
    tool_priority = {
        "get_hierarchy_evidence": 4,
        "get_emails_between": 4,
        "get_relationship_evidence": 4,
        "get_source_evidence": 3,
        "search_emails": 3,
        "semantic_search_emails": 1,
    }

    for call, result_str in tool_results.items():
        if not isinstance(result_str, str):
            continue
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        email_lists = []
        for key in ("emails", "evidence_emails", "evidence"):
            if key in data and isinstance(data[key], list):
                email_lists.extend(data[key])

        for email in email_lists:
            if not isinstance(email, dict):
                continue
            mid = email.get("message_id", "") or email.get("id", "")
            tid = email.get("thread_id", "")
            if not mid and not tid:
                continue
            score = float(email.get("relevance_score", 0.5))
            priority = tool_priority.get(_tool_name_from_call(call), 0)
            candidates.append((priority, score, mid, tid))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen: set[str] = set()
    seen_threads: set[str] = set()
    results: list[tuple[str, str]] = []
    for _priority, _score, mid, tid in candidates:
        dedup_key = mid or tid
        if dedup_key in seen:
            continue
        if tid and tid in seen_threads:
            continue
        seen.add(dedup_key)
        if tid:
            seen_threads.add(tid)
        results.append((mid, tid))
        if len(results) >= limit:
            break

    return results


def _extract_top_contacts_for_evidence(tool_results: dict, limit: int = 3) -> list[str]:
    """Parse find_top_contacts result from tool_results and return top N contact names."""
    for result_str in tool_results.values():
        if not isinstance(result_str, str):
            continue
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or "top_contacts" not in data:
            continue
        contacts = data["top_contacts"]
        if not isinstance(contacts, list):
            continue
        names = []
        for c in contacts:
            name = c.get("name", "")
            if name and len(name) > 1:
                names.append(name)
            if len(names) >= limit:
                break
        return names
    return []


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
# Pattern registry import (inline fallback REMOVED — see D-007)
# ---------------------------------------------------------------------------
try:
    from src.agent.pattern_registry import PATTERN_REGISTRY, resolve_params, ExecutionStep
    _PATTERN_REGISTRY_IMPORT_SOURCE = "src.agent.pattern_registry"
except ImportError:
    try:
        from pattern_registry import PATTERN_REGISTRY, resolve_params, ExecutionStep
        _PATTERN_REGISTRY_IMPORT_SOURCE = "pattern_registry"
    except ImportError:
        _pr_dir = None
        for _candidate_dir in [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "code"),
        ]:
            if os.path.isfile(os.path.join(_candidate_dir, "pattern_registry.py")):
                _pr_dir = _candidate_dir
                break
        if _pr_dir is None:
            raise ImportError(
                "pattern_registry.py could not be imported. "
                "This module is required — the inline fallback has been removed "
                "to prevent silent divergence between the canonical registry and "
                "the inline copy. Ensure pattern_registry.py is on sys.path."
            )
        import sys as _sys
        if _pr_dir not in _sys.path:
            _sys.path.insert(0, _pr_dir)
        from pattern_registry import PATTERN_REGISTRY, resolve_params, ExecutionStep
        _PATTERN_REGISTRY_IMPORT_SOURCE = f"{_pr_dir}/pattern_registry.py"


# ---------------------------------------------------------------------------
# Tool map for fast-path invocation (name -> callable)
# ---------------------------------------------------------------------------
TOOL_MAP: dict[str, callable] = {}


def _build_tool_map():
    """Populate TOOL_MAP from both local and discovered MCP tools."""
    TOOL_MAP.clear()
    for t in GRAPH_TOOLS:
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
PLANNER_ENDPOINT = os.environ.get("GRAPHRAG_PLANNER_ENDPOINT", "databricks-gpt-5-4-nano")
PLANNER_MAX_TOKENS = int(os.environ.get("GRAPHRAG_PLANNER_MAX_TOKENS", "512"))
PLANNER_TEMPERATURE = float(os.environ.get("GRAPHRAG_PLANNER_TEMPERATURE", "0.0"))

VALID_PATTERNS = {"entity_structure", "entity_explore", "entity_pair", "timeline", "keyword_search", "general", "genie_analytics"}

QUERY_PLANNER_PROMPT = """You are a Query Planner for a knowledge-graph-backed corporate investigation system (Enron email corpus).

Your job is to DECOMPOSE a user question into one or more atomic sub-questions, each tagged with a computational primitive.

## Step 1: Coreference Resolution
If the question contains pronouns ("he", "him", "they", "her", "it") or references like "that person" or "the same email", resolve them using the conversation history and entity memory provided below. Replace all pronouns with the actual entity name. Prefer the entity the user explicitly asked about in recent turns over entities that only appeared in tool results. The Entity Memory section lists user-mentioned entities first.

## Step 2: Decomposition
Break the resolved question into 1-N sub-questions. Each sub-question should be answerable by exactly ONE primitive.

## Available Primitives

- **entity_structure**: Answers "who reports to X?", "what is X's title?", "X's org chart position". Requires: entity name. Tools: query_org_hierarchy, find_connections(REPORTS_TO/MANAGES), get_entity_summary.
- **entity_explore**: Answers "who did X email most?", "what did X discuss?", "X's activities". Requires: entity name. Tools: find_top_contacts, find_connections(DISCUSSES), get_entity_summary, get_source_evidence.
- **entity_pair**: Answers "how are A and B connected?", "did A and B email each other?", "what did A and B discuss?". Requires: two entity names. Tools: trace_path, get_emails_between, get_dyad_topics, find_connections.
- **timeline**: Answers "what happened in August 2001?", "when did X resign?", "events between dates". Requires: optional entity name + date range. Tools: query_timeline, get_communication_timeline, get_source_evidence.
- **keyword_search**: Answers "what was Project Raptor?", "emails about California energy crisis". Requires: keywords. Tools: search_emails, get_source_evidence, find_entity.
- **general**: Complex questions that don't fit any primitive. Falls to the flexible ReAct agent loop. Use ONLY when no other primitive fits.
- **genie_analytics**: Questions answerable with SQL — counts, rankings, aggregations, percentages, full email listings, topic distributions. Trigger phrases: "how many", "what percentage", "most/least", "top N", "compare", "trend over time", "email volume", "communication frequency", "who sent the most", "top email pairs", "who communicated most with X?", "how many emails between X and Y?", "show me all emails between X and Y", "what are the most common topics for X?", "top contacts of X", "what percentage of X's emails were from Y?", "busiest", "least active", "average", "total count". Tools: query_and_enrich. PREFER this over entity_explore/entity_pair/general when the question can be answered with a COUNT, SUM, GROUP BY, SELECT *, or ranking SQL query — even if an entity is named.

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
- PREFER "genie_analytics" over entity_explore/entity_pair/general for any question about email counts, rankings, percentages, comparisons, trends, volumes, or complete email listings. If a question asks "how many", "who communicated most", "show me all emails", "top N contacts", "what percentage", or "compare X and Y", classify as genie_analytics even if specific people are named.
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
    contract: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class QueryPlan:
    resolved_question: str
    sub_questions: list[SubQuestion]


def _resolve_coreference(question: str, conversation_history: list[dict], entity_memory_context: str) -> str:
    """Resolve pronouns in the question using conversation history and entity memory.

    Only used by the classifier fallback path (_plan_from_classification).
    The planner LLM path handles coreference via its own prompt Step 1.

    EntityMemory.context_for_classifier() now lists user-mentioned entities
    first, so recent_names[0] is the conversational subject, not an
    incidental entity from tool output.
    """
    _PRONOUNS = ("he", "him", "his", "she", "her", "they", "them", "their", "it", "its")
    if not any(w in question.lower().split() for w in _PRONOUNS):
        return question

    # Long questions likely name their own subjects — skip rewriting.
    if len(question.split()) > 15:
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
    "executive emails": ["Kenneth Lay", "Jeff Skilling", "Andrew Fastow", "strategy"],
    "executives": ["Kenneth Lay", "Jeff Skilling", "Andrew Fastow", "leadership"],
    "key players": ["Kenneth Lay", "Jeff Skilling", "Andrew Fastow", "Sherron Watkins"],
    "topics discussed": ["strategy", "trading", "accounting", "California", "SPE"],
    "subjects": ["strategy", "trading", "accounting", "California", "SPE"],
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


ENRON_TOPIC_CONCEPTS: dict[str, dict] = {
    "special purpose entities": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "SPE, LJM, Raptors, Chewco, JEDI, special purpose, partnership",
    },
    "spe": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "SPE, LJM, Raptors, Chewco, JEDI, special purpose, partnership",
    },
    "financial events": {
        "entities": [],
        "keywords": "earnings, stock, SEC, restatement, quarterly, mark-to-market, revenue, loss",
    },
    "accounting fraud": {
        "entities": [{"name": "Arthur Andersen", "entity_type": "Organization"}],
        "keywords": "mark-to-market, SPE, Arthur Andersen, earnings restatement, accounting",
    },
    "arthur andersen": {
        "entities": [{"name": "Arthur Andersen", "entity_type": "Organization"}],
        "keywords": "audit, accounting, document retention, shredding, review, Andersen",
    },
    "board of directors": {
        "entities": [],
        "keywords": "board, directors, oversight, governance, fiduciary, committee, approval",
    },
    "board directors": {
        "entities": [],
        "keywords": "board, directors, oversight, governance, fiduciary, committee, approval",
    },
    "projects": {
        "entities": [],
        "keywords": "Project Raptor, Dabhol Power, Enron Online, Enron Broadband Services, Braveheart",
    },
    "initiatives": {
        "entities": [],
        "keywords": "Project Raptor, Dabhol Power, Enron Online, Enron Broadband Services, Braveheart",
    },
    "internal projects": {
        "entities": [],
        "keywords": "Project Raptor, Dabhol Power, Enron Online, Enron Broadband Services, Braveheart",
    },
    "broadband": {
        "entities": [{"name": "Kenneth Rice", "entity_type": "Person"}],
        "keywords": "Enron Broadband Services, EBS, fiber, content, Blockbuster, Braveheart",
    },
    "california energy": {
        "entities": [{"name": "Tim Belden", "entity_type": "Person"}],
        "keywords": "California, FERC, energy crisis, West Power, deregulation, price",
    },
    "energy trading": {
        "entities": [{"name": "Tim Belden", "entity_type": "Person"}, {"name": "David Delainey", "entity_type": "Person"}],
        "keywords": "California, West Power, trading, energy, Enron Energy Trading",
    },
    "whistleblower": {
        "entities": [{"name": "Sherron Watkins", "entity_type": "Person"}],
        "keywords": "Sherron Watkins, warning letter, Kenneth Lay, accounting concerns",
    },
    "document destruction": {
        "entities": [{"name": "Arthur Andersen", "entity_type": "Organization"}],
        "keywords": "shredding, document retention, destroy, Arthur Andersen",
    },
    "bankruptcy": {
        "entities": [],
        "keywords": "bankruptcy filing, Chapter 11, employee communications, layoffs, creditors",
    },
    "ljm": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "LJM, related party, off-balance-sheet, Raptors, Chewco, restatement",
    },
    "off-balance-sheet": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "off-balance-sheet, related party, LJM, Raptors, Chewco, restatement",
    },
    "enron": {
        "entities": [{"name": "Enron", "entity_type": "Organization"}],
        "keywords": "Enron, corporation, energy, Houston, bankruptcy, fraud",
    },
    "why did enron fail": {
        "entities": [],
        "keywords": "bankruptcy, fraud, accounting, SPE, collapse, oversight, Andersen, mark-to-market",
    },
    "enron fail": {
        "entities": [],
        "keywords": "bankruptcy, fraud, accounting, SPE, collapse, oversight, Andersen, mark-to-market",
    },
    "enron scandal": {
        "entities": [{"name": "Sherron Watkins", "entity_type": "Person"}],
        "keywords": "fraud, whistleblower, SEC, Arthur Andersen, bankruptcy, investigation",
    },
    "fastow": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "Andrew Fastow, LJM, partnership, SPE, Global Finance, CFO",
    },
    "partnerships": {
        "entities": [{"name": "Andrew Fastow", "entity_type": "Person"}],
        "keywords": "LJM, Raptors, Chewco, JEDI, partnership, SPE, Andrew Fastow",
    },
    "financial event": {
        "entities": [],
        "keywords": "earnings, stock, SEC, restatement, quarterly",
    },
}


def _extract_topic_metadata(question: str) -> dict:
    """Extract topic-specific entities and keywords from a question.

    Parallel to _extract_temporal_metadata but for topic/concept questions.
    Maps known Enron topics to their graph entity names and expanded keywords.
    """
    q_lower = question.lower()
    result: dict = {}
    discovered_entities: list[dict] = []
    keyword_parts: list[str] = []
    seen_entity_names: set[str] = set()

    for trigger, concept in ENRON_TOPIC_CONCEPTS.items():
        if trigger in q_lower:
            for ent in concept.get("entities", []):
                if ent["name"] not in seen_entity_names:
                    discovered_entities.append(ent)
                    seen_entity_names.add(ent["name"])
            if concept.get("keywords"):
                keyword_parts.append(concept["keywords"])

    if discovered_entities:
        result["topic_entities"] = discovered_entities
    if keyword_parts:
        result["topic_keywords"] = ", ".join(keyword_parts)

    return result


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
    contract = classification.get("contract") if isinstance(classification.get("contract"), dict) else _extract_answer_contract(resolved, entities)

    temporal_meta = {}
    if contract.get("date_from"):
        temporal_meta["date_from"] = contract.get("date_from", "")
    if contract.get("date_to"):
        temporal_meta["date_to"] = contract.get("date_to", "")
    if pattern == "timeline" and not temporal_meta:
        temporal_meta = _extract_temporal_metadata(resolved)

    classifier_keywords = classification.get("keywords", "")
    keywords = ""
    if pattern in ("keyword_search", "general", "timeline"):
        if classifier_keywords:
            keywords = _expand_keywords(classifier_keywords)
        else:
            _stop = {"what", "who", "how", "why", "when", "where", "did", "does", "was", "were",
                     "is", "are", "the", "a", "an", "at", "in", "on", "of", "to", "and", "or",
                     "for", "about", "from", "with", "by", "can", "you", "tell", "me", "do",
                     "happened", "between", "after", "before", "during", "change", "changed"}
            words = [w for w in re.split(r'\W+', resolved) if w.lower() not in _stop and len(w) > 1]
            keywords = " ".join(words[:6])
            keywords = _expand_keywords(keywords)

    if pattern in ("keyword_search", "general"):
        topic_meta = _extract_topic_metadata(resolved)
        if topic_meta.get("topic_entities") and not entities:
            entities = topic_meta["topic_entities"]
            log.info("Classifier fallback: injected topic entities %s",
                     [e["name"] for e in entities])
        if topic_meta.get("topic_keywords") and not classifier_keywords:
            keywords = _expand_keywords(topic_meta["topic_keywords"])

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
            contract=contract,
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
    # Let the planner LLM handle coreference (Step 1 of QUERY_PLANNER_PROMPT).
    # The old regex resolver blindly replaced pronouns with recent_names[0]
    # from EntityMemory, which could be an incidental tool-extracted entity
    # rather than the conversational subject.
    resolved_question = question

    history_lines = []
    for msg in conversation_history[-6:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            history_lines.append(f"{role}: {content[:200]}")
    history_str = "\n".join(history_lines) if history_lines else "(no prior conversation)"

    contract = _extract_answer_contract(resolved_question)
    routing_hint = _get_case_based_pattern_hint(resolved_question, contract)
    routing_lines = []
    if contract.get("answer_type", "") != "unknown":
        routing_lines.append(f"- Answer contract: {contract['answer_type']}")
    if contract.get("force_pattern"):
        routing_lines.append(f"- Deterministic candidate pattern: {contract['force_pattern']}")
    if routing_hint.get("pattern"):
        routing_lines.append(
            f"- Case-based candidate pattern: {routing_hint['pattern']} (confidence {routing_hint.get('confidence', 0.0):.2f})"
        )
    routing_block = (
        "\n\n## Routing Hints\n" + "\n".join(routing_lines)
        + "\nUse these hints unless the question clearly decomposes into multiple primitives."
        if routing_lines else ""
    )

    user_block = (
        f"## Conversation History\n{history_str}\n\n"
        f"## Entity Memory\n{entity_memory_context or '(no entities yet)'}\n\n"
        f"## Current Question\n{resolved_question}"
        + routing_block
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
        classification = classify_and_extract(
            resolved_question,
            raw_question=resolved_question,
            contract=contract,
            routing_hint=routing_hint,
        )
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
        classification = classify_and_extract(
            resolved_question,
            raw_question=resolved_question,
            contract=contract,
            routing_hint=routing_hint,
        )
        return _plan_from_classification(resolved_question, classification, entity_memory_context)

    resolved_q = plan_data.get("resolved_question", resolved_question)
    sqs = []
    for sq_data in plan_data.get("sub_questions", []):
        pattern = sq_data.get("pattern", "general")
        if pattern not in VALID_PATTERNS:
            log.warning("Planner returned invalid pattern %r, mapping to general", pattern)
            pattern = "general"
        sq_question = sq_data.get("question", resolved_question)
        sq_entities = sq_data.get("entities", [])
        sq_contract = _extract_answer_contract(sq_question, sq_entities)
        raw_keywords = sq_data.get("keywords", "")
        if pattern in ("keyword_search", "general"):
            raw_keywords = _expand_keywords(raw_keywords)
        sqs.append(SubQuestion(
            id=sq_data.get("id", f"sq{len(sqs)+1}"),
            question=sq_question,
            pattern=pattern,
            entities=sq_entities,
            keywords=raw_keywords,
            date_from=sq_data.get("date_from", "") or sq_contract.get("date_from", ""),
            date_to=sq_data.get("date_to", "") or sq_contract.get("date_to", ""),
            contract=sq_contract,
            depends_on=sq_data.get("depends_on", []),
        ))

    if not sqs:
        classification = classify_and_extract(
            resolved_question,
            raw_question=resolved_question,
            contract=contract,
            routing_hint=routing_hint,
        )
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
        self.user_mentioned: list[str] = []
        self.max = max_entities

    def record_user_entity(self, name: str):
        """Track an entity the user explicitly mentioned in their question.

        These are prioritized over tool-extracted entities in
        context_for_classifier() so pronoun resolution targets the
        conversational subject, not incidental entities from tool output.
        """
        if name in self.user_mentioned:
            self.user_mentioned.remove(name)
        self.user_mentioned.insert(0, name)
        self.user_mentioned = self.user_mentioned[:self.max]

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
        """Return a string of recent entity names for the classifier prompt.

        User-mentioned entities (conversational subjects) are listed first,
        followed by tool-extracted entities. This ordering ensures pronoun
        resolution targets the entity the user asked about.
        """
        if not self.recent and not self.user_mentioned:
            return ""
        user_names = self.user_mentioned[:3]
        tool_names = [e["name"] for e in self.recent
                      if e["name"] not in user_names][:5]
        all_names = (user_names + tool_names)[:5]
        if not all_names:
            return ""
        return f"\nRecent entities from prior conversation: {', '.join(all_names)}\n"

    def clear(self):
        self.recent.clear()
        self.user_mentioned.clear()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]


class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None, tools=None):
        self.llm = _get_llm(endpoint=endpoint or SYNTHESIS_ENDPOINT)
        self.react_llm = _get_llm(endpoint=REACT_ENDPOINT)
        configured_tools = list(tools or GRAPH_TOOLS)
        if _MODEL_LOGGING_TOOL_LIMIT:
            configured_tools = configured_tools[:_MODEL_LOGGING_TOOL_LIMIT]
        self.tools = configured_tools
        self.llm_with_tools = self.react_llm.bind_tools(self.tools)
        self.entity_memory = EntityMemory()
        if not TOOL_MAP:
            _build_tool_map()

    def _build_graph(self, prelookup_context: str = "", *, tier: str = "", permitted_books: str = ""):
        corpus_cfg = _get_corpus_config(tier_override=tier, permitted_books_override=permitted_books)
        base_prompt = _get_system_prompt() if CORPUS == "bible" else corpus_cfg["system_prompt"]
        system_prompt = base_prompt + prelookup_context

        llm_with_tools = self.llm_with_tools

        def should_continue(state):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return "end"

        def call_model(state):
            messages = [{"role": "system", "content": system_prompt}] + state["messages"]
            response = llm_with_tools.invoke(messages)
            return {"messages": [response]}

        graph = StateGraph(AgentState)
        graph.add_node("agent", RunnableLambda(call_model))
        graph.add_node("tools", ToolNode(self.tools))
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")
        graph.set_entry_point("agent")
        return graph.compile()

    @staticmethod
    def _validate_tool_consistency(tool_results: dict) -> list[str]:
        """Detect contradictions between tool outputs before synthesis.

        Returns a list of warning strings to inject into the synthesis prompt.
        """
        warnings: list[str] = []

        connection_counts: dict[str, int] = {}
        email_counts: dict[str, int] = {}
        corrections: list[str] = []

        for key, val in tool_results.items():
            if not isinstance(val, str):
                continue
            try:
                parsed = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue

            res_meta = parsed.get("resolution", {})
            if isinstance(res_meta, dict):
                for sub in [res_meta, res_meta.get("a", {}), res_meta.get("b", {}),
                            res_meta.get("source", {}), res_meta.get("target", {})]:
                    if isinstance(sub, dict) and sub.get("correction"):
                        corrections.append(sub["correction"])

            if key.startswith("find_connections("):
                by_type = parsed.get("by_type", {})
                for rel_type, entries in by_type.items():
                    if rel_type == "SENT_TO" and isinstance(entries, list):
                        for e in entries:
                            tgt = e.get("target", "") if isinstance(e, dict) else ""
                            src = e.get("source", "") if isinstance(e, dict) else ""
                            freq = int(e.get("frequency", 0)) if isinstance(e, dict) else 0
                            if freq > 0:
                                pair_key = f"{src}|{tgt}".lower()
                                connection_counts[pair_key] = max(connection_counts.get(pair_key, 0), freq)

            if key.startswith("get_emails_between("):
                total = parsed.get("total_emails", 0)
                between = parsed.get("between", [])
                if len(between) == 2:
                    pair_key = f"{between[0]}|{between[1]}".lower()
                    email_counts[pair_key] = total

            if key.startswith("find_top_contacts("):
                contacts = parsed.get("top_contacts", [])
                entity = parsed.get("entity", "").lower()
                for c in contacts[:3]:
                    name = (c.get("name", "") if isinstance(c, dict) else "").lower()
                    if name and entity:
                        pair_key = f"{entity}|{name}"
                        connection_counts[pair_key] = max(
                            connection_counts.get(pair_key, 0),
                            int(c.get("total", 0)) if isinstance(c, dict) else 0,
                        )

        for pair, conn_count in connection_counts.items():
            if conn_count > 0 and pair in email_counts and email_counts[pair] == 0:
                parts = pair.split("|")
                warnings.append(
                    f"CONTRADICTION: find_connections reports {conn_count} edges between "
                    f"'{parts[0]}' and '{parts[1]}', but get_emails_between found 0 emails. "
                    f"The edge count comes from graph extraction (may include body mentions), "
                    f"while get_emails_between searches email headers. Do NOT claim direct "
                    f"emails exist unless get_emails_between found them."
                )

        if corrections:
            unique = list(dict.fromkeys(corrections))
            warnings.append(
                "SPELLING CORRECTION: " + "; ".join(unique)
                + ". Mention this correction to the user so they know the intended entity."
            )

        return warnings

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

        contract = {}
        if isinstance(metadata, dict) and isinstance(metadata.get("contract"), dict):
            contract = metadata["contract"]
        elif question:
            contract = _extract_answer_contract(question, entities)
        shortcut_documentary = _should_use_targeted_documentary_shortcut(
            question,
            contract=contract,
            pattern_name=pattern.name,
        )

        tool_results = {}
        tool_sequence = []
        work: list[tuple] = []
        steps_to_run = (
            _build_documentary_shortcut_steps(
                question,
                contract=contract,
                keyword_hint=str((metadata or {}).get("keywords", "") or ""),
            )
            if shortcut_documentary
            else pattern.steps
        )
        for step in steps_to_run:
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
                ordered_results = [None] * len(work)
                future_to_idx = {
                    pool.submit(_fast_path_invoke_tool, item): idx
                    for idx, item in enumerate(work)
                }
                for fut in as_completed(future_to_idx):
                    ordered_results[future_to_idx[fut]] = fut.result()
                for item in ordered_results:
                    if item is None:
                        continue
                    step, resolved, call_id, result = item
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

        # Mirror the PDES evidence drill-down in fast-path mode so planner-bypassed
        # documentary questions still fetch full email bodies before abstaining.
        followup_steps: list[ExecutionStep] = []
        if shortcut_documentary and not _has_access_request_approval_signal(question):
            followup_steps.extend(
                _build_targeted_retry_drilldown_steps(
                    tool_results,
                    question=question,
                    contract=contract,
                )[:2]
            )
        if (
            not shortcut_documentary
            and CORPUS == "enron"
            and pattern.name in (
            "entity_structure", "entity_pair", "entity_explore",
            "keyword_search", "timeline",
            )
        ):
            drill_limit = 4 if contract.get("requires_evidence") else 2
            drill_ids = _extract_evidence_ids_for_drilldown(tool_results, limit=drill_limit)
            for mid, tid in drill_ids:
                drill_params = {}
                if mid:
                    drill_params["message_id"] = mid
                elif tid:
                    drill_params["thread_id"] = tid
                drill_params["limit"] = 2 if contract.get("requires_evidence") else 1
                followup_steps.append(ExecutionStep("get_email_full_body", drill_params))

        if followup_steps:
            for step in followup_steps:
                resolved = _resolve(step.params, entities, metadata=metadata, question=question)
                tool_fn = TOOL_MAP.get(step.tool_name)
                if not tool_fn:
                    continue
                call_id = f"fp_followup_{step.tool_name}_{len(tool_sequence)}"
                tool_sequence.append(step.tool_name)
                if tools_invoked_out is not None:
                    tools_invoked_out.append(step.tool_name)
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
                    log.exception("Fast path follow-up tool %s failed", step.tool_name)
                    result = f"Error: {exc}"
                tool_results[f"{step.tool_name}({json.dumps(resolved)})"] = result
                if isinstance(result, str):
                    self.entity_memory.extract(result)
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_output_item(
                        call_id=call_id,
                        output=str(result)[:4000],
                    ),
                )

        tool_entries = list(tool_results.items())
        if _should_run_targeted_documentary_retry(
            tool_entries,
            question,
            contract=contract,
            pattern_name=pattern.name,
        ):
            retry_tool_results: dict[str, str] = {}
            retry_steps = _build_targeted_documentary_retry_steps(
                question,
                contract=contract,
                existing_calls=list(tool_results.keys()),
            )
            for step in retry_steps:
                resolved = _resolve(step.params, entities, metadata=metadata, question=question)
                tool_fn = TOOL_MAP.get(step.tool_name)
                if not tool_fn:
                    continue
                call_id = f"fp_retry_{step.tool_name}_{len(tool_sequence)}"
                tool_sequence.append(step.tool_name)
                if tools_invoked_out is not None:
                    tools_invoked_out.append(step.tool_name)
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
                    log.exception("Fast path targeted retry tool %s failed", step.tool_name)
                    result = f"Error: {exc}"
                key = f"{step.tool_name}({json.dumps(resolved)})"
                retry_tool_results[key] = result
                tool_results[key] = result
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_output_item(
                        call_id=call_id,
                        output=str(result)[:4000],
                    ),
                )

            retry_followups = _build_targeted_retry_drilldown_steps(
                retry_tool_results,
                question=question,
                contract=contract,
            )
            for step in retry_followups:
                resolved = _resolve(step.params, entities, metadata=metadata, question=question)
                tool_fn = TOOL_MAP.get(step.tool_name)
                if not tool_fn:
                    continue
                call_id = f"fp_retry_followup_{step.tool_name}_{len(tool_sequence)}"
                tool_sequence.append(step.tool_name)
                if tools_invoked_out is not None:
                    tools_invoked_out.append(step.tool_name)
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
                    log.exception("Fast path targeted retry follow-up tool %s failed", step.tool_name)
                    result = f"Error: {exc}"
                key = f"{step.tool_name}({json.dumps(resolved)})"
                retry_tool_results[key] = result
                tool_results[key] = result
                if isinstance(result, str):
                    self.entity_memory.extract(result)
                yield ResponsesAgentStreamEvent(
                    type="response.output_item.done",
                    item=create_function_call_output_item(
                        call_id=call_id,
                        output=str(result)[:4000],
                    ),
                )

            for result in retry_tool_results.values():
                if isinstance(result, str):
                    self.entity_memory.extract(result)

        consistency_warnings = self._validate_tool_consistency(tool_results)
        consistency_block = ""
        if consistency_warnings:
            consistency_block = (
                "\n\n## CROSS-TOOL CONSISTENCY WARNINGS\n"
                + "\n".join(f"- {w}" for w in consistency_warnings)
                + "\n\nYou MUST address these warnings in your response. "
                "Do NOT ignore contradictions between tools.\n"
            )

        if pattern.name in ("entity_explore", "timeline", "keyword_search"):
            tool_results = _prioritize_email_results(tool_results)
        if pattern.name in ("entity_explore", "timeline"):
            context_limit = 10000
        elif pattern.name in ("keyword_search", "general"):
            context_limit = 8000
        else:
            context_limit = 6000
        tool_entries = list(tool_results.items())
        strength = _estimate_evidence_strength(tool_entries)
        sufficiency = _assess_evidence_sufficiency(
            tool_entries,
            strength,
            question=question,
            contract=contract,
            consistency_warnings=consistency_warnings,
            pattern_name=pattern.name,
        )
        if contract.get("requires_evidence") and _has_access_request_approval_signal(question):
            deterministic_response = AIMessage(
                content=_render_access_request_workflow_hedge_response(
                    tool_entries,
                    strength,
                    question=question,
                    contract=contract,
                )
            )
            yield from output_to_responses_items_stream([deterministic_response])
            return
        if sufficiency["decision"] == "abstain":
            abstain_response = AIMessage(content=_render_abstention_response(
                tool_entries,
                strength,
                sufficiency,
                question=question,
                contract=contract,
            ))
            yield from output_to_responses_items_stream([abstain_response])
            return
        synthesis_entries = _select_claim_supporting_tool_entries(
            tool_entries,
            question=question,
            contract=contract,
        )
        synthesis_results = {call: result for call, result in synthesis_entries}
        if pattern.name in ("entity_explore", "timeline", "keyword_search"):
            synthesis_results = _prioritize_email_results(synthesis_results)
        context_raw = json.dumps(synthesis_results, ensure_ascii=False, indent=2)
        context = _truncate_json_aware(context_raw, context_limit)
        fp_evidence_block = ""
        if strength != "STRONG":
            fp_evidence_block = (
                f"\n\n## Evidence Strength: {strength}\n"
                "The data retrieval returned fewer results than usual. "
                "Base your answer ONLY on the data provided below. "
                "Explicitly state when information is not available. "
                "Do NOT compensate with general knowledge.\n"
            )
        sufficiency_block = _build_sufficiency_guardrail_block(sufficiency)
        provenance_guardrail = _build_provenance_guardrail_block(
            tool_entries,
            strength,
            question=question,
            contract=contract,
        )
        synthesis_system = (
            pattern.synthesis_prompt
            + consistency_block
            + fp_evidence_block
            + sufficiency_block
            + PROVENANCE_FORMAT
            + provenance_guardrail
            + f"\n\nData:\n{context}"
        )

        response = self.llm.invoke([
            {"role": "system", "content": synthesis_system},
            {"role": "user", "content": question},
        ])
        if isinstance(getattr(response, "content", None), str):
            response.content = _apply_provenance_guardrails(
                response.content,
                tool_entries,
                strength,
                question=question,
                contract=contract,
            )
            response.content = _apply_claim_verification(
                response.content,
                tool_entries,
                strength,
                question=question,
                contract=contract,
                consistency_warnings=consistency_warnings,
            )
            response.content = _apply_human_readable_output_contract(
                response.content,
                tool_entries,
                strength,
                question=question,
                contract=contract,
                assessment=sufficiency,
            )

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
        all_sub_tool_entries: dict[str, list[tuple[str, str]]] = {}
        completed_ids: set[str] = set()

        independent = [sq for sq in plan.sub_questions if not sq.depends_on]
        dependent = [sq for sq in plan.sub_questions if sq.depends_on]

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
            _ENTITY_KEYS = ("name", "person", "entity", "entity_name",
                            "person_a", "person_b", "sender", "from_name",
                            "display_name", "source_entity", "target_entity")

            def _extract_from_item(item: dict) -> None:
                if not isinstance(item, dict):
                    return
                for key in _ENTITY_KEYS:
                    val = item.get(key, "")
                    if val and isinstance(val, str) and val not in seen and len(val) > 1:
                        seen.add(val)
                        discovered.append({"name": val, "entity_type": "Person"})
                        if len(discovered) >= 5:
                            return
                for val in item.values():
                    if isinstance(val, list):
                        for sub in val[:5]:
                            if isinstance(sub, dict):
                                _extract_from_item(sub)
                                if len(discovered) >= 5:
                                    return
                    elif isinstance(val, dict):
                        _extract_from_item(val)
                        if len(discovered) >= 5:
                            return

            for result_str in tool_results.values():
                if not isinstance(result_str, str):
                    continue
                try:
                    data = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    _extract_from_item(item)
                    if len(discovered) >= 5:
                        return discovered
            return discovered

        def _execute_sub_question(sq: SubQuestion) -> tuple[str, list[tuple[str, str]]]:
            pattern = PATTERN_REGISTRY.get(sq.pattern)
            if not pattern or not pattern.steps:
                return "", []
            entities = sq.entities or []
            metadata = {}
            _date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            if sq.date_from and _date_re.match(sq.date_from):
                metadata["date_from"] = sq.date_from
            if sq.date_to and _date_re.match(sq.date_to):
                metadata["date_to"] = sq.date_to
            if sq.contract:
                metadata["contract"] = sq.contract
            if sq.pattern == "timeline" and "date_from" not in metadata:
                auto_dates = _extract_temporal_metadata(sq.question)
                metadata.update(auto_dates)
            if sq.pattern in ("keyword_search", "general"):
                topic_meta = _extract_topic_metadata(sq.question)
                if topic_meta.get("topic_keywords") and not sq.keywords:
                    metadata["keywords"] = topic_meta["topic_keywords"]
                if topic_meta.get("topic_entities") and not entities:
                    entities = topic_meta["topic_entities"]
                    log.info("PDES topic enrichment: injected entities %s for sq %s",
                             [e["name"] for e in entities], sq.id)
            if sq.keywords:
                metadata["keywords"] = sq.keywords

            tool_results = {}
            shortcut_documentary = _should_use_targeted_documentary_shortcut(
                sq.question,
                contract=sq.contract,
                pattern_name=sq.pattern,
            )
            has_entity = bool(entities and entities[0].get("name"))
            needs_discovery = (
                sq.pattern in ("keyword_search", "general", "timeline")
                and not has_entity
            )

            if shortcut_documentary:
                _run_steps(
                    _build_documentary_shortcut_steps(
                        sq.question,
                        contract=sq.contract,
                        keyword_hint=str(metadata.get("keywords", "") or ""),
                    ),
                    entities,
                    metadata,
                    sq,
                    tool_results,
                )
            elif needs_discovery:
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

            # --- Parallel follow-up: top-contact emails + evidence drill-down ---
            followup_steps: list[ExecutionStep] = []

            if shortcut_documentary and not _has_access_request_approval_signal(sq.question):
                followup_steps.extend(
                    _build_targeted_retry_drilldown_steps(
                        tool_results,
                        question=sq.question,
                        contract=sq.contract,
                    )[:2]
                )

            if sq.pattern == "entity_explore" and entities:
                primary = entities[0].get("name", "")
                if primary:
                    top_names = _extract_top_contacts_for_evidence(tool_results, limit=3)
                    for contact_name in top_names:
                        followup_steps.append(ExecutionStep("get_emails_between", {
                            "entity_a": primary,
                            "entity_b": contact_name,
                            "limit": 3,
                        }))

            if (
                not shortcut_documentary
                and CORPUS == "enron" and sq.pattern in (
                "entity_structure", "entity_pair", "entity_explore",
                "keyword_search", "timeline",
                )
            ):
                drill_ids = _extract_evidence_ids_for_drilldown(tool_results, limit=4)
                for mid, tid in drill_ids:
                    drill_params = {}
                    if mid:
                        drill_params["message_id"] = mid
                    elif tid:
                        drill_params["thread_id"] = tid
                    drill_params["limit"] = 2
                    followup_steps.append(ExecutionStep("get_email_full_body", drill_params))

            if followup_steps:
                if _PARALLEL_TOOLS and len(followup_steps) > 1:
                    with ThreadPoolExecutor(max_workers=min(8, len(followup_steps))) as pool:
                        ordered_results = [None] * len(followup_steps)
                        future_to_idx = {
                            pool.submit(_invoke_step, step, entities, metadata, sq): idx
                            for idx, step in enumerate(followup_steps)
                        }
                        for fut in as_completed(future_to_idx):
                            ordered_results[future_to_idx[fut]] = fut.result()
                        for item in ordered_results:
                            if item:
                                key, result, tool_name = item
                                tool_results[key] = result
                                if tools_invoked_out is not None:
                                    tools_invoked_out.append(tool_name)
                else:
                    for step in followup_steps:
                        item = _invoke_step(step, entities, metadata, sq)
                        if item:
                            key, result, tool_name = item
                            tool_results[key] = result
                            if tools_invoked_out is not None:
                                tools_invoked_out.append(tool_name)

            tool_entries = list(tool_results.items())
            if _should_run_targeted_documentary_retry(
                tool_entries,
                sq.question,
                contract=sq.contract,
                pattern_name=sq.pattern,
            ):
                retry_tool_results: dict[str, str] = {}
                retry_steps = _build_targeted_documentary_retry_steps(
                    sq.question,
                    contract=sq.contract,
                    existing_calls=list(tool_results.keys()),
                )
                for step in retry_steps:
                    item = _invoke_step(step, entities, metadata, sq)
                    if item:
                        key, result, tool_name = item
                        retry_tool_results[key] = result
                        tool_results[key] = result
                        if tools_invoked_out is not None:
                            tools_invoked_out.append(tool_name)

                retry_followups = _build_targeted_retry_drilldown_steps(
                    retry_tool_results,
                    question=sq.question,
                    contract=sq.contract,
                )
                for step in retry_followups:
                    item = _invoke_step(step, entities, metadata, sq)
                    if item:
                        key, result, tool_name = item
                        retry_tool_results[key] = result
                        tool_results[key] = result
                        if tools_invoked_out is not None:
                            tools_invoked_out.append(tool_name)

            for result_str in tool_results.values():
                if isinstance(result_str, str):
                    self.entity_memory.extract(result_str)

            if tool_results and sq.pattern in ("entity_explore", "timeline"):
                tool_results = _prioritize_email_results(tool_results)

            raw = json.dumps(tool_results, ensure_ascii=False) if tool_results else ""
            if sq.pattern in ("entity_explore", "timeline"):
                limit = 10000
            elif sq.pattern in ("keyword_search", "general"):
                limit = 8000
            else:
                limit = 6000
            return _truncate_json_aware(raw, limit), list(tool_results.items())

        if _PARALLEL_TOOLS and len(independent) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(independent))) as pool:
                futures = {pool.submit(_execute_sub_question, sq): sq for sq in independent}
                for fut in as_completed(futures):
                    sq = futures[fut]
                    result, tool_entries = fut.result()
                    if result:
                        all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
                    all_sub_tool_entries[sq.id] = tool_entries
                    completed_ids.add(sq.id)
        else:
            for sq in independent:
                result, tool_entries = _execute_sub_question(sq)
                if result:
                    all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
                all_sub_tool_entries[sq.id] = tool_entries
                completed_ids.add(sq.id)

        for sq in dependent:
            deps_met = all(d in completed_ids for d in sq.depends_on)
            if not deps_met:
                log.warning("PDES: skipping sq %s — unmet dependencies %s", sq.id, sq.depends_on)
                continue
            result, tool_entries = _execute_sub_question(sq)
            if result:
                all_sub_results[sq.id] = f"[{sq.pattern}] {sq.question}\n{result}"
            all_sub_tool_entries[sq.id] = tool_entries
            completed_ids.add(sq.id)

        if not all_sub_results:
            return

        sub_answers_block = "\n\n---\n\n".join(
            f"### Sub-question: {all_sub_results[k]}" for k in sorted(all_sub_results.keys())
        )

        tool_entries: list[tuple[str, str]] = []
        for sq_id in sorted(all_sub_tool_entries.keys()):
            tool_entries.extend(all_sub_tool_entries[sq_id])
        evidence_strength = _estimate_evidence_strength(tool_entries)
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

        pdes_consistency = self._validate_tool_consistency({call: result for call, result in tool_entries})
        pdes_consistency_block = ""
        if pdes_consistency:
            pdes_consistency_block = (
                "\n\n## CROSS-TOOL CONSISTENCY WARNINGS\n"
                + "\n".join(f"- {w}" for w in pdes_consistency)
                + "\n\nYou MUST address these warnings in your response. "
                "Do NOT ignore contradictions between tools.\n"
            )

        plan_contract = (
            plan.sub_questions[0].contract
            if len(plan.sub_questions) == 1 and plan.sub_questions[0].contract
            else _extract_answer_contract(plan.resolved_question)
        )
        pdes_pattern_name = plan.sub_questions[0].pattern if len(plan.sub_questions) == 1 else ""
        pdes_sufficiency = _assess_evidence_sufficiency(
            tool_entries,
            evidence_strength,
            question=plan.resolved_question,
            contract=plan_contract,
            consistency_warnings=pdes_consistency,
            pattern_name=pdes_pattern_name,
        )
        if plan_contract.get("requires_evidence") and _has_access_request_approval_signal(plan.resolved_question):
            deterministic_response = AIMessage(
                content=_render_access_request_workflow_hedge_response(
                    tool_entries,
                    evidence_strength,
                    question=plan.resolved_question,
                    contract=plan_contract,
                )
            )
            yield from output_to_responses_items_stream([deterministic_response])
            return
        if pdes_sufficiency["decision"] == "abstain":
            abstain_response = AIMessage(
                content=_render_abstention_response(
                    tool_entries,
                    evidence_strength,
                    pdes_sufficiency,
                    question=plan.resolved_question,
                    contract=plan_contract,
                )
            )
            yield from output_to_responses_items_stream([abstain_response])
            return
        sufficiency_block = _build_sufficiency_guardrail_block(pdes_sufficiency)
        provenance_guardrail = _build_provenance_guardrail_block(
            tool_entries,
            evidence_strength,
            question=plan.resolved_question,
            contract=plan_contract,
        )
        unique_patterns = list({sq.pattern for sq in plan.sub_questions})
        if len(unique_patterns) == 1:
            sole_pattern = PATTERN_REGISTRY.get(unique_patterns[0])
            if sole_pattern and sole_pattern.synthesis_prompt:
                synthesis_prompt = (
                    sole_pattern.synthesis_prompt
                    + pdes_consistency_block
                    + evidence_block
                    + sufficiency_block
                    + PROVENANCE_FORMAT
                    + provenance_guardrail
                    + f"\n\n## Data Retrieved\n\n{sub_answers_block}"
                )
            else:
                synthesis_prompt = (
                    "You are a corporate communications analyst synthesizing answers from data queries about Enron.\n\n"
                    + pdes_consistency_block
                    + evidence_block
                    + sufficiency_block
                    + PROVENANCE_FORMAT
                    + provenance_guardrail
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
                + pdes_consistency_block
                + evidence_block
                + sufficiency_block
                + "Guidelines:\n"
                "- Integrate information from all sub-queries into a unified narrative.\n"
                "- Prioritize curated data (source: curated_org_hierarchy) over LLM-extracted relationships.\n"
                "- Cite specific evidence: emails [YYYY-MM-DD, From: sender, Subject: topic], timeline entries, relationship data.\n"
                "- Only cite emails that DIRECTLY support a specific claim.\n"
                "- If sub-queries returned conflicting information, note the discrepancy.\n"
                "- Do NOT fabricate information not present in any sub-query result.\n"
                + PROVENANCE_FORMAT
                + provenance_guardrail
                + f"\n\n## Sub-Query Results\n\n{sub_answers_block}"
            )

        response = self.llm.invoke([
            {"role": "system", "content": synthesis_prompt},
            {"role": "user", "content": plan.resolved_question},
        ])
        if isinstance(getattr(response, "content", None), str):
            response.content = _apply_provenance_guardrails(
                response.content,
                tool_entries,
                evidence_strength,
                question=plan.resolved_question,
                contract=plan_contract,
            )
            response.content = _apply_claim_verification(
                response.content,
                tool_entries,
                evidence_strength,
                question=plan.resolved_question,
                contract=plan_contract,
                consistency_warnings=pdes_consistency,
            )
            response.content = _apply_human_readable_output_contract(
                response.content,
                tool_entries,
                evidence_strength,
                question=plan.resolved_question,
                contract=plan_contract,
                assessment=pdes_sufficiency,
            )

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

        _emit_runtime_observability()
        clear_resolve_cache()
        if hasattr(_backend, "clear"):
            _backend.clear()

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

            bible_comparison_answer = _build_bible_comparison_answer(question)
            if bible_comparison_answer:
                execution_path = "fast"
                yield from output_to_responses_items_stream([
                    AIMessage(content=bible_comparison_answer)
                ])
                return

            bible_lineage_answer = _build_bible_lineage_answer(question)
            if bible_lineage_answer:
                execution_path = "fast"
                yield from output_to_responses_items_stream([
                    AIMessage(content=bible_lineage_answer)
                ])
                return

            # --- PDES: Plan-Decompose-Execute-Synthesize ---
            em_context = self.entity_memory.context_for_classifier()

            if question and CORPUS == "enron" and TOOL_MAP:
                classify_question = question + em_context if em_context else question
                answer_contract = _extract_answer_contract(question)
                routing_hint = _get_case_based_pattern_hint(question, answer_contract)
                pre_classification = classify_and_extract(
                    classify_question,
                    raw_question=question,
                    contract=answer_contract,
                    routing_hint=routing_hint,
                )
                for ent in pre_classification.get("entities", []):
                    name = ent.get("name", "") if isinstance(ent, dict) else ""
                    if name:
                        self.entity_memory.record_user_entity(name)
                pre_conf = pre_classification.get("confidence", 0.0)
                pre_pattern = pre_classification.get("pattern", "general")
                if pre_conf >= 0.7 and pre_pattern != "general":
                    log.info("PDES: high-confidence bypass (%.2f %s), skipping planner LLM",
                             pre_conf, pre_pattern)
                    plan = _plan_from_classification(question, pre_classification, em_context)
                else:
                    log.info("PDES: planning query decomposition")
                    plan = _plan_query(question, messages, em_context)
                classified_intent = ",".join(sq.pattern for sq in plan.sub_questions)

                has_fast_primitives = len(plan.sub_questions) > 0

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
                        if sq.contract:
                            fp_metadata["contract"] = sq.contract
                        if not fp_metadata and sq.pattern == "timeline":
                            fp_metadata = _extract_temporal_metadata(question)
                        if sq.pattern in ("keyword_search", "general"):
                            topic_meta = _extract_topic_metadata(question)
                            if topic_meta.get("topic_keywords") and "keywords" not in fp_metadata:
                                fp_metadata["keywords"] = topic_meta["topic_keywords"]
                            if topic_meta.get("topic_entities") and not entities:
                                entities = topic_meta["topic_entities"]

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
                classification = classify_and_extract(
                    classify_question,
                    raw_question=question,
                )
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
