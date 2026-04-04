from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ENRON_ENDPOINT_NAME = os.environ.get(
    "GRAPHRAG_ENRON_ENDPOINT_NAME",
    "graphrag-enron-agent",
)
ENRON_CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
ENRON_SCHEMA = os.environ.get("GRAPHRAG_ENRON_SCHEMA", "graphrag_enron")
ENRON_REGISTERED_MODEL = f"{ENRON_CATALOG}.{ENRON_SCHEMA}.graphrag_enron_agent"

DEFAULT_LLM_ENDPOINT = os.environ.get(
    "GRAPHRAG_LLM_ENDPOINT",
    "databricks-meta-llama-3-3-70b-instruct",
)
DEFAULT_SMALL_LLM_ENDPOINT = os.environ.get(
    "GRAPHRAG_SMALL_LLM_ENDPOINT",
    "databricks-meta-llama-3-1-8b-instruct",
)
DEFAULT_SYNTHESIS_ENDPOINT = os.environ.get(
    "GRAPHRAG_SYNTHESIS_ENDPOINT",
    DEFAULT_LLM_ENDPOINT,
)
DEFAULT_REACT_ENDPOINT = os.environ.get(
    "GRAPHRAG_REACT_ENDPOINT",
    DEFAULT_LLM_ENDPOINT,
)
DEFAULT_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "399215661843ad19")
DEFAULT_LAKEBASE_PROJECT = os.environ.get("GRAPHRAG_LAKEBASE_PROJECT", "graphrag")

GENIE_SPACE_IDS = {
    "GENIE_COMM_SPACE_ID": "01f12b3ef5121d88be4f23d2dfe2d770",
    "GENIE_ORG_SPACE_ID": "01f12b3ef5521f078ba8438cc94e108b",
    "GENIE_INVEST_SPACE_ID": "01f12b3ef56e198e828cd8b59f646430",
}

ENRON_TABLE_NAMES = [
    "entities",
    "relationships",
    "emails",
    "entity_analytics",
    "entity_paths",
    "entity_mentions",
    "communication_dyads",
    "participants",
    "entity_aliases",
    "person_activity",
    "investigation_timeline",
    "extraction_provenance",
    "pipeline_lineage",
    "topic_taxonomy",
    "corpus_coverage",
    "person_role_timeline",
    "person_identity",
    "email_classification",
    "data_quality_report",
    "threads",
    "org_hierarchy",
    "org_hierarchy_evidence",
]

ENRON_REQUIRED_LAKEBASE_TABLES = (
    "enron.entities",
    "enron.relationships",
    "enron.emails",
    "enron.org_hierarchy",
    "enron.org_hierarchy_evidence",
)

ENRON_PIP_REQUIREMENTS = [
    "mlflow>=3.0",
    "databricks-langchain",
    "langgraph>=0.3.4",
    "databricks-agents",
    "databricks-mcp",
    "databricks-sdk",
    "psycopg[binary,pool]>=3.0",
]

ENRON_INPUT_EXAMPLE = {
    "input": [
        {
            "role": "user",
            "content": "Who communicated most frequently with Kenneth Lay?",
        }
    ]
}

DEFAULT_PROMOTION_MANIFEST = "enron_promotion_manifest.json"


def _explicit_lakebase_username() -> str:
    return (
        os.environ.get("GRAPHRAG_LAKEBASE_USERNAME")
        or os.environ.get("LAKEBASE_USERNAME")
        or os.environ.get("LAKEBASE_USER")
        or ""
    ).strip()


def _default_serving_lakebase_username() -> str:
    override = _explicit_lakebase_username()
    if override:
        return override

    try:
        from databricks.sdk import WorkspaceClient

        username = str(WorkspaceClient().current_user.me().user_name or "").strip()
    except Exception:
        return ""
    return username


