"""Trigger and monitor incremental ingestion / removal pipelines via Databricks Jobs API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import NotebookTask, SubmitTask, Source

_client: WorkspaceClient | None = None
_client_created: float = 0
_CLIENT_TTL = 1800


def _get_client() -> WorkspaceClient:
    global _client, _client_created
    now = time.time()
    if _client is None or (now - _client_created) > _CLIENT_TTL:
        _client = WorkspaceClient()
        _client_created = now
    return _client


_WORKSPACE_PATH = os.getenv(
    "GRAPHRAG_NOTEBOOK_PATH",
    "/Workspace/Users/dastan.aitzhanov@databricks.com/GraphRAG/notebooks",
)
_CLUSTER_ID = os.getenv("GRAPHRAG_CLUSTER_ID", "")


@dataclass
class PipelineRun:
    run_id: int
    action: str  # "add" or "remove"
    books: list[str]
    status: str  # PENDING, RUNNING, TERMINATED, SKIPPED, INTERNAL_ERROR
    result: str  # SUCCESS, FAILED, CANCELED, ...
    start_time: float
    elapsed_seconds: float = 0.0
    output: dict | None = None


def _build_run_config(notebook_name: str, params: dict[str, str]) -> dict[str, Any]:
    """Build the submit run configuration, preferring serverless when no cluster is specified."""
    task = SubmitTask(
        task_key="incremental_pipeline",
        notebook_task=NotebookTask(
            notebook_path=f"{_WORKSPACE_PATH}/{notebook_name}",
            base_parameters=params,
            source=Source.WORKSPACE,
        ),
    )

    if _CLUSTER_ID:
        task.existing_cluster_id = _CLUSTER_ID
    else:
        task.environment_key = "default"

    return task


def submit_add_books(books: list[str]) -> int:
    """Submit a notebook run to ingest the given books. Returns the run_id."""
    w = _get_client()
    task = _build_run_config(
        "04_Incremental_Ingest",
        {"books_to_add": json.dumps(books)},
    )

    environments = None
    if not _CLUSTER_ID:
        from databricks.sdk.service.jobs import JobEnvironment
        environments = [
            JobEnvironment.from_dict({
                "environment_key": "default",
                "spec": {
                    "client": "2",
                    "dependencies": [
                        "mlflow>=3.0",
                        "networkx",
                        "requests",
                    ],
                },
            })
        ]

    response = w.jobs.submit(
        run_name=f"GraphRAG — Add Books: {', '.join(books[:3])}{'...' if len(books) > 3 else ''}",
        tasks=[task],
        environments=environments,
    )
    return response.run_id


def submit_remove_books(books: list[str]) -> int:
    """Submit a notebook run to remove the given books. Returns the run_id."""
    w = _get_client()
    task = _build_run_config(
        "05_Remove_Books",
        {"books_to_remove": json.dumps(books)},
    )

    environments = None
    if not _CLUSTER_ID:
        from databricks.sdk.service.jobs import JobEnvironment
        environments = [
            JobEnvironment.from_dict({
                "environment_key": "default",
                "spec": {
                    "client": "2",
                    "dependencies": [
                        "mlflow>=3.0",
                        "networkx",
                    ],
                },
            })
        ]

    response = w.jobs.submit(
        run_name=f"GraphRAG — Remove Books: {', '.join(books[:3])}{'...' if len(books) > 3 else ''}",
        tasks=[task],
        environments=environments,
    )
    return response.run_id


def get_run_status(run_id: int) -> PipelineRun:
    """Poll the status of a submitted run."""
    w = _get_client()
    run = w.jobs.get_run(run_id)

    state = run.state
    status = state.life_cycle_state.value if state.life_cycle_state else "UNKNOWN"
    result = state.result_state.value if state.result_state else ""

    elapsed = 0.0
    if run.start_time:
        elapsed = (time.time() * 1000 - run.start_time) / 1000

    output = None
    if status == "TERMINATED" and result == "SUCCESS":
        try:
            run_output = w.jobs.get_run_output(run_id)
            if run_output.notebook_output and run_output.notebook_output.result:
                output = json.loads(run_output.notebook_output.result)
        except Exception:
            pass

    run_name = run.run_name or ""
    action = "add" if "Add" in run_name else "remove"
    books_str = run_name.split(":")[-1].strip() if ":" in run_name else ""
    books = [b.strip().rstrip(".") for b in books_str.split(",") if b.strip()]

    return PipelineRun(
        run_id=run_id,
        action=action,
        books=books,
        status=status,
        result=result,
        start_time=run.start_time / 1000 if run.start_time else time.time(),
        elapsed_seconds=elapsed,
        output=output,
    )