def resolve_lakebase_username(
    token: str,
    *,
    workspace_user_name: str | None = None,
) -> str:
    """Resolve the Postgres username for a generated Lakebase credential.

    Databricks-hosted runtimes can surface `current_user.me().user_name` as an
    internal principal id. The generated database credential is authoritative,
    so prefer explicit overrides or the JWT subject claim when available.
    """
    override = _explicit_lakebase_username()
    if override:
        return override

    payload: dict[str, Any] = {}
    try:
        parts = str(token or "").split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        payload = {}

    for key in ("sub", "preferred_username", "email", "upn"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    if workspace_user_name:
        return workspace_user_name.strip()
    return ""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def repo_root_from(anchor: str | Path | None = None) -> Path:
    if anchor is None:
        return Path(__file__).resolve().parents[2]
    anchor_path = Path(anchor).resolve()
    if anchor_path.is_file():
        return anchor_path.parents[2]
    return anchor_path.resolve()


def get_default_lakebase_endpoint(project_id: str = DEFAULT_LAKEBASE_PROJECT) -> str:
    return f"projects/{project_id}/branches/production/endpoints/primary"


def build_enron_code_paths(repo_root: str | Path) -> list[str]:
    root = Path(repo_root).resolve()
    return [
        str((root / "src").resolve()),
    ]


def build_enron_serving_environment(
    *,
    schema: str = ENRON_SCHEMA,
    llm_endpoint: str = DEFAULT_LLM_ENDPOINT,
    small_llm_endpoint: str = DEFAULT_SMALL_LLM_ENDPOINT,
    synthesis_endpoint: str = DEFAULT_SYNTHESIS_ENDPOINT,
    react_endpoint: str = DEFAULT_REACT_ENDPOINT,
    backend: str = "databricks",
    llm_provider: str = "databricks",
    lakebase_endpoint: str | None = None,
    lakebase_host: str | None = None,
    lakebase_dbname: str | None = None,
    model_logging_tool_limit: int | None = 32,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    graph_transport = "local" if backend == "local" else "mcp"
    evidence_transport = "local" if backend == "local" else "mcp"
    analytics_transport = "local" if backend == "local" else "mcp"
    env = {
        "GRAPHRAG_CORPUS": "enron",
        "GRAPHRAG_SCHEMA": schema,
        "GRAPHRAG_ENRON_SCHEMA": schema,
        "GRAPHRAG_BACKEND": backend,
        "GRAPHRAG_DATA_BACKEND": backend,
        "GRAPHRAG_LLM_PROVIDER": llm_provider,
        "GRAPHRAG_LLM_ENDPOINT": llm_endpoint,
        "GRAPHRAG_SYNTHESIS_ENDPOINT": synthesis_endpoint,
        "GRAPHRAG_REACT_ENDPOINT": react_endpoint,
        "GRAPHRAG_SMALL_LLM_ENDPOINT": small_llm_endpoint,
        "GRAPHRAG_RUNTIME_TRANSPORT": "direct",
        "GRAPHRAG_ROUTER_TRANSPORT": "local",
        "GRAPHRAG_PLANNER_TRANSPORT": "local",
        "GRAPHRAG_GRAPH_TRANSPORT": graph_transport,
        "GRAPHRAG_EVIDENCE_TRANSPORT": evidence_transport,
        "GRAPHRAG_ANALYTICS_TRANSPORT": analytics_transport,
        "GRAPHRAG_ANALYTICS_BACKEND": "databricks_sql",
    }
    if model_logging_tool_limit is not None:
        env["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = str(model_logging_tool_limit)
    env.update(GENIE_SPACE_IDS)

    resolved_lakebase_endpoint = lakebase_endpoint or os.environ.get("LAKEBASE_ENDPOINT")
    if not resolved_lakebase_endpoint and backend == "lakebase":
        resolved_lakebase_endpoint = get_default_lakebase_endpoint()
    if resolved_lakebase_endpoint:
        env["LAKEBASE_ENDPOINT"] = resolved_lakebase_endpoint

    resolved_lakebase_host = lakebase_host or os.environ.get("LAKEBASE_HOST")
    if resolved_lakebase_host:
        env["LAKEBASE_HOST"] = resolved_lakebase_host

    resolved_lakebase_dbname = lakebase_dbname or os.environ.get("LAKEBASE_DBNAME")
    if resolved_lakebase_dbname:
        env["LAKEBASE_DBNAME"] = resolved_lakebase_dbname

    resolved_lakebase_username = ""
    if backend == "lakebase":
        # Serving runtimes can decode Lakebase credential subjects as UUID principals.
        # Carry a known username into the environment so connections stay stable.
        resolved_lakebase_username = _default_serving_lakebase_username()
    if resolved_lakebase_username:
        env["GRAPHRAG_LAKEBASE_USERNAME"] = resolved_lakebase_username

    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    return env


@contextlib.contextmanager
def enron_model_logging_env(
    *,
    schema: str = ENRON_SCHEMA,
    llm_endpoint: str = DEFAULT_LLM_ENDPOINT,
    small_llm_endpoint: str = DEFAULT_SMALL_LLM_ENDPOINT,
    synthesis_endpoint: str = DEFAULT_SYNTHESIS_ENDPOINT,
    react_endpoint: str = DEFAULT_REACT_ENDPOINT,
    backend: str = "databricks",
    llm_provider: str = "databricks",
    lakebase_endpoint: str | None = None,
    lakebase_host: str | None = None,
    lakebase_dbname: str | None = None,
    model_logging_tool_limit: int = 32,
) -> dict[str, str]:
    updates = build_enron_serving_environment(
        schema=schema,
        llm_endpoint=llm_endpoint,
        small_llm_endpoint=small_llm_endpoint,
        synthesis_endpoint=synthesis_endpoint,
        react_endpoint=react_endpoint,
        backend=backend,
        llm_provider=llm_provider,
        lakebase_endpoint=lakebase_endpoint,
        lakebase_host=lakebase_host,
        lakebase_dbname=lakebase_dbname,
        extra_env={
            "GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT": str(model_logging_tool_limit),
        },
    )
    prior = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield updates
    finally:
        for key, old_value in prior.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def build_enron_log_model_kwargs(
    repo_root: str | Path,
    *,
    catalog: str = ENRON_CATALOG,
    schema: str = ENRON_SCHEMA,
    llm_endpoint: str = DEFAULT_LLM_ENDPOINT,
    small_llm_endpoint: str = DEFAULT_SMALL_LLM_ENDPOINT,
    warehouse_id: str = DEFAULT_WAREHOUSE_ID,
) -> dict[str, Any]:
    from mlflow.models.resources import (
        DatabricksGenieSpace,
        DatabricksServingEndpoint,
        DatabricksSQLWarehouse,
        DatabricksTable,
    )

    root = Path(repo_root).resolve()
    python_model_path = root / "src" / "agent" / "agent_serving.py"
    resources = [
        DatabricksServingEndpoint(endpoint_name=llm_endpoint),
        DatabricksServingEndpoint(endpoint_name=small_llm_endpoint),
        *[
            DatabricksTable(table_name=f"{catalog}.{schema}.{table_name}")
            for table_name in ENRON_TABLE_NAMES
        ],
        DatabricksSQLWarehouse(warehouse_id=warehouse_id),
        *[
            DatabricksGenieSpace(genie_space_id=space_id)
            for space_id in GENIE_SPACE_IDS.values()
        ],
    ]
    return {
        "name": "agent",
        "python_model": str(python_model_path.resolve()),
        "code_paths": build_enron_code_paths(root),
        "resources": resources,
        "pip_requirements": list(ENRON_PIP_REQUIREMENTS),
        "input_example": dict(ENRON_INPUT_EXAMPLE),
        "registered_model_name": f"{catalog}.{schema}.graphrag_enron_agent",
    }


def assert_enron_lakebase_ready(
    *,
    endpoint_name: str | None = None,
    dbname: str | None = None,
    check_connectivity: bool = True,
    required_tables: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    resolved_endpoint = endpoint_name or os.environ.get("LAKEBASE_ENDPOINT") or get_default_lakebase_endpoint()
    resolved_dbname = dbname or os.environ.get("LAKEBASE_DBNAME", "databricks_postgres")

    w = WorkspaceClient()
    endpoint = w.postgres.get_endpoint(name=resolved_endpoint)
    host = getattr(getattr(endpoint.status, "hosts", None), "host", None)
    if not host:
        raise RuntimeError(
            f"Lakebase endpoint '{resolved_endpoint}' is missing a reachable host."
        )

    summary = {
        "endpoint_name": resolved_endpoint,
        "host": host,
        "dbname": resolved_dbname,
        "connected": False,
        "required_tables": list(required_tables or ENRON_REQUIRED_LAKEBASE_TABLES),
        "table_status": [],
    }
    if not check_connectivity:
        return summary

    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "psycopg is required for Lakebase connectivity validation."
        ) from exc

    cred = w.postgres.generate_database_credential(endpoint=resolved_endpoint)
    username = resolve_lakebase_username(
        cred.token,
        workspace_user_name=w.current_user.me().user_name,
    )
    if not username:
        raise RuntimeError("Lakebase credential did not yield a usable username.")
    with psycopg.connect(
        host=host,
        dbname=resolved_dbname,
        user=username,
        password=cred.token,
        sslmode="require",
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            required = tuple(required_tables or ENRON_REQUIRED_LAKEBASE_TABLES)
            table_status: list[dict[str, Any]] = []
            for full_name in required:
                if "." in full_name:
                    schema_name, table_name = full_name.split(".", 1)
                else:
                    schema_name, table_name = "public", full_name
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                    """,
                    (schema_name, table_name),
                )
                exists = bool(cur.fetchone()[0])
                entry = {"table": full_name, "exists": exists, "readable": False}
                if exists:
                    cur.execute(
                        sql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(
                            sql.Identifier(schema_name),
                            sql.Identifier(table_name),
                        )
                    )
                    cur.fetchone()
                    entry["readable"] = True
                table_status.append(entry)
            summary["table_status"] = table_status

    missing = [row["table"] for row in summary["table_status"] if not row["exists"]]
    unreadable = [row["table"] for row in summary["table_status"] if row["exists"] and not row["readable"]]
    if missing:
        raise RuntimeError(
            "Lakebase is reachable but missing required Enron tables: "
            + ", ".join(missing)
        )
    if unreadable:
        raise RuntimeError(
            "Lakebase is reachable but required Enron tables are unreadable: "
            + ", ".join(unreadable)
        )
    summary["connected"] = True
    return summary


def get_live_endpoint_state(endpoint_name: str = ENRON_ENDPOINT_NAME) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    endpoint = w.serving_endpoints.get(name=endpoint_name)
    ready = endpoint.state.ready if endpoint.state else None
    config_update = endpoint.state.config_update if endpoint.state else None
    served_entities = []
    if endpoint.config and endpoint.config.served_entities:
        for served_entity in endpoint.config.served_entities:
            served_entities.append(
                {
                    "entity_name": getattr(served_entity, "entity_name", ""),
                    "entity_version": str(getattr(served_entity, "entity_version", "")),
                }
            )
    return {
        "endpoint_name": endpoint_name,
        "ready": str(ready) if ready is not None else None,
        "config_update": str(config_update) if config_update is not None else None,
        "served_entities": served_entities,
    }


def capture_git_snapshot(
    repo_root: str | Path,
    tracked_paths: list[str] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    tracked_paths = tracked_paths or []

    def _hash_path(path: Path) -> str:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()

        digest = hashlib.sha256()
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(str(child.relative_to(path)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    commit = _git("rev-parse", "HEAD") or None
    status_text = _git("status", "--short")
    changed_files = []
    for line in status_text.splitlines():
        stripped = line.strip()
        if stripped:
            changed_files.append(stripped.split(maxsplit=1)[-1])

    file_hashes: dict[str, str] = {}
    for raw_path in tracked_paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = (root / path).resolve()
        if not path.exists():
            continue
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = str(path)
        file_hashes[relative] = _hash_path(path)

    return {
        "git_commit": commit,
        "working_tree_dirty": bool(changed_files),
        "changed_files": changed_files,
        "file_hashes": file_hashes,
    }


def build_default_gate_thresholds(
    quality_payload: dict[str, Any] | None,
    latency_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    quality_payload = quality_payload or {}
    latency_payload = latency_payload or {}
    quality_metrics = quality_payload.get("overall_metrics", quality_payload)
    latency_metrics = latency_payload.get("runtime", latency_payload)
    overall_score = _safe_float(
        quality_metrics.get("benchmark_score")
    )
    mean_ms = _safe_float(latency_metrics.get("mean_ms"))
    p95_ms = _safe_float(latency_metrics.get("p95_ms"))
    slice_question_count = int(
        quality_payload.get("slice_question_count")
        or latency_payload.get("slice_question_count")
        or 5
    )

    thresholds: dict[str, Any] = {
        "split": "test",
        "cases": max(3, min(8, slice_question_count)),
        "max_error_count": 0,
    }
    if overall_score is not None:
        thresholds["min_overall_score"] = round(max(0.0, overall_score - 0.03), 4)
    else:
        thresholds["min_overall_score"] = 0.58
    if mean_ms is not None:
        thresholds["max_mean_latency_ms"] = round(mean_ms * 1.35 + 750.0, 1)
    if p95_ms is not None:
        thresholds["max_p95_latency_ms"] = round(p95_ms * 1.35 + 1000.0, 1)
    return thresholds


def build_promotion_manifest(
    *,
    artifact_dir: str | Path = "data",
    output_path: str | Path | None = None,
    candidate_label: str = "postchange",
    repo_root: str | Path | None = None,
    catalog: str = ENRON_CATALOG,
    schema: str = ENRON_SCHEMA,
    llm_endpoint: str = DEFAULT_LLM_ENDPOINT,
    small_llm_endpoint: str = DEFAULT_SMALL_LLM_ENDPOINT,
    synthesis_endpoint: str = DEFAULT_SYNTHESIS_ENDPOINT,
    react_endpoint: str = DEFAULT_REACT_ENDPOINT,
    serving_backend: str = "databricks",
    lakebase_endpoint: str | None = None,
    warehouse_id: str = DEFAULT_WAREHOUSE_ID,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir).resolve()
    quality_path = artifact_root / f"factual_{candidate_label}_quality.json"
    latency_path = artifact_root / f"factual_{candidate_label}_latency.json"
    assessment_path = artifact_root / "assessment.json"
    loop_state_path = artifact_root / "loop_state.json"
    benchmark_path = artifact_root / "factual_benchmark_definition.json"

    quality_payload = _read_json(quality_path)
    latency_payload = _read_json(latency_path)
    assessment_payload = _read_json(assessment_path) if assessment_path.exists() else {}
    loop_state = _read_json(loop_state_path) if loop_state_path.exists() else {}
    benchmark_payload = _read_json(benchmark_path) if benchmark_path.exists() else {}

    resolved_repo_root = Path(repo_root or repo_root_from()).resolve()
    python_model_path = resolved_repo_root / "src" / "agent" / "agent_serving.py"
    code_paths = build_enron_code_paths(resolved_repo_root)
    serving_env = build_enron_serving_environment(
        schema=schema,
        llm_endpoint=llm_endpoint,
        small_llm_endpoint=small_llm_endpoint,
        synthesis_endpoint=synthesis_endpoint,
        react_endpoint=react_endpoint,
        backend=serving_backend,
        lakebase_endpoint=lakebase_endpoint,
    )
    gate_thresholds = build_default_gate_thresholds(quality_payload, latency_payload)

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "candidate_label": candidate_label,
        "corpus": "enron",
        "deploy_target": {
            "endpoint_name": ENRON_ENDPOINT_NAME,
            "registered_model_name": f"{catalog}.{schema}.graphrag_enron_agent",
            "serving_backend": serving_backend,
            "lakebase_endpoint": serving_env.get("LAKEBASE_ENDPOINT"),
            "warehouse_id": warehouse_id,
            "llm_provider": serving_env["GRAPHRAG_LLM_PROVIDER"],
            "llm_endpoint": serving_env["GRAPHRAG_LLM_ENDPOINT"],
            "synthesis_endpoint": serving_env["GRAPHRAG_SYNTHESIS_ENDPOINT"],
            "react_endpoint": serving_env["GRAPHRAG_REACT_ENDPOINT"],
            "small_llm_endpoint": serving_env["GRAPHRAG_SMALL_LLM_ENDPOINT"],
        },
        "local_candidate": {
            "quality_artifact": str(quality_path),
            "latency_artifact": str(latency_path),
            "assessment_artifact": str(assessment_path) if assessment_path.exists() else None,
            "loop_state_artifact": str(loop_state_path) if loop_state_path.exists() else None,
            "benchmark_artifact": str(benchmark_path) if benchmark_path.exists() else None,
            "assessment_verdict": assessment_payload.get("verdict"),
            "primary_metric": assessment_payload.get("primary_metric", "benchmark_score"),
            "primary_metric_delta": assessment_payload.get("primary_metric_delta"),
            "quality_summary": quality_payload.get("overall_metrics", {}),
            "latency_summary": latency_payload.get("runtime", {}),
            "slice_question_count": quality_payload.get("slice_question_count"),
            "benchmark_question_count": len(benchmark_payload.get("questions", [])),
            "iteration": assessment_payload.get("iteration") or loop_state.get("iteration"),
        },
        "promotion_contract": {
            "python_model": str(python_model_path.resolve()),
            "code_paths": code_paths,
            "pip_requirements": list(ENRON_PIP_REQUIREMENTS),
            "environment_vars": serving_env,
            "gate_thresholds": gate_thresholds,
            "lakebase_readiness_required": serving_backend == "lakebase",
        },
        "code_snapshot": capture_git_snapshot(
            resolved_repo_root,
            tracked_paths=[str(python_model_path.resolve()), *code_paths],
        ),
    }

    manifest_path = (
        Path(output_path).resolve()
        if output_path is not None
        else (artifact_root / DEFAULT_PROMOTION_MANIFEST).resolve()
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2))
    payload["manifest_path"] = str(manifest_path)
    return payload


def load_promotion_manifest(path: str | Path) -> dict[str, Any]:
    return _read_json(path)


def evaluate_deployed_gate(
    manifest: dict[str, Any],
    quality_payload: dict[str, Any],
    latency_payload: dict[str, Any],
) -> dict[str, Any]:
    thresholds = (
        manifest.get("promotion_contract", {}).get("gate_thresholds", {})
        or manifest.get("gate_thresholds", {})
    )
    quality_overall = _safe_float(
        quality_payload.get("overall_score")
        or quality_payload.get("overall_metrics", {}).get("benchmark_score")
    )
    quality_error_count = int(quality_payload.get("error_question_count") or 0)
    latency_error_count = int(latency_payload.get("error_question_count") or 0)
    mean_ms = _safe_float(latency_payload.get("runtime", {}).get("mean_ms"))
    p95_ms = _safe_float(latency_payload.get("runtime", {}).get("p95_ms"))

    checks: list[dict[str, Any]] = []

    def _append_check(name: str, actual: Any, expected: Any, passed: bool) -> None:
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "passed": bool(passed),
            }
        )

    if thresholds.get("min_overall_score") is not None:
        floor = float(thresholds["min_overall_score"])
        _append_check(
            "quality_floor",
            quality_overall,
            f">={floor}",
            quality_overall is not None and quality_overall >= floor,
        )

    max_error_count = int(thresholds.get("max_error_count", 0))
    _append_check(
        "quality_errors",
        quality_error_count,
        f"<={max_error_count}",
        quality_error_count <= max_error_count,
    )
    _append_check(
        "latency_errors",
        latency_error_count,
        f"<={max_error_count}",
        latency_error_count <= max_error_count,
    )

    if thresholds.get("max_mean_latency_ms") is not None:
        limit = float(thresholds["max_mean_latency_ms"])
        _append_check(
            "mean_latency",
            mean_ms,
            f"<={limit}",
            mean_ms is not None and mean_ms <= limit,
        )

    if thresholds.get("max_p95_latency_ms") is not None:
        limit = float(thresholds["max_p95_latency_ms"])
        _append_check(
            "p95_latency",
            p95_ms,
            f"<={limit}",
            p95_ms is not None and p95_ms <= limit,
        )

    passed = all(check["passed"] for check in checks)
    return {
        "version": "1.0",
        "checked_at": _utc_now(),
        "passed": passed,
        "thresholds": thresholds,
        "checks": checks,
        "local_candidate": manifest.get("local_candidate", {}),
        "deployed_quality": {
            "overall_score": quality_overall,
            "error_question_count": quality_error_count,
            "slice_question_count": quality_payload.get("slice_question_count"),
            "overall_metrics": quality_payload.get("overall_metrics", {}),
        },
        "deployed_latency": {
            "error_question_count": latency_error_count,
            "slice_question_count": latency_payload.get("slice_question_count"),
            "runtime": latency_payload.get("runtime", {}),
        },
    }
