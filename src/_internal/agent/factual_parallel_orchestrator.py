"""Parallel factual hardening orchestration helpers.

Implements the first working slice of the "GraphRAG Factual QA Hardening —
Parallel Subagent Orchestration" spec:

- benchmark curation into ``data/factual_benchmark_definition.json``
- MLflow-backed quality evaluation with concurrent question execution
- latency evaluation over the same benchmark slice
- measure-phase orchestration that manages ``data/loop_state.json``
- implement-stage orchestration via a shared ``data/implementation_log.json``
- loop orchestration with archive, gating, and final report emission

The implementation deliberately isolates answer generation in subprocesses.
`src/agent/_agent_core.py` clears module-global caches/backend counters at the
start of every request, so thread-level concurrency would cause cross-talk.

Usage:
    python -m src.agent.factual_parallel_orchestrator curate-benchmark
    python -m src.agent.factual_parallel_orchestrator evaluate-quality
    python -m src.agent.factual_parallel_orchestrator evaluate-latency
    python -m src.agent.factual_parallel_orchestrator analyze-failures
    python -m src.agent.factual_parallel_orchestrator investigate-root-causes
    python -m src.agent.factual_parallel_orchestrator plan-improvements
    python -m src.agent.factual_parallel_orchestrator assess-iteration
    python -m src.agent.factual_parallel_orchestrator orchestrate-measure
    python -m src.agent.factual_parallel_orchestrator orchestrate-analyze
    python -m src.agent.factual_parallel_orchestrator orchestrate-implement
    python -m src.agent.factual_parallel_orchestrator orchestrate-assess
    python -m src.agent.factual_parallel_orchestrator orchestrate-iteration
    python -m src.agent.factual_parallel_orchestrator emit-final-report
    python -m src.agent.factual_parallel_orchestrator orchestrate-loop
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from pathlib import Path
from threading import Lock
from typing import Any

os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

from src.agent.enron_promotion import build_promotion_manifest
from src.evaluation.enron_evaluation import (
    DATA_CONTEXT,
    answer_completeness,
    factual_accuracy,
    grounding_integrity,
    hallucination_detection,
)
from src.evaluation.question_bank import export_governed_flat_questions


DEFAULT_ARTIFACT_DIR = Path("data")
DEFAULT_BENCHMARK_PATH = DEFAULT_ARTIFACT_DIR / "factual_benchmark_definition.json"
DEFAULT_LOOP_STATE_PATH = DEFAULT_ARTIFACT_DIR / "loop_state.json"
DEFAULT_FINAL_REPORT_PATH = DEFAULT_ARTIFACT_DIR / "final_report.json"
DEFAULT_PROMOTION_MANIFEST_PATH = DEFAULT_ARTIFACT_DIR / "enron_promotion_manifest.json"
DEFAULT_MAX_CONCURRENT_QUESTIONS = 8
DEFAULT_MAX_CONCURRENT_JUDGE_CALLS = 4
DEFAULT_REPRO_RUNS = 3
DEFAULT_LATENCY_SLA_MS = 15000
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_PLATEAU_THRESHOLD = 0.02
DEFAULT_PLATEAU_WINDOW = 2
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 600
JUDGE_ENDPOINT = os.environ.get(
    "GRAPHRAG_JUDGE_ENDPOINT",
    "databricks-claude-sonnet-4-6",
)

INCLUDED_ATTORNEY_CATEGORIES = [
    "org_structure",
    "person_profile",
    "relationship_analysis",
    "documentary_evidence",
    "quantitative_analysis",
    "timeline_reconstruction",
]

QUALITY_METRIC_NAMES = [
    "factual_accuracy",
    "grounding_integrity",
    "hallucination_detection",
    "answer_completeness",
    "citation_accuracy",
    "evidence_fabrication",
    "provenance_structure_compliance",
    "provenance_content_quality",
]
PRIMARY_METRIC_NAME = "benchmark_score"

QUESTION_FAILURE_THRESHOLDS = {
    "benchmark_score": 0.65,
    "factual_accuracy": 0.8,
    "grounding_integrity": 0.7,
    "hallucination_detection": 0.7,
    "answer_completeness": 0.5,
    "citation_accuracy": 0.7,
    "evidence_fabrication": 0.8,
    "provenance_structure_compliance": 0.75,
    "provenance_content_quality": 0.4,
}

FAILURE_CLASS_METADATA: dict[str, dict[str, Any]] = {
    "routing_classification_failure": {
        "kind": "quality",
        "severity": 1.0,
        "default_fix": (
            "Tighten factual routing heuristics so documentary questions do not "
            "bypass into timeline/entity_explore on date tokens alone."
        ),
        "expected_metric_impact": (
            "+0.03 benchmark_score and better primitive_match_rate on the "
            "documentary/timeline slice"
        ),
        "change_type": "routing_condition",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_apply_factual_routing_overrides",
            "src/agent/_agent_core.py::_get_case_based_pattern_hint",
            "src/agent/_agent_core.py::GraphRAGAgent.predict",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "planner_decomposition_failure": {
        "kind": "quality",
        "severity": 0.85,
        "default_fix": (
            "Constrain planner decomposition for deterministic factual prompts "
            "so it does not decompose into weaker primitives than the expected slice."
        ),
        "expected_metric_impact": (
            "+0.02 benchmark_score on multi-constraint factual questions"
        ),
        "change_type": "planner_condition",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_plan_query",
            "src/agent/_agent_core.py::GraphRAGAgent.predict",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "wrong_primitive_chosen": {
        "kind": "quality",
        "severity": 0.95,
        "default_fix": (
            "Add stronger documentary-evidence overrides so expected evidence "
            "primitives win over broad timeline or entity-summary paths."
        ),
        "expected_metric_impact": (
            "+0.03 grounding_integrity on documentary and relationship slices"
        ),
        "change_type": "routing_condition",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_apply_factual_routing_overrides",
            "src/agent/_agent_core.py::_plan_from_classification",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "deterministic_query_failure": {
        "kind": "quality",
        "severity": 0.9,
        "default_fix": (
            "Short-circuit missing-table or SQL errors into explicit abstention "
            "and avoid expensive fallback fan-out when deterministic evidence is unavailable."
        ),
        "expected_metric_impact": (
            "Reduce hard failures and lower worst-case latency spikes"
        ),
        "change_type": "error_handling",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_execute_fast_path_stream",
            "src/agent/_agent_core.py::_plan_and_execute_stream",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "retrieval_failure": {
        "kind": "quality",
        "severity": 0.8,
        "default_fix": (
            "Narrow failing retrieval paths and surface explicit retrieval gaps "
            "instead of synthesizing around empty evidence."
        ),
        "expected_metric_impact": (
            "+0.02 grounding_integrity with fewer unsupported responses"
        ),
        "change_type": "retrieval_guardrail",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_assess_evidence_sufficiency",
            "src/agent/_agent_core.py::_render_abstention_response",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "entity_resolution_failure": {
        "kind": "quality",
        "severity": 0.7,
        "default_fix": (
            "Route ambiguous person-path questions through the stronger entity-pair "
            "path and preserve canonical entity correction in the final answer."
        ),
        "expected_metric_impact": (
            "+0.02 factual_accuracy on path and name-resolution regressions"
        ),
        "change_type": "entity_resolution",
        "code_touchpoints": [
            "src/agent/_agent_core.py::resolve_entity_cached",
            "src/agent/_agent_core.py::_validate_tool_consistency",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "evidence_selection_failure": {
        "kind": "quality",
        "severity": 0.95,
        "default_fix": (
            "Prioritize query-relevant email evidence and suppress broad "
            "fallback context when documentary support is thin."
        ),
        "expected_metric_impact": (
            "+0.04 answer_completeness and provenance quality on documentary evidence"
        ),
        "change_type": "evidence_selection",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_assess_evidence_sufficiency",
            "src/agent/_agent_core.py::_build_provenance_guardrail_block",
            "src/agent/_agent_core.py::_apply_claim_verification",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "unsupported_synthesis_hallucination": {
        "kind": "quality",
        "severity": 0.9,
        "default_fix": (
            "Tighten synthesis prompts so unsupported narrative is replaced with "
            "explicit uncertainty or abstention when evidence is weak."
        ),
        "expected_metric_impact": (
            "+0.03 hallucination_detection without sacrificing honest abstention"
        ),
        "change_type": "synthesis_guardrail",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_execute_fast_path_stream",
            "src/agent/_agent_core.py::_plan_and_execute_stream",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "citation_fabrication": {
        "kind": "quality",
        "severity": 1.0,
        "default_fix": (
            "Require claim-level citation support before rendering supporting-evidence "
            "tables or inline email references."
        ),
        "expected_metric_impact": (
            "+0.05 citation_accuracy and evidence_fabrication on documentary slices"
        ),
        "change_type": "provenance_guardrail",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_apply_claim_verification",
            "src/agent/_agent_core.py::_apply_provenance_guardrails",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "provenance_grounding_failure": {
        "kind": "quality",
        "severity": 0.95,
        "default_fix": (
            "Limit provenance to claim-supporting sources and downgrade grounding "
            "when only broad context or partial evidence was retrieved."
        ),
        "expected_metric_impact": (
            "+0.04 provenance_content_quality and grounding_integrity"
        ),
        "change_type": "provenance_guardrail",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_build_provenance_guardrail_block",
            "src/agent/_agent_core.py::_apply_provenance_guardrails",
            "src/agent/_agent_core.py::_build_provenance_metadata",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "abstention_failure": {
        "kind": "quality",
        "severity": 0.75,
        "default_fix": (
            "Differentiate healthy documentary abstention from over-abstention by "
            "adding one more targeted evidence pass before final refusal."
        ),
        "expected_metric_impact": (
            "+0.02 answer_completeness on answerable documentary questions"
        ),
        "change_type": "abstention_policy",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_assess_evidence_sufficiency",
            "src/agent/_agent_core.py::_render_abstention_response",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "reproducibility_instability": {
        "kind": "quality",
        "severity": 0.8,
        "default_fix": (
            "Reduce nondeterminism in answer packaging and provenance ordering for "
            "the reproducibility subset."
        ),
        "expected_metric_impact": (
            "Raise exact-match and token-jaccard stability on the repro subset"
        ),
        "change_type": "stability",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_format_canonical_provenance",
            "src/agent/_agent_core.py::_apply_claim_verification",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "misrouting_to_general": {
        "kind": "latency",
        "severity": 1.0,
        "default_fix": (
            "Prevent slow general routing on deterministic factual prompts that "
            "already have a stronger expected primitive."
        ),
        "expected_metric_impact": (
            "Lower mean and p95 latency on deterministic factual questions"
        ),
        "change_type": "routing_condition",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_get_case_based_pattern_hint",
            "src/agent/_agent_core.py::GraphRAGAgent.predict",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "unnecessary_planner_call": {
        "kind": "latency",
        "severity": 0.8,
        "default_fix": (
            "Skip planner invocation for low-ambiguity factual prompts that are "
            "already confidently classified."
        ),
        "expected_metric_impact": (
            "Lower mean latency without reducing quality on deterministic slices"
        ),
        "change_type": "planner_condition",
        "code_touchpoints": [
            "src/agent/_agent_core.py::GraphRAGAgent.predict",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "unnecessary_llm_stage": {
        "kind": "latency",
        "severity": 0.75,
        "default_fix": (
            "Avoid expensive synthesis stages when retrieval clearly produced no "
            "question-relevant evidence and abstention is inevitable."
        ),
        "expected_metric_impact": (
            "Reduce worst-case latency on documentary abstentions"
        ),
        "change_type": "latency_optimization",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_assess_evidence_sufficiency",
            "src/agent/_agent_core.py::_execute_fast_path_stream",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "low",
    },
    "redundant_tool_calls": {
        "kind": "latency",
        "severity": 0.75,
        "default_fix": (
            "Trim duplicate follow-up evidence calls once the answer contract has "
            "already been satisfied or abstention is determined."
        ),
        "expected_metric_impact": (
            "Lower tool-count inflation and p95 latency"
        ),
        "change_type": "latency_optimization",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_plan_and_execute_stream",
            "src/agent/_agent_core.py::_execute_fast_path_stream",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
    "missed_cache": {
        "kind": "latency",
        "severity": 0.65,
        "default_fix": (
            "Reuse evidence bundles and deterministic intermediate results across "
            "quality and latency passes when the benchmark snapshot is unchanged."
        ),
        "expected_metric_impact": (
            "Raise cache-hit rate and lower average latency across reruns"
        ),
        "change_type": "cache_strategy",
        "code_touchpoints": [
            "src/agent/_agent_core.py",
            "src/_internal/agent/factual_parallel_orchestrator.py",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
            "src/_internal/agent/factual_parallel_orchestrator.py",
        ],
        "regression_risk": "low",
    },
    "remote_when_local_possible": {
        "kind": "latency",
        "severity": 0.6,
        "default_fix": (
            "Default factual benchmark runs to the local backend when the required "
            "local export is present and reserve remote mode for parity checks."
        ),
        "expected_metric_impact": (
            "Cut end-to-end latency on eval loops by removing avoidable remote hops"
        ),
        "change_type": "execution_config",
        "code_touchpoints": [
            "src/_internal/agent/factual_parallel_orchestrator.py",
        ],
        "files_to_modify": [
            "src/_internal/agent/factual_parallel_orchestrator.py",
        ],
        "regression_risk": "low",
    },
    "overbroad_search": {
        "kind": "latency",
        "severity": 0.7,
        "default_fix": (
            "Narrow keyword and semantic search inputs before broad fallback "
            "searches are allowed to fan out."
        ),
        "expected_metric_impact": (
            "Lower search-heavy latency tails and reduce irrelevant evidence"
        ),
        "change_type": "retrieval_guardrail",
        "code_touchpoints": [
            "src/agent/_agent_core.py::_extract_topic_metadata",
            "src/agent/_agent_core.py::_plan_and_execute_stream",
        ],
        "files_to_modify": [
            "src/agent/_agent_core.py",
        ],
        "regression_risk": "medium",
    },
}

REJECTED_FIX_TEMPLATES = [
    {
        "description": "Universal planner bypass",
        "rejection_reason": (
            "Broad bypasses hide routing mistakes instead of fixing them and "
            "can reintroduce citation/provenance regressions."
        ),
    },
    {
        "description": "Wholesale provenance rewrite",
        "rejection_reason": (
            "Formatting-only rewrites are too broad unless traced examples show "
            "they correct the underlying grounding problem."
        ),
    },
]

KNOWN_ARTIFACTS = [
    "factual_benchmark_definition.json",
    "factual_baseline_quality.json",
    "factual_baseline_latency.json",
    "factual_postchange_quality.json",
    "factual_postchange_latency.json",
    "failure_taxonomy.json",
    "root_cause_report.json",
    "improvement_plan.json",
    "implementation_log.json",
    "assessment.json",
    "final_report.json",
    "loop_state.json",
    "enron_promotion_manifest.json",
]

ARTIFACT_REQUIRED_KEYS = {
    "benchmark_definition": {"questions", "composition_summary"},
    "quality": {"overall_metrics", "questions"},
    "latency": {"runtime", "questions"},
    "failure_taxonomy": {"quality_failures", "latency_failures", "ranked_by_impact"},
    "root_cause_report": {"investigated_failure_classes"},
    "improvement_plan": {"changes", "plan_empty"},
    "implementation_log": {"changes_implemented", "changes_skipped"},
    "assessment": {"verdict", "success_criteria_met", "success_criteria_unmet"},
    "final_report": {"termination_reason", "iterations_completed", "history"},
}

_PROVENANCE_SECTIONS = {
    "provenance": r"(?:^|\n)#{1,3}\s*provenance",
    "path": r"(?:^|\n)\s*[-*]?\s*\**path\**\s*:",
    "sources": r"(?:^|\n)\s*[-*]?\s*\**sources\**\s*:",
    "grounding": r"(?:^|\n)\s*[-*]?\s*\**grounding\**\s*:",
}
_ANSWER_PATTERN = r"(?:^|\n)#{1,3}\s*answer"
_WORKER_AGENT = None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric):
        return None
    return numeric


def _mean(values: list[Any]) -> float | None:
    numeric = [_safe_float(v) for v in values]
    filtered = [v for v in numeric if v is not None]
    if not filtered:
        return None
    return round(sum(filtered) / len(filtered), 4)


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = math.ceil(pct / 100 * len(ordered)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return round(float(ordered[idx]), 1)


def _tokenize_text(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = _tokenize_text(left)
    right_tokens = _tokenize_text(right)
    if not left_tokens and not right_tokens:
        return 1.0
    union = left_tokens | right_tokens
    if not union:
        return 1.0
    return round(len(left_tokens & right_tokens) / len(union), 4)


def _trim_text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _require_artifact(
    path: str | Path,
    required_keys: set[str],
    label: str,
) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(f"Missing required {label} artifact: {artifact_path}")
    payload = _read_json(artifact_path)
    missing = sorted(key for key in required_keys if key not in payload)
    if missing:
        missing_str = ", ".join(missing)
        raise ValueError(
            f"Invalid {label} artifact at {artifact_path}; missing keys: {missing_str}"
        )
    return payload


def _wait_for_artifact(
    path: str | Path,
    required_keys: set[str],
    label: str,
    *,
    timeout_seconds: int,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        try:
            return _require_artifact(path, required_keys, label)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(poll_seconds)
    raise TimeoutError(
        f"Timed out waiting for {label} artifact at {Path(path)}"
    ) from last_error


def _copy_measurement_artifacts(
    artifact_root: Path,
    *,
    source_label: str,
    target_label: str,
) -> dict[str, str]:
    copied: dict[str, str] = {}
    for suffix in ("quality", "latency"):
        source = artifact_root / f"factual_{source_label}_{suffix}.json"
        target = artifact_root / f"factual_{target_label}_{suffix}.json"
        if not source.exists():
            continue
        shutil.copy2(source, target)
        copied[suffix] = str(target)
    return copied


def _run_external_command(
    command_template: str,
    *,
    iteration: int,
    artifact_dir: str | Path,
    improvement_plan: str | Path,
    implementation_log: str | Path,
    loop_state: str | Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    context = {
        "iteration": iteration,
        "artifact_dir": str(Path(artifact_dir)),
        "improvement_plan": str(Path(improvement_plan)),
        "implementation_log": str(Path(implementation_log)),
        "loop_state": str(Path(loop_state)),
    }
    try:
        formatted_command = command_template.format(**context)
    except KeyError as exc:
        raise ValueError(
            f"Unknown implementer command placeholder: {exc}"
        ) from exc

    completed = subprocess.run(
        shlex.split(formatted_command),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env={
            **os.environ,
            "GRAPHRAG_FACTUAL_ITERATION": str(iteration),
            "GRAPHRAG_FACTUAL_ARTIFACT_DIR": str(Path(artifact_dir)),
            "GRAPHRAG_FACTUAL_IMPROVEMENT_PLAN": str(Path(improvement_plan)),
            "GRAPHRAG_FACTUAL_IMPLEMENTATION_LOG": str(Path(implementation_log)),
            "GRAPHRAG_FACTUAL_LOOP_STATE": str(Path(loop_state)),
        },
    )
    if completed.returncode != 0:
        stdout = _trim_text(completed.stdout or "", 500)
        stderr = _trim_text(completed.stderr or "", 500)
        raise RuntimeError(
            "External implementer command failed "
            f"(exit={completed.returncode}). stdout={stdout!r} stderr={stderr!r}"
        )
    return completed


def _emit_progress(event: str, **payload: Any) -> None:
    message = {"event": event, **payload}
    print(json.dumps(message, ensure_ascii=False), flush=True)


def _question_text(record: dict[str, Any]) -> str:
    return record.get("question") or record.get("question_text") or ""


def _determinism_class(record: dict[str, Any]) -> str:
    if record.get("attorney_category") == "documentary_evidence":
        return "evidence_backed_synthesis_light"
    return "deterministic_first"


def _load_factual_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in export_governed_flat_questions(corpus="enron"):
        if record.get("attorney_category") not in INCLUDED_ATTORNEY_CATEGORIES:
            continue
        rows.append(copy.deepcopy(record))
    rows.sort(
        key=lambda row: (
            row.get("attorney_category", ""),
            row.get("eval_split", ""),
            row.get("question_id", ""),
        )
    )
    return rows


def _select_repro_subset(records: list[dict[str, Any]], target_size: int = 6) -> list[str]:
    if not records:
        return []
    desired = min(len(records), max(5, target_size))
    selected: list[str] = []
    seen: set[str] = set()

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_category[record.get("attorney_category", "")].append(record)

    for category in INCLUDED_ATTORNEY_CATEGORIES:
        candidates = sorted(
            by_category.get(category, []),
            key=lambda row: (
                row.get("eval_split", ""),
                row.get("question_id", ""),
            ),
        )
        if not candidates:
            continue
        qid = candidates[0]["question_id"]
        if qid not in seen:
            seen.add(qid)
            selected.append(qid)
        if len(selected) >= desired:
            return selected

    for record in records:
        qid = record["question_id"]
        if qid in seen:
            continue
        seen.add(qid)
        selected.append(qid)
        if len(selected) >= desired:
            break

    return selected


def build_benchmark_definition(
    output_path: str | Path = DEFAULT_BENCHMARK_PATH,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    records = _load_factual_records()
    if limit is not None:
        records = records[:limit]

    repro_subset_ids = set(_select_repro_subset(records))
    questions: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()
    by_architecture: Counter[str] = Counter()
    by_determinism: Counter[str] = Counter()

    for record in records:
        determinism_class = _determinism_class(record)
        question = {
            "id": record["question_id"],
            "question_id": record["question_id"],
            "text": _question_text(record),
            "category": record.get("attorney_category"),
            "architecture_primary": record.get("architecture_primary"),
            "primitive": record.get("primitive"),
            "eval_split": record.get("eval_split"),
            "domain": record.get("domain_primary"),
            "determinism_class": determinism_class,
            "in_repro_subset": record["question_id"] in repro_subset_ids,
        }
        questions.append(question)
        by_category[question["category"] or "unknown"] += 1
        by_architecture[question["architecture_primary"] or "unknown"] += 1
        by_determinism[determinism_class] += 1

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "total_questions": len(questions),
        "inclusion_rules": [
            "Only active, validated, governed Enron factual QA questions are included.",
            "Only attorney categories in the approved factual slice are included.",
            "Question metadata is pulled from src/evaluation/question_bank.py.",
        ],
        "exclusion_rules": [
            "Exclude thematic synthesis by omitting case_synthesis/topic_investigation categories.",
            "Exclude adversarial ambiguity and conflict-analysis style probes by selecting only the approved factual categories.",
            "Exclude regime-change reasoning and other open-ended synthesis prompts outside the factual slice.",
        ],
        "questions": questions,
        "composition_summary": {
            "by_category": dict(sorted(by_category.items())),
            "by_architecture": dict(sorted(by_architecture.items())),
            "by_determinism_class": dict(sorted(by_determinism.items())),
        },
    }
    _write_json(output_path, payload)
    return payload


def _records_from_benchmark(
    benchmark: dict[str, Any],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    records_by_id = {row["question_id"]: row for row in _load_factual_records()}
    questions = benchmark.get("questions", [])
    if limit is not None:
        questions = questions[:limit]

    resolved: list[dict[str, Any]] = []
    for item in questions:
        qid = item.get("question_id") or item.get("id")
        if not qid:
            continue
        if qid not in records_by_id:
            raise KeyError(f"Question {qid!r} not found in governed factual bank.")
        record = copy.deepcopy(records_by_id[qid])
        record["determinism_class"] = item.get(
            "determinism_class",
            _determinism_class(record),
        )
        record["in_repro_subset"] = bool(item.get("in_repro_subset"))
        resolved.append(record)
    return resolved


def _call_judge_json(prompt: str, endpoint: str = JUDGE_ENDPOINT) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    response = client.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 512,
        },
    )
    text = response["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


@scorer
def citation_accuracy(inputs, outputs, expectations=None):
    """Judge whether email citations are present and substantively plausible."""

    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")

    prompt = f"""{DATA_CONTEXT}

Evaluate the ACCURACY of email citations in this response. Check:
1. Do cited dates fall within the Enron corpus period (1999-2002)?
2. Do cited senders appear to be real Enron employees or plausible external correspondents?
3. Are cited subject lines specific and plausible?
4. Does each citation appear to support the claim it is attached to?
5. If no citations are present, that itself is a failure.

Scoring rubric (0.0 to 1.0):
- 1.0: All citations look valid and materially support the surrounding claims.
- 0.7: Most citations valid; one or two imprecise.
- 0.5: Some valid, others suspicious or weakly attached.
- 0.3: Few valid citations.
- 0.0: No citations or citations appear fabricated.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(
            value=float(parsed["score"]),
            rationale=parsed.get("justification", ""),
        )
    except Exception as exc:  # pragma: no cover - network-bound
        return Feedback(value=0.0, rationale=f"Judge failed: {exc}")


@scorer
def evidence_fabrication(inputs, outputs, expectations=None):
    """Judge whether evidence appears invented rather than retrieved."""

    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")

    prompt = f"""{DATA_CONTEXT}

Check if this response FABRICATES evidence. Signs of fabrication:
1. Email citations with suspiciously perfect details that do not feel grounded.
2. Claiming specific dates/subjects/body text without retrieved support.
3. Supporting-evidence tables that seem unrelated to the actual answer.
4. Quoted body text that looks generated rather than retrieved.

Note: Honest abstention or saying "no email evidence was retrieved" is NOT fabrication.

Scoring rubric (0.0 to 1.0, where higher is BETTER and means LESS fabrication):
- 1.0: No fabrication detected.
- 0.7: Minor concern; one citation seems questionable.
- 0.5: Mixed; some evidence plausible, some suspicious.
- 0.3: Multiple details appear fabricated.
- 0.0: Blatant evidence fabrication.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(
            value=float(parsed["score"]),
            rationale=parsed.get("justification", ""),
        )
    except Exception as exc:  # pragma: no cover - network-bound
        return Feedback(value=0.0, rationale=f"Judge failed: {exc}")


@scorer
def provenance_structure_compliance(inputs, outputs, expectations=None):
    """Check required Answer + Provenance structure without an LLM."""

    response = outputs if isinstance(outputs, str) else str(outputs)
    response_lower = response.lower()

    found = {}
    missing = []

    if re.search(_ANSWER_PATTERN, response_lower):
        found["answer"] = True
    else:
        missing.append("Answer")

    for section, pattern in _PROVENANCE_SECTIONS.items():
        if re.search(pattern, response_lower):
            found[section] = True
        else:
            missing.append(section.capitalize())

    total_required = 5
    score = round(len(found) / total_required, 3)
    if missing:
        return Feedback(
            value=score,
            rationale=(
                f"Missing sections: {', '.join(missing)}. "
                f"Found {len(found)}/{total_required} required sections."
            ),
        )
    return Feedback(
        value=1.0,
        rationale="All required Answer/Provenance sections present.",
    )


@scorer
def provenance_content_quality(inputs, outputs, expectations=None):
    """Judge provenance content quality rather than just structural presence."""

    response = outputs if isinstance(outputs, str) else str(outputs)
    provenance_match = re.search(r"(?i)#{1,3}\s*provenance(.+)", response, re.DOTALL)
    if not provenance_match:
        return Feedback(
            value=0.0,
            rationale="No Provenance section found — cannot evaluate content quality.",
        )

    provenance_text = provenance_match.group(1)[:2000]
    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    prompt = f"""You are auditing the Provenance section of an AI agent's response for content quality.

Question asked: "{question}"

Provenance section:
---
{provenance_text}
---

Evaluate these three dimensions (each 0.0-1.0):

1. path_quality: Does the Path contain actual entity connections or useful trace structure?
2. source_quality: Do Sources reference specific evidence (email dates, subjects, entity rows, timeline events)?
3. grounding_honesty: Does the Grounding declaration honestly match the uncertainty in the answer?

Return ONLY a JSON object:
{{"path_quality": float, "source_quality": float, "grounding_honesty": float, "justification": "brief explanation"}}"""
    try:
        parsed = _call_judge_json(prompt)
        path_q = float(parsed.get("path_quality", 0.0))
        source_q = float(parsed.get("source_quality", 0.0))
        grounding_q = float(parsed.get("grounding_honesty", 0.0))
        avg = round((path_q + source_q + grounding_q) / 3, 3)
        return Feedback(
            value=avg,
            rationale=(
                f"path={path_q:.2f} sources={source_q:.2f} grounding={grounding_q:.2f}. "
                f"{parsed.get('justification', '')}"
            ),
        )
    except Exception as exc:  # pragma: no cover - network-bound
        return Feedback(value=0.5, rationale=f"Judge error — defaulting to 0.5: {exc}")


QUALITY_SCORERS = [
    factual_accuracy,
    grounding_integrity,
    hallucination_detection,
    answer_completeness,
    citation_accuracy,
    evidence_fabrication,
    provenance_structure_compliance,
    provenance_content_quality,
]

_MLFLOW_EVAL_LOCK = Lock()


def _build_mlflow_eval_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "inputs": {
            "question": _question_text(record),
        },
        "expectations": {
            "question_id": record.get("question_id", ""),
            "expected_entities": list(record.get("expected_entities", [])),
            "graph_ground_truth": record.get("graph_ground_truth", ""),
            "historical_ground_truth": record.get("historical_ground_truth", ""),
            "evidence_required": bool(record.get("evidence_required", True)),
            "category": record.get("attorney_category", ""),
            "architecture_primary": record.get("architecture_primary", ""),
            "primitive": record.get("primitive", ""),
            "eval_split": record.get("eval_split", ""),
        },
    }


def _score_answer_with_mlflow(record: dict[str, Any], answer_text: str) -> dict[str, Any]:
    eval_df = pd.DataFrame([_build_mlflow_eval_row(record)])

    def predict_fn(question: str) -> str:  # noqa: ARG001 - MLflow signature is input-key driven
        return answer_text

    run_name = f"factual_quality_{record.get('question_id', 'unknown')}"
    # mlflow.genai.evaluate has shown thread-safety issues when several judge
    # calls start simultaneously, so serialize the scoring critical section.
    with _MLFLOW_EVAL_LOCK:
        with mlflow.start_run(run_name=run_name):
            results = mlflow.genai.evaluate(
                data=eval_df,
                predict_fn=predict_fn,
                scorers=QUALITY_SCORERS,
            )

    eval_row = results.tables["eval_results"].iloc[0].to_dict()
    metrics: dict[str, float | None] = {}
    rationales: dict[str, str] = {}
    for metric_name in QUALITY_METRIC_NAMES:
        metrics[metric_name] = _safe_float(eval_row.get(f"{metric_name}/value"))
        rationale = eval_row.get(f"{metric_name}/rationale")
        if rationale:
            rationales[metric_name] = _trim_text(rationale, limit=400)

    return {
        "metrics": metrics,
        "benchmark_score": _mean(list(metrics.values())),
        "judge_rationales": rationales,
    }


def _get_worker_agent():
    global _WORKER_AGENT
    if _WORKER_AGENT is None:
        from src.agent.agent_serving import GraphRAGAgent

        _WORKER_AGENT = GraphRAGAgent()
    return _WORKER_AGENT


def _extract_response_text_and_tools(response: Any) -> tuple[str, list[str]]:
    texts: list[str] = []
    tools: list[str] = []
    for item in getattr(response, "output", []):
        item_dict = item.model_dump() if hasattr(item, "model_dump") else item
        if isinstance(item_dict, dict):
            item_type = item_dict.get("type", "")
            if item_type == "function_call":
                tools.append(item_dict.get("name", "?"))
                continue
            if item_type == "message":
                for part in item_dict.get("content", []):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and part.get("text"):
                        texts.append(part["text"])
                continue
            if item_dict.get("text"):
                texts.append(str(item_dict["text"]))
            continue

        item_type = getattr(item, "type", "")
        if item_type == "function_call":
            tools.append(getattr(item, "name", "?"))
            continue
        if item_type == "message":
            for part in getattr(item, "content", []):
                if isinstance(part, dict):
                    if part.get("type") == "output_text" and part.get("text"):
                        texts.append(part["text"])
                elif getattr(part, "type", "") == "output_text" and getattr(part, "text", None):
                    texts.append(part.text)

    return "\n".join(piece for piece in texts if piece).strip(), tools


def _generate_answer_with_metadata(record: dict[str, Any]) -> dict[str, Any]:
    from mlflow.types.responses import ResponsesAgentRequest
    import src.agent.agent_serving as agent_mod

    question = _question_text(record)
    question_id = record.get("question_id", "unknown")
    trace: dict[str, Any] = {
        "pre_class_pattern": "",
        "pre_class_confidence": 0.0,
        "plan_patterns": "",
        "planner_called": False,
        "planner_bypass": False,
    }

    original_classify = agent_mod.classify_and_extract
    original_plan_from_classification = agent_mod._plan_from_classification
    original_plan_query = agent_mod._plan_query
    observed_tool_names: list[str] = []
    patched_tool_invokes: list[tuple[Any, Any]] = []

    def classify_wrapper(*args, **kwargs):
        result = original_classify(*args, **kwargs)
        if isinstance(result, dict):
            trace["pre_class_pattern"] = result.get("pattern", "") or ""
            trace["pre_class_confidence"] = float(result.get("confidence", 0.0) or 0.0)
        return result

    def plan_from_classification_wrapper(*args, **kwargs):
        trace["planner_bypass"] = True
        plan = original_plan_from_classification(*args, **kwargs)
        trace["plan_patterns"] = ",".join(
            sq.pattern for sq in getattr(plan, "sub_questions", [])
        )
        return plan

    def plan_query_wrapper(*args, **kwargs):
        trace["planner_called"] = True
        plan = original_plan_query(*args, **kwargs)
        trace["plan_patterns"] = ",".join(
            sq.pattern for sq in getattr(plan, "sub_questions", [])
        )
        return plan

    agent_mod.classify_and_extract = classify_wrapper
    agent_mod._plan_from_classification = plan_from_classification_wrapper
    agent_mod._plan_query = plan_query_wrapper

    try:
        agent = _get_worker_agent()

        def make_invoke_wrapper(tool_name: str, original_invoke: Any):
            def wrapped_invoke(*args, **kwargs):
                observed_tool_names.append(tool_name)
                return original_invoke(*args, **kwargs)

            return wrapped_invoke

        seen_tool_ids: set[int] = set()
        candidate_tools: list[tuple[str, Any]] = list(agent_mod.TOOL_MAP.items())
        candidate_tools.extend(
            (getattr(tool, "name", "?") or "?", tool)
            for tool in getattr(agent, "tools", [])
        )
        for tool_name, tool in candidate_tools:
            if tool is None or id(tool) in seen_tool_ids:
                continue
            seen_tool_ids.add(id(tool))
            original_invoke = getattr(tool, "invoke", None)
            if not callable(original_invoke):
                continue
            object.__setattr__(
                tool,
                "invoke",
                make_invoke_wrapper(tool_name or "?", original_invoke),
            )
            patched_tool_invokes.append((tool, original_invoke))

        request = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
        started = time.monotonic()
        response = agent.predict(request)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)

        response_text, tool_names = _extract_response_text_and_tools(response)
        if len(observed_tool_names) > len(tool_names):
            tool_names = list(observed_tool_names)
        if not response_text:
            response_text = str(response)

        cache_hits = getattr(agent_mod._backend, "_hits", None)
        cache_misses = getattr(agent_mod._backend, "_misses", None)
        cache_lookup_count = None
        cache_hit_rate = None
        if isinstance(cache_hits, int) and isinstance(cache_misses, int):
            cache_lookup_count = cache_hits + cache_misses
            if cache_lookup_count >= 2:
                cache_hit_rate = round(cache_hits / cache_lookup_count, 4)

        runtime_primary_pattern = ""
        if trace["plan_patterns"]:
            runtime_primary_pattern = trace["plan_patterns"].split(",")[0]
        elif trace["pre_class_pattern"]:
            runtime_primary_pattern = trace["pre_class_pattern"]
        else:
            runtime_primary_pattern = record.get("primitive", "")

        if response_text.startswith("ERROR:"):
            return {
                "question_id": question_id,
                "status": "error",
                "error": response_text[:500],
                "response_text": response_text,
                "latency_ms": elapsed_ms,
                "tool_names": tool_names,
                "tool_count": len(tool_names),
                "pre_class_pattern": trace["pre_class_pattern"] or None,
                "pre_class_confidence": round(trace["pre_class_confidence"], 3),
                "plan_patterns": trace["plan_patterns"] or None,
                "runtime_primary_pattern": runtime_primary_pattern or None,
                "planner_called": bool(trace["planner_called"]),
                "planner_bypass": bool(trace["planner_bypass"]),
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "cache_lookup_count": cache_lookup_count,
                "cache_hit_rate": cache_hit_rate,
            }

        return {
            "question_id": question_id,
            "status": "ok",
            "error": "",
            "response_text": response_text,
            "latency_ms": elapsed_ms,
            "tool_names": tool_names,
            "tool_count": len(tool_names),
            "pre_class_pattern": trace["pre_class_pattern"] or None,
            "pre_class_confidence": round(trace["pre_class_confidence"], 3),
            "plan_patterns": trace["plan_patterns"] or None,
            "runtime_primary_pattern": runtime_primary_pattern or None,
            "planner_called": bool(trace["planner_called"]),
            "planner_bypass": bool(trace["planner_bypass"]),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_lookup_count": cache_lookup_count,
            "cache_hit_rate": cache_hit_rate,
        }
    except Exception as exc:  # pragma: no cover - runtime integration
        return {
            "question_id": question_id,
            "status": "error",
            "error": str(exc),
            "response_text": f"ERROR: {exc}",
            "latency_ms": 0.0,
            "tool_names": [],
            "tool_count": 0,
            "pre_class_pattern": trace["pre_class_pattern"] or None,
            "pre_class_confidence": round(trace["pre_class_confidence"], 3),
            "plan_patterns": trace["plan_patterns"] or None,
            "runtime_primary_pattern": record.get("primitive", None),
            "planner_called": bool(trace["planner_called"]),
            "planner_bypass": bool(trace["planner_bypass"]),
            "cache_hits": None,
            "cache_misses": None,
            "cache_lookup_count": None,
            "cache_hit_rate": None,
        }
    finally:
        for tool, original_invoke in reversed(patched_tool_invokes):
            object.__setattr__(tool, "invoke", original_invoke)
        agent_mod.classify_and_extract = original_classify
        agent_mod._plan_from_classification = original_plan_from_classification
        agent_mod._plan_query = original_plan_query


def _finalize_question_result(
    record: dict[str, Any],
    answer_payload: dict[str, Any],
    score_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    result = {
        "question_id": record.get("question_id"),
        "question": _question_text(record),
        "attorney_category": record.get("attorney_category"),
        "architecture_primary": record.get("architecture_primary"),
        "primitive": record.get("primitive"),
        "eval_split": record.get("eval_split"),
        "domain_primary": record.get("domain_primary"),
        "determinism_class": record.get("determinism_class", _determinism_class(record)),
        "in_repro_subset": bool(record.get("in_repro_subset")),
        "status": answer_payload.get("status", "error"),
        "error": answer_payload.get("error", ""),
        "response_text": answer_payload.get("response_text", ""),
        "latency_ms": _safe_float(answer_payload.get("latency_ms")),
        "tool_names": list(answer_payload.get("tool_names", [])),
        "tool_count": int(answer_payload.get("tool_count", 0) or 0),
        "pre_class_pattern": answer_payload.get("pre_class_pattern"),
        "pre_class_confidence": _safe_float(answer_payload.get("pre_class_confidence")),
        "plan_patterns": answer_payload.get("plan_patterns"),
        "runtime_primary_pattern": answer_payload.get("runtime_primary_pattern"),
        "planner_called": bool(answer_payload.get("planner_called")),
        "planner_bypass": bool(answer_payload.get("planner_bypass")),
        "cache_hits": answer_payload.get("cache_hits"),
        "cache_misses": answer_payload.get("cache_misses"),
        "cache_lookup_count": (
            int(answer_payload["cache_lookup_count"])
            if answer_payload.get("cache_lookup_count") is not None
            else None
        ),
        "cache_hit_rate": _safe_float(answer_payload.get("cache_hit_rate")),
        "benchmark_score": None,
        "judge_rationales": {},
    }

    for metric_name in QUALITY_METRIC_NAMES:
        result[f"{metric_name}/value"] = None

    if score_payload:
        result["benchmark_score"] = _safe_float(score_payload.get("benchmark_score"))
        result["judge_rationales"] = dict(score_payload.get("judge_rationales", {}))
        for metric_name, value in score_payload.get("metrics", {}).items():
            result[f"{metric_name}/value"] = _safe_float(value)

    return result


def _drain_completed_score_futures(
    future_map: dict[Any, tuple[dict[str, Any], dict[str, Any]]],
    results_by_id: dict[str, dict[str, Any]],
    *,
    block: bool = False,
) -> None:
    if not future_map:
        return

    futures = list(future_map.keys())
    if block:
        done = list(as_completed(futures))
    else:
        done, _ = wait(futures, timeout=0, return_when=FIRST_COMPLETED)
        done = list(done)
    if not done:
        return

    for future in done:
        record, answer_payload = future_map.pop(future)
        try:
            score_payload = future.result()
        except Exception as exc:  # pragma: no cover - network-bound
            score_payload = {
                "metrics": {},
                "benchmark_score": None,
                "judge_rationales": {"judge_error": _trim_text(exc, limit=300)},
            }
        result = _finalize_question_result(record, answer_payload, score_payload)
        results_by_id[result["question_id"]] = result
        _emit_progress(
            "question_complete",
            id=result["question_id"],
            elapsed_ms=result["latency_ms"] or 0.0,
            pattern=result.get("runtime_primary_pattern") or result.get("primitive") or "unknown",
            status=result["status"],
        )


def _summarize_quality_groups(
    rows: list[dict[str, Any]],
    group_key: str,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group = row.get(group_key)
        if not group:
            continue
        groups[str(group)].append(row)

    summary: dict[str, dict[str, Any]] = {}
    for group, group_rows in sorted(groups.items()):
        item: dict[str, Any] = {"count": len(group_rows)}
        for metric_name in QUALITY_METRIC_NAMES:
            item[f"{metric_name}/value"] = _mean(
                [row.get(f"{metric_name}/value") for row in group_rows]
            )
        item["overall_score"] = _mean([row.get("benchmark_score") for row in group_rows])
        item["latency_ms"] = _mean([row.get("latency_ms") for row in group_rows])
        item["tool_count"] = _mean([row.get("tool_count") for row in group_rows])
        item["cache_lookup_count"] = _mean([row.get("cache_lookup_count") for row in group_rows])
        item["cache_hit_rate"] = _mean([row.get("cache_hit_rate") for row in group_rows])
        summary[group] = item
    return summary


def _build_worst_examples(
    rows: list[dict[str, Any]],
    *,
    count: int = 10,
) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            float("inf") if row.get("benchmark_score") is None else row.get("benchmark_score"),
            row.get("question_id", ""),
        ),
    )
    worst: list[dict[str, Any]] = []
    for row in ordered[:count]:
        item = {
            "question_id": row.get("question_id"),
            "question": row.get("question"),
            "attorney_category": row.get("attorney_category"),
            "architecture_primary": row.get("architecture_primary"),
            "primitive": row.get("primitive"),
            "eval_split": row.get("eval_split"),
            "overall_score": row.get("benchmark_score"),
            "plan_patterns": row.get("plan_patterns"),
            "pre_class_pattern": row.get("pre_class_pattern"),
            "planner_called": row.get("planner_called"),
            "planner_bypass": row.get("planner_bypass"),
            "tool_count": row.get("tool_count"),
            "cache_lookup_count": row.get("cache_lookup_count"),
            "cache_hit_rate": row.get("cache_hit_rate"),
            "latency_ms": row.get("latency_ms"),
            "response_text": row.get("response_text", ""),
        }
        for metric_name in QUALITY_METRIC_NAMES:
            item[metric_name] = row.get(f"{metric_name}/value")
        worst.append(item)
    return worst


def _run_reproducibility_subset(
    records: list[dict[str, Any]],
    *,
    runs: int = DEFAULT_REPRO_RUNS,
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
) -> dict[str, Any]:
    repro_records = [record for record in records if record.get("in_repro_subset")]
    if not repro_records:
        return {
            "runs_per_question": runs,
            "question_count": 0,
            "exact_match_rate": None,
            "token_jaccard_mean": None,
            "per_question": [],
        }

    outputs_by_question: dict[str, list[str]] = defaultdict(list)
    tasks: list[dict[str, Any]] = []
    for record in repro_records:
        for _ in range(runs):
            tasks.append(record)

    workers = max(1, min(max_concurrent_questions, len(tasks)))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_generate_answer_with_metadata, record): record["question_id"]
            for record in tasks
        }
        for future in as_completed(future_map):
            question_id = future_map[future]
            payload = future.result()
            outputs_by_question[question_id].append(payload.get("response_text", ""))

    per_question: list[dict[str, Any]] = []
    overall_exact: list[float] = []
    overall_jaccard: list[float] = []

    for record in repro_records:
        question_id = record["question_id"]
        responses = outputs_by_question.get(question_id, [])
        exact_scores: list[float] = []
        jaccard_scores: list[float] = []
        for idx in range(len(responses)):
            for jdx in range(idx + 1, len(responses)):
                exact_scores.append(1.0 if responses[idx].strip() == responses[jdx].strip() else 0.0)
                jaccard_scores.append(_token_jaccard(responses[idx], responses[jdx]))
        exact_match_rate = _mean(exact_scores)
        token_jaccard_mean = _mean(jaccard_scores)
        if exact_match_rate is not None:
            overall_exact.append(exact_match_rate)
        if token_jaccard_mean is not None:
            overall_jaccard.append(token_jaccard_mean)
        per_question.append(
            {
                "question_id": question_id,
                "question": _question_text(record),
                "exact_match_rate": exact_match_rate,
                "token_jaccard_mean": token_jaccard_mean,
                "responses": responses,
            }
        )

    return {
        "runs_per_question": runs,
        "question_count": len(repro_records),
        "exact_match_rate": _mean(overall_exact),
        "token_jaccard_mean": _mean(overall_jaccard),
        "per_question": per_question,
    }


def run_quality_evaluation(
    benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH,
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json",
    *,
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
    max_concurrent_judge_calls: int = DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
    limit: int | None = None,
    repro_runs: int = DEFAULT_REPRO_RUNS,
) -> dict[str, Any]:
    benchmark = _read_json(benchmark_path)
    records = _records_from_benchmark(benchmark, limit=limit)
    results_by_id: dict[str, dict[str, Any]] = {}
    score_future_map: dict[Any, tuple[dict[str, Any], dict[str, Any]]] = {}

    started = time.monotonic()
    workers = max(1, min(max_concurrent_questions, len(records) or 1))
    judge_workers = max(1, max_concurrent_judge_calls)

    with ProcessPoolExecutor(max_workers=workers) as answer_pool:
        with ThreadPoolExecutor(max_workers=judge_workers) as judge_pool:
            answer_futures = {
                answer_pool.submit(_generate_answer_with_metadata, record): record
                for record in records
            }
            for answer_future in as_completed(answer_futures):
                record = answer_futures[answer_future]
                try:
                    answer_payload = answer_future.result()
                except Exception as exc:  # pragma: no cover - process-bound
                    answer_payload = {
                        "status": "error",
                        "error": str(exc),
                        "response_text": f"ERROR: {exc}",
                        "latency_ms": 0.0,
                        "tool_names": [],
                        "tool_count": 0,
                        "pre_class_pattern": None,
                        "pre_class_confidence": None,
                        "plan_patterns": None,
                        "runtime_primary_pattern": record.get("primitive"),
                        "planner_called": False,
                        "planner_bypass": False,
                        "cache_hits": None,
                        "cache_misses": None,
                        "cache_hit_rate": None,
                    }

                if answer_payload.get("status") == "ok":
                    future = judge_pool.submit(
                        _score_answer_with_mlflow,
                        record,
                        answer_payload["response_text"],
                    )
                    score_future_map[future] = (record, answer_payload)
                else:
                    result = _finalize_question_result(record, answer_payload, None)
                    results_by_id[result["question_id"]] = result
                    _emit_progress(
                        "question_error",
                        id=result["question_id"],
                        elapsed_ms=result["latency_ms"] or 0.0,
                        pattern=result.get("runtime_primary_pattern") or result.get("primitive") or "unknown",
                        error=result.get("error", ""),
                    )

                _drain_completed_score_futures(score_future_map, results_by_id, block=False)
                _emit_progress(
                    "batch_progress",
                    completed=len(results_by_id),
                    total=len(records),
                    wall_clock_seconds=round(time.monotonic() - started, 1),
                )

            while score_future_map:
                _drain_completed_score_futures(score_future_map, results_by_id, block=True)
                _emit_progress(
                    "batch_progress",
                    completed=len(results_by_id),
                    total=len(records),
                    wall_clock_seconds=round(time.monotonic() - started, 1),
                )

    ordered_question_ids = [
        item.get("question_id") or item.get("id")
        for item in benchmark.get("questions", [])[: len(records)]
    ]
    ordered_results = [
        results_by_id[qid]
        for qid in ordered_question_ids
        if qid in results_by_id
    ]

    successful_rows = [
        row
        for row in ordered_results
        if row.get("status") == "ok" and row.get("benchmark_score") is not None
    ]
    latencies = [
        float(row["latency_ms"])
        for row in successful_rows
        if row.get("latency_ms") is not None
    ]

    overall_metrics = {
        "benchmark_score": _mean([row.get("benchmark_score") for row in successful_rows]),
    }
    for metric_name in QUALITY_METRIC_NAMES:
        overall_metrics[metric_name] = _mean(
            [row.get(f"{metric_name}/value") for row in successful_rows]
        )

    runtime_primary = [row.get("runtime_primary_pattern") for row in successful_rows if row.get("runtime_primary_pattern")]
    planner_bypass_rate = _mean([1.0 if row.get("planner_bypass") else 0.0 for row in successful_rows])
    planner_called_rate = _mean([1.0 if row.get("planner_called") else 0.0 for row in successful_rows])
    general_plan_rate = _mean([1.0 if row.get("runtime_primary_pattern") == "general" else 0.0 for row in successful_rows])
    primitive_match_rate = _mean(
        [
            1.0 if row.get("runtime_primary_pattern") == row.get("primitive") else 0.0
            for row in successful_rows
            if row.get("runtime_primary_pattern")
        ]
    )

    route_diag_rows = [row for row in successful_rows if row.get("primitive")]
    route_diagnostics = {
        "plan_vs_expected_mismatch_rate": _mean(
            [
                1.0 if row.get("runtime_primary_pattern") != row.get("primitive") else 0.0
                for row in route_diag_rows
                if row.get("runtime_primary_pattern")
            ]
        ),
        "preclass_vs_expected_mismatch_rate": _mean(
            [
                1.0 if row.get("pre_class_pattern") != row.get("primitive") else 0.0
                for row in route_diag_rows
                if row.get("pre_class_pattern")
            ]
        ),
        "plan_vs_preclass_mismatch_rate": _mean(
            [
                1.0 if row.get("runtime_primary_pattern") != row.get("pre_class_pattern") else 0.0
                for row in route_diag_rows
                if row.get("runtime_primary_pattern") and row.get("pre_class_pattern")
            ]
        ),
    }

    reproducibility = _run_reproducibility_subset(
        [row for row in records if row.get("in_repro_subset")],
        runs=repro_runs,
        max_concurrent_questions=max_concurrent_questions,
    )

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "benchmark_path": str(benchmark_path),
        "elapsed_s": round(time.monotonic() - started, 1),
        "slice_question_count": len(ordered_results),
        "successful_question_count": len(successful_rows),
        "error_question_count": len(ordered_results) - len(successful_rows),
        "overall_metrics": overall_metrics,
        "routing": {
            "planner_bypass_rate": planner_bypass_rate,
            "planner_called_rate": planner_called_rate,
            "general_plan_rate": general_plan_rate,
            "primitive_match_rate": primitive_match_rate,
        },
        "runtime": {
            "mean_ms": _mean(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "avg_tool_count": _mean([row.get("tool_count") for row in successful_rows]),
            "avg_cache_lookup_count": _mean([row.get("cache_lookup_count") for row in successful_rows]),
            "avg_cache_hit_rate": _mean([row.get("cache_hit_rate") for row in successful_rows]),
        },
        "route_diagnostics": route_diagnostics,
        "reproducibility": reproducibility,
        "by_attorney_category": _summarize_quality_groups(successful_rows, "attorney_category"),
        "by_primitive": _summarize_quality_groups(successful_rows, "primitive"),
        "by_eval_split": _summarize_quality_groups(successful_rows, "eval_split"),
        "by_runtime_plan_pattern": _summarize_quality_groups(successful_rows, "runtime_primary_pattern"),
        "questions": ordered_results,
        "worst_examples": _build_worst_examples(ordered_results, count=min(10, len(ordered_results))),
    }
    _write_json(output_path, payload)
    return payload


def _summarize_latency_groups(
    rows: list[dict[str, Any]],
    group_key: str,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group = row.get(group_key)
        if not group:
            continue
        groups[str(group)].append(row)

    summary: dict[str, dict[str, Any]] = {}
    for group, group_rows in sorted(groups.items()):
        latencies = [
            float(row["latency_ms"])
            for row in group_rows
            if row.get("latency_ms") is not None
        ]
        summary[group] = {
            "count": len(group_rows),
            "mean_ms": _mean(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "avg_tool_count": _mean([row.get("tool_count") for row in group_rows]),
            "avg_cache_lookup_count": _mean([row.get("cache_lookup_count") for row in group_rows]),
            "avg_cache_hit_rate": _mean([row.get("cache_hit_rate") for row in group_rows]),
            "planner_bypass_rate": _mean(
                [1.0 if row.get("planner_bypass") else 0.0 for row in group_rows]
            ),
        }
    return summary


def _order_rows_by_question_ids(
    question_ids: list[str | None],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        question_id = row.get("question_id")
        if question_id and question_id not in rows_by_id:
            rows_by_id[question_id] = row
    return [
        rows_by_id[question_id]
        for question_id in question_ids
        if question_id and question_id in rows_by_id
    ]


def _build_latency_payload(
    *,
    benchmark_path: str | Path,
    mode: str,
    sla_ms: int,
    elapsed_s: float,
    ordered_rows: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    successful_rows = [row for row in ordered_rows if row.get("status") == "ok"]
    latencies = [
        float(row["latency_ms"])
        for row in successful_rows
        if row.get("latency_ms") is not None
    ]
    return {
        "version": "1.0",
        "created_at": _utc_now(),
        "benchmark_path": str(benchmark_path),
        "mode": mode,
        "source": source,
        "elapsed_s": elapsed_s,
        "slice_question_count": len(ordered_rows),
        "successful_question_count": len(successful_rows),
        "error_question_count": len(ordered_rows) - len(successful_rows),
        "runtime": {
            "mean_ms": _mean(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "avg_tool_count": _mean([row.get("tool_count") for row in successful_rows]),
            "avg_cache_lookup_count": _mean([row.get("cache_lookup_count") for row in successful_rows]),
            "avg_cache_hit_rate": _mean([row.get("cache_hit_rate") for row in successful_rows]),
            "sla_ms": int(sla_ms),
        },
        "routing": {
            "planner_bypass_rate": _mean(
                [1.0 if row.get("planner_bypass") else 0.0 for row in successful_rows]
            ),
            "planner_called_rate": _mean(
                [1.0 if row.get("planner_called") else 0.0 for row in successful_rows]
            ),
            "general_plan_rate": _mean(
                [1.0 if row.get("runtime_primary_pattern") == "general" else 0.0 for row in successful_rows]
            ),
            "primitive_match_rate": _mean(
                [
                    1.0 if row.get("runtime_primary_pattern") == row.get("primitive") else 0.0
                    for row in successful_rows
                    if row.get("runtime_primary_pattern")
                ]
            ),
        },
        "by_pattern": _summarize_latency_groups(successful_rows, "runtime_primary_pattern"),
        "by_expected_primitive": _summarize_latency_groups(successful_rows, "primitive"),
        "questions": ordered_rows,
        "sla_violations": [
            {
                "question_id": row["question_id"],
                "question": row["question"],
                "latency_ms": row["latency_ms"],
                "pattern": row.get("runtime_primary_pattern") or row.get("primitive"),
            }
            for row in successful_rows
            if row.get("exceeds_sla")
        ],
    }


def run_latency_evaluation(
    benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH,
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json",
    *,
    mode: str = "isolated",
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
    limit: int | None = None,
    sla_ms: int = DEFAULT_LATENCY_SLA_MS,
    precomputed_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    benchmark = _read_json(benchmark_path)
    records = _records_from_benchmark(benchmark, limit=limit)
    started = time.monotonic()
    ordered_question_ids = [
        record.get("question_id") or record.get("id")
        for record in records
    ]

    if precomputed_rows is not None:
        ordered_rows = _order_rows_by_question_ids(
            ordered_question_ids,
            [copy.deepcopy(row) for row in precomputed_rows],
        )
        for row in ordered_rows:
            row["mode"] = mode
            row["exceeds_sla"] = bool(
                row.get("latency_ms") is not None and float(row["latency_ms"]) > sla_ms
            )
        payload = _build_latency_payload(
            benchmark_path=benchmark_path,
            mode=mode,
            sla_ms=sla_ms,
            elapsed_s=round(time.monotonic() - started, 1),
            ordered_rows=ordered_rows,
            source="quality_pass_reuse",
        )
        _write_json(output_path, payload)
        return payload

    workers = 1 if mode == "isolated" else max(1, min(max_concurrent_questions, len(records) or 1))
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_generate_answer_with_metadata, record): record
            for record in records
        }
        for future in as_completed(future_map):
            record = future_map[future]
            try:
                answer_payload = future.result()
            except Exception as exc:  # pragma: no cover - process-bound
                answer_payload = {
                    "status": "error",
                    "error": str(exc),
                    "response_text": f"ERROR: {exc}",
                    "latency_ms": 0.0,
                    "tool_names": [],
                    "tool_count": 0,
                    "pre_class_pattern": None,
                    "pre_class_confidence": None,
                    "plan_patterns": None,
                    "runtime_primary_pattern": record.get("primitive"),
                    "planner_called": False,
                    "planner_bypass": False,
                    "cache_hits": None,
                    "cache_misses": None,
                    "cache_hit_rate": None,
                }

            row = _finalize_question_result(record, answer_payload, None)
            row["mode"] = mode
            row["exceeds_sla"] = bool(
                row.get("latency_ms") is not None and float(row["latency_ms"]) > sla_ms
            )
            rows.append(row)
            if row["status"] == "ok":
                _emit_progress(
                    "question_complete",
                    id=row["question_id"],
                    elapsed_ms=row["latency_ms"] or 0.0,
                    pattern=row.get("runtime_primary_pattern") or row.get("primitive") or "unknown",
                    status="ok",
                )
            else:
                _emit_progress(
                    "question_error",
                    id=row["question_id"],
                    elapsed_ms=row["latency_ms"] or 0.0,
                    pattern=row.get("runtime_primary_pattern") or row.get("primitive") or "unknown",
                    error=row.get("error", ""),
                )
            _emit_progress(
                "batch_progress",
                completed=len(rows),
                total=len(records),
                wall_clock_seconds=round(time.monotonic() - started, 1),
            )

    ordered_rows = _order_rows_by_question_ids(ordered_question_ids, rows)
    payload = _build_latency_payload(
        benchmark_path=benchmark_path,
        mode=mode,
        sla_ms=sla_ms,
        elapsed_s=round(time.monotonic() - started, 1),
        ordered_rows=ordered_rows,
        source="fresh_run",
    )
    _write_json(output_path, payload)
    return payload


def _metric_from_row(row: dict[str, Any], metric_name: str) -> float | None:
    if metric_name in row:
        return _safe_float(row.get(metric_name))
    return _safe_float(row.get(f"{metric_name}/value"))


def _question_is_quality_failure(row: dict[str, Any]) -> bool:
    if row.get("status") != "ok":
        return True
    for metric_name, threshold in QUESTION_FAILURE_THRESHOLDS.items():
        value = _metric_from_row(row, metric_name)
        if value is None:
            continue
        if value < threshold:
            return True
    return False


def _classify_quality_failure_buckets(row: dict[str, Any]) -> list[str]:
    buckets: list[str] = []
    text = (row.get("response_text") or "").lower()
    error_text = (row.get("error") or "").lower()
    expected = row.get("primitive") or ""
    actual = (
        row.get("runtime_primary_pattern")
        or row.get("plan_patterns")
        or row.get("pre_class_pattern")
        or ""
    )
    factual_accuracy = _metric_from_row(row, "factual_accuracy")
    grounding = _metric_from_row(row, "grounding_integrity")
    hallucination = _metric_from_row(row, "hallucination_detection")
    completeness = _metric_from_row(row, "answer_completeness")
    citation = _metric_from_row(row, "citation_accuracy")
    fabrication = _metric_from_row(row, "evidence_fabrication")
    prov_structure = _metric_from_row(row, "provenance_structure_compliance")
    prov_content = _metric_from_row(row, "provenance_content_quality")

    if row.get("status") != "ok":
        if "sql execution failed" in error_text or "unknown error" in error_text:
            buckets.append("deterministic_query_failure")
        else:
            buckets.append("retrieval_failure")

    if expected and actual and actual != expected:
        buckets.append("routing_classification_failure")
        if actual in {"general", "timeline", "entity_explore", "entity_structure"}:
            buckets.append("wrong_primitive_chosen")
        if row.get("planner_called"):
            buckets.append("planner_decomposition_failure")

    if row.get("attorney_category") == "documentary_evidence":
        if completeness is not None and completeness < 0.45:
            buckets.append("evidence_selection_failure")
        if (
            "no query-relevant email evidence" in text
            and factual_accuracy is not None
            and factual_accuracy < 0.5
        ):
            buckets.append("abstention_failure")

    if (
        (grounding is not None and grounding < 0.6)
        or (prov_content is not None and prov_content < 0.35)
        or (prov_structure is not None and prov_structure < 0.75)
    ):
        buckets.append("provenance_grounding_failure")

    if (
        (citation is not None and citation < 0.55 and ("supporting evidence" in text or "sources" in text))
        or (fabrication is not None and fabrication < 0.65)
    ):
        buckets.append("citation_fabrication")

    if (
        completeness is not None
        and completeness < 0.3
        and hallucination is not None
        and hallucination < 0.7
        and row.get("status") == "ok"
    ):
        buckets.append("unsupported_synthesis_hallucination")

    if (
        row.get("attorney_category") == "relationship_analysis"
        and expected == "entity_pair"
        and actual
        and actual != "entity_pair"
    ):
        buckets.append("entity_resolution_failure")

    return list(dict.fromkeys(buckets))


def _classify_latency_failure_buckets(
    row: dict[str, Any],
    latency_artifact: dict[str, Any],
) -> list[str]:
    buckets: list[str] = []
    if row.get("status") != "ok" or not row.get("exceeds_sla"):
        return buckets

    expected = row.get("primitive") or ""
    actual = row.get("runtime_primary_pattern") or expected
    latency_mode = latency_artifact.get("mode", "isolated")
    backend = os.environ.get("GRAPHRAG_BACKEND", "lakebase")
    text = (row.get("response_text") or "").lower()
    tool_count = int(row.get("tool_count", 0) or 0)
    cache_hits = row.get("cache_hits")
    cache_lookup_count = row.get("cache_lookup_count")
    cache_hit_rate = _safe_float(row.get("cache_hit_rate"))

    if actual == "general" and expected and expected != "general":
        buckets.append("misrouting_to_general")
    if row.get("planner_called"):
        buckets.append("unnecessary_planner_call")
    if tool_count >= 6:
        buckets.append("redundant_tool_calls")
    if (
        isinstance(cache_hits, int)
        and cache_hits > 0
        and isinstance(cache_lookup_count, int)
        and cache_lookup_count > 0
        and cache_hit_rate is not None
        and cache_hit_rate < 0.05
    ):
        buckets.append("missed_cache")
    if backend != "local":
        buckets.append("remote_when_local_possible")
    if actual in {"keyword_search", "timeline"} and tool_count <= 1:
        buckets.append("unnecessary_llm_stage")
    if actual in {"keyword_search", "timeline"} and "semantic_search_emails" in text:
        buckets.append("overbroad_search")
    if latency_mode == "throughput" and actual in {"entity_structure", "entity_pair", "timeline", "keyword_search"}:
        buckets.append("serial_when_parallel_safe")

    return list(dict.fromkeys(buckets))


def _failure_example(row: dict[str, Any], *, failure_class: str) -> dict[str, Any]:
    return {
        "question_id": row.get("question_id"),
        "question": row.get("question"),
        "failure_class": failure_class,
        "attorney_category": row.get("attorney_category"),
        "primitive": row.get("primitive"),
        "runtime_primary_pattern": row.get("runtime_primary_pattern"),
        "planner_called": row.get("planner_called"),
        "planner_bypass": row.get("planner_bypass"),
        "benchmark_score": _metric_from_row(row, "benchmark_score"),
        "latency_ms": _metric_from_row(row, "latency_ms"),
        "error": _trim_text(row.get("error", ""), limit=200),
        "response_preview": _trim_text(row.get("response_text", ""), limit=220),
    }


def _impact_score(failure_class: str, count: int) -> float:
    severity = float(FAILURE_CLASS_METADATA.get(failure_class, {}).get("severity", 0.5))
    return round(count * severity, 3)


def _rank_failure_sections(
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, float], list[str]]:
    impact_scores = {
        section["category"]: _impact_score(section["category"], section["count"])
        for section in sections
    }
    ordered_sections = sorted(
        sections,
        key=lambda section: (
            -impact_scores.get(section["category"], 0.0),
            section["category"],
        ),
    )
    ranked = [section["category"] for section in ordered_sections]
    return ordered_sections, impact_scores, ranked


def _prior_blocker_status(
    prior_assessment: dict[str, Any] | None,
    current_categories: set[str],
) -> list[dict[str, Any]]:
    if not prior_assessment:
        return []

    keyword_map = {
        "provenance": {"provenance_grounding_failure", "citation_fabrication"},
        "citation": {"citation_fabrication"},
        "routing": {
            "routing_classification_failure",
            "wrong_primitive_chosen",
            "misrouting_to_general",
        },
        "planner": {"planner_decomposition_failure", "unnecessary_planner_call"},
        "latency": {
            "misrouting_to_general",
            "unnecessary_planner_call",
            "unnecessary_llm_stage",
            "redundant_tool_calls",
            "missed_cache",
            "remote_when_local_possible",
            "overbroad_search",
        },
        "abstention": {"abstention_failure"},
        "evidence": {
            "evidence_selection_failure",
            "retrieval_failure",
            "deterministic_query_failure",
        },
    }

    status_rows: list[dict[str, Any]] = []
    for blocker in prior_assessment.get("top_blockers", []):
        blocker_lower = blocker.lower()
        matched_classes: set[str] = set()
        for keyword, classes in keyword_map.items():
            if keyword in blocker_lower:
                matched_classes.update(classes)
        if not matched_classes:
            status = "unknown"
        elif current_categories & matched_classes:
            status = "still_present"
        else:
            status = "resolved"
        status_rows.append(
            {
                "blocker": blocker,
                "status": status,
            }
        )
    return status_rows


def analyze_failures(
    quality_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json",
    latency_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json",
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json",
    *,
    iteration: int | None = None,
    prior_assessment_path: str | Path | None = None,
) -> dict[str, Any]:
    quality_artifact = _read_json(quality_path)
    latency_artifact = _read_json(latency_path)
    prior_assessment = None
    if prior_assessment_path and Path(prior_assessment_path).exists():
        prior_assessment = _read_json(prior_assessment_path)

    quality_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latency_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quality_failing_qids: set[str] = set()
    latency_failing_qids: set[str] = set()

    for row in quality_artifact.get("questions", []):
        if not _question_is_quality_failure(row):
            continue
        quality_failing_qids.add(row["question_id"])
        buckets = _classify_quality_failure_buckets(row)
        if not buckets:
            buckets = ["unsupported_synthesis_hallucination"]
        for bucket in buckets:
            quality_examples[bucket].append(_failure_example(row, failure_class=bucket))

    for row in latency_artifact.get("questions", []):
        buckets = _classify_latency_failure_buckets(row, latency_artifact)
        if not buckets:
            continue
        latency_failing_qids.add(row["question_id"])
        for bucket in buckets:
            latency_examples[bucket].append(_failure_example(row, failure_class=bucket))

    quality_sections: list[dict[str, Any]] = []
    quality_total = len(quality_failing_qids) or 1
    for bucket, examples in quality_examples.items():
        unique_qids = {example["question_id"] for example in examples}
        quality_sections.append(
            {
                "category": bucket,
                "count": len(unique_qids),
                "pct_of_failures": round(100 * len(unique_qids) / quality_total, 1),
                "representative_examples": [
                    example["question_id"] for example in examples[:5]
                ],
                "highest_leverage_fix": FAILURE_CLASS_METADATA.get(bucket, {}).get(
                    "default_fix",
                    "No default fix recorded.",
                ),
            }
        )

    latency_sections: list[dict[str, Any]] = []
    latency_total = len(latency_failing_qids) or 1
    for bucket, examples in latency_examples.items():
        unique_qids = {example["question_id"] for example in examples}
        latency_sections.append(
            {
                "category": bucket,
                "count": len(unique_qids),
                "pct_of_failures": round(100 * len(unique_qids) / latency_total, 1),
                "representative_examples": [
                    example["question_id"] for example in examples[:5]
                ],
                "highest_leverage_fix": FAILURE_CLASS_METADATA.get(bucket, {}).get(
                    "default_fix",
                    "No default fix recorded.",
                ),
            }
        )

    quality_sections, quality_impact_scores, quality_ranked = _rank_failure_sections(
        quality_sections
    )
    latency_sections, latency_impact_scores, latency_ranked = _rank_failure_sections(
        latency_sections
    )
    combined_scores = dict(quality_impact_scores)
    combined_scores.update(latency_impact_scores)
    ranked_by_impact = [
        name
        for name, _ in sorted(
            combined_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    current_categories = set(ranked_by_impact)

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "iteration": int(iteration or 0),
        "inputs": {
            "quality_artifact": str(quality_path),
            "latency_artifact": str(latency_path),
            "prior_assessment": str(prior_assessment_path) if prior_assessment_path else None,
        },
        "prior_focus": {
            "top_blockers": (prior_assessment or {}).get("top_blockers", []),
            "next_fixes": (prior_assessment or {}).get("next_fixes", []),
        },
        "total_failing_questions": len(quality_failing_qids | latency_failing_qids),
        "quality_failing_questions": len(quality_failing_qids),
        "latency_failing_questions": len(latency_failing_qids),
        "quality_failures": quality_sections,
        "latency_failures": latency_sections,
        "impact_scores": combined_scores,
        "ranked_by_impact": ranked_by_impact,
        "ranked_quality_failure_classes": quality_ranked,
        "ranked_latency_failure_classes": latency_ranked,
        "prior_blockers_status": _prior_blocker_status(
            prior_assessment,
            current_categories,
        ),
        "examples": {
            "quality": {name: rows[:5] for name, rows in quality_examples.items()},
            "latency": {name: rows[:5] for name, rows in latency_examples.items()},
        },
    }
    _write_json(output_path, payload)
    return payload


def _question_index(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        row["question_id"]: row
        for row in artifact.get("questions", [])
        if row.get("question_id")
    }


def _pick_passing_comparators(
    quality_index: dict[str, dict[str, Any]],
    example_row: dict[str, Any],
    *,
    limit: int = 3,
) -> list[str]:
    if not example_row:
        return []

    primitive = example_row.get("primitive")
    category = example_row.get("attorney_category")
    candidates = [
        row
        for row in quality_index.values()
        if row.get("question_id") != example_row.get("question_id")
        and row.get("status") == "ok"
        and _metric_from_row(row, "benchmark_score") is not None
        and _metric_from_row(row, "benchmark_score") >= 0.75
        and (
            row.get("primitive") == primitive
            or row.get("attorney_category") == category
        )
    ]
    candidates.sort(
        key=lambda row: (
            -(_metric_from_row(row, "benchmark_score") or 0.0),
            row.get("question_id", ""),
        )
    )
    return [row["question_id"] for row in candidates[:limit]]


def _describe_current_behavior(row: dict[str, Any], failure_class: str) -> str:
    expected = row.get("primitive") or "unknown"
    actual = row.get("runtime_primary_pattern") or row.get("plan_patterns") or "unknown"
    latency_ms = _metric_from_row(row, "latency_ms")
    if failure_class in {"routing_classification_failure", "wrong_primitive_chosen"}:
        return (
            f"Expected primitive `{expected}` but runtime path was `{actual}` "
            f"(planner_called={row.get('planner_called')}, planner_bypass={row.get('planner_bypass')})."
        )
    if failure_class == "planner_decomposition_failure":
        return (
            f"Planner was invoked and produced `{row.get('plan_patterns') or actual}` "
            f"for a question expected to use `{expected}`."
        )
    if failure_class in {"citation_fabrication", "provenance_grounding_failure"}:
        return (
            f"Grounding/citation metrics were soft "
            f"(citation={_metric_from_row(row, 'citation_accuracy')}, "
            f"grounding={_metric_from_row(row, 'grounding_integrity')}, "
            f"provenance_content={_metric_from_row(row, 'provenance_content_quality')})."
        )
    if failure_class in {"deterministic_query_failure", "retrieval_failure"}:
        return f"Request produced an execution error: {row.get('error') or _trim_text(row.get('response_text', ''), 120)}"
    if failure_class == "evidence_selection_failure":
        return (
            f"Documentary evidence question had weak completeness/citation support "
            f"(answer_completeness={_metric_from_row(row, 'answer_completeness')}, "
            f"citation_accuracy={_metric_from_row(row, 'citation_accuracy')})."
        )
    if failure_class == "abstention_failure":
        return "The answer abstained or nearly abstained on a question that still scored as answerable."
    if failure_class in {
        "misrouting_to_general",
        "unnecessary_planner_call",
        "unnecessary_llm_stage",
        "redundant_tool_calls",
        "missed_cache",
        "remote_when_local_possible",
        "overbroad_search",
    }:
        return (
            f"Latency was {latency_ms}ms with runtime path `{actual}`, "
            f"tool_count={row.get('tool_count')}, "
            f"cache_lookup_count={row.get('cache_lookup_count')}, "
            f"cache_hit_rate={row.get('cache_hit_rate')}."
        )
    return (
        f"Benchmark score { _metric_from_row(row, 'benchmark_score') } "
        f"with runtime path `{actual}` and expected primitive `{expected}`."
    )


def investigate_root_causes(
    failure_taxonomy_path: str | Path = DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json",
    quality_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json",
    latency_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json",
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "root_cause_report.json",
    *,
    max_failure_classes: int = 5,
    max_examples_per_class: int = 5,
) -> dict[str, Any]:
    taxonomy = _read_json(failure_taxonomy_path)
    quality_artifact = _read_json(quality_path)
    latency_artifact = _read_json(latency_path)
    quality_index = _question_index(quality_artifact)
    latency_index = _question_index(latency_artifact)

    investigated: list[dict[str, Any]] = []
    for failure_class in taxonomy.get("ranked_by_impact", [])[:max_failure_classes]:
        meta = FAILURE_CLASS_METADATA.get(failure_class, {})
        source_examples = (
            taxonomy.get("examples", {}).get("quality", {}).get(failure_class)
            or taxonomy.get("examples", {}).get("latency", {}).get(failure_class)
            or []
        )
        traced_examples: list[dict[str, Any]] = []
        failing_ids = [example["question_id"] for example in source_examples[:max_examples_per_class]]
        for example in source_examples[:max_examples_per_class]:
            qid = example["question_id"]
            row = quality_index.get(qid) or latency_index.get(qid) or example
            traced_examples.append(
                {
                    "question_id": qid,
                    "question_text": row.get("question"),
                    "current_behavior": _describe_current_behavior(row, failure_class),
                    "root_cause": meta.get(
                        "default_fix",
                        "No traced fix direction recorded for this failure class.",
                    ),
                    "proposed_correction": meta.get(
                        "default_fix",
                        "Investigate the failure path in src/agent/_agent_core.py.",
                    ),
                    "validated_on": failing_ids[:3],
                    "regression_check": _pick_passing_comparators(
                        quality_index,
                        row,
                        limit=3,
                    ),
                    "regression_risk": meta.get("regression_risk", "medium"),
                }
            )

        comparator_ids = traced_examples[0]["regression_check"] if traced_examples else []
        investigated.append(
            {
                "failure_class": failure_class,
                "impact_score": taxonomy.get("impact_scores", {}).get(failure_class),
                "code_touchpoints": meta.get("code_touchpoints", []),
                "comparative_signal": {
                    "failing_examples": failing_ids[:3],
                    "passing_comparators": comparator_ids,
                    "observed_difference": (
                        "Failing examples share the traced failure class while passing "
                        "comparators keep higher benchmark scores on the same primitive/category."
                    ),
                },
                "traced_examples": traced_examples,
                "validated_fix_directions": [
                    {
                        "description": meta.get("default_fix"),
                        "supporting_examples": failing_ids[:3],
                        "regression_checks": comparator_ids,
                        "regression_risk": meta.get("regression_risk", "medium"),
                    }
                ],
            }
        )

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "inputs": {
            "failure_taxonomy": str(failure_taxonomy_path),
            "quality_artifact": str(quality_path),
            "latency_artifact": str(latency_path),
        },
        "investigated_failure_classes": investigated,
    }
    _write_json(output_path, payload)
    return payload


def _attempted_fix_descriptions(loop_state: dict[str, Any]) -> set[str]:
    attempted: set[str] = set()
    for entry in loop_state.get("history", []):
        for change in entry.get("changes_implemented", []):
            description = change.get("description")
            if description:
                attempted.add(description.lower())
        plan = entry.get("improvement_plan", {})
        for change in plan.get("changes", []):
            description = change.get("description")
            if description:
                attempted.add(description.lower())
    return attempted


def plan_improvements(
    root_cause_report_path: str | Path = DEFAULT_ARTIFACT_DIR / "root_cause_report.json",
    failure_taxonomy_path: str | Path = DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json",
    loop_state_path: str | Path = DEFAULT_LOOP_STATE_PATH,
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "improvement_plan.json",
    *,
    max_changes: int = 5,
) -> dict[str, Any]:
    root_cause_report = _read_json(root_cause_report_path)
    failure_taxonomy = _read_json(failure_taxonomy_path)
    loop_state = _load_or_init_loop_state(loop_state_path)
    attempted = _attempted_fix_descriptions(loop_state)

    changes: list[dict[str, Any]] = []
    rejected = list(REJECTED_FIX_TEMPLATES)
    seen_descriptions: set[str] = set()

    for idx, item in enumerate(root_cause_report.get("investigated_failure_classes", []), start=1):
        failure_class = item.get("failure_class", "")
        meta = FAILURE_CLASS_METADATA.get(failure_class, {})
        description = meta.get("default_fix")
        if not description:
            continue
        normalized = description.lower()
        if normalized in seen_descriptions:
            continue
        if normalized in attempted:
            rejected.append(
                {
                    "description": description,
                    "rejection_reason": (
                        "A similar fix already appears in loop_state history; "
                        "avoid re-proposing it without new traced evidence."
                    ),
                }
            )
            continue
        traced_examples = item.get("traced_examples", [])
        question_ids = [example.get("question_id") for example in traced_examples if example.get("question_id")]
        questions_at_risk = traced_examples[0].get("regression_check", []) if traced_examples else []
        changes.append(
            {
                "id": f"fix_{idx:03d}",
                "description": description,
                "target_failure_class": failure_class,
                "evidence_ref": f"root_cause_report.investigated_failure_classes[{idx - 1}]",
                "questions_fixed": question_ids[:5],
                "questions_at_risk": questions_at_risk[:3],
                "expected_metric_impact": meta.get("expected_metric_impact"),
                "regression_risk": meta.get("regression_risk", "medium"),
                "files_to_modify": meta.get("files_to_modify", []),
                "change_type": meta.get("change_type", "code_change"),
            }
        )
        seen_descriptions.add(normalized)
        if len(changes) >= max_changes:
            break

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "iteration": loop_state.get("iteration", 0),
        "inputs": {
            "root_cause_report": str(root_cause_report_path),
            "failure_taxonomy": str(failure_taxonomy_path),
            "loop_state": str(loop_state_path),
        },
        "changes": changes,
        "rejected_fixes": rejected,
        "plan_empty": len(changes) == 0,
        "ranked_by_impact": failure_taxonomy.get("ranked_by_impact", []),
    }
    _write_json(output_path, payload)
    return payload


def _failure_count_from_quality(artifact: dict[str, Any]) -> int:
    return sum(
        1
        for row in artifact.get("questions", [])
        if _question_is_quality_failure(row)
    )


def _detect_plateau(loop_state: dict[str, Any], primary_metric_delta: float | None) -> bool:
    if primary_metric_delta is None:
        return False
    deltas: list[float] = []
    for entry in loop_state.get("history", []):
        assessment = entry.get("assessment", {})
        delta = assessment.get("primary_metric_delta")
        if delta is None:
            continue
        try:
            deltas.append(abs(float(delta)))
        except (TypeError, ValueError):
            continue
    deltas.append(abs(primary_metric_delta))
    if len(deltas) < DEFAULT_PLATEAU_WINDOW:
        return False
    return all(delta < DEFAULT_PLATEAU_THRESHOLD for delta in deltas[-DEFAULT_PLATEAU_WINDOW :])


def assess_iteration(
    baseline_quality_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json",
    postchange_quality_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_postchange_quality.json",
    baseline_latency_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json",
    postchange_latency_path: str | Path = DEFAULT_ARTIFACT_DIR / "factual_postchange_latency.json",
    failure_taxonomy_path: str | Path = DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json",
    loop_state_path: str | Path = DEFAULT_LOOP_STATE_PATH,
    output_path: str | Path = DEFAULT_ARTIFACT_DIR / "assessment.json",
    *,
    plateau_threshold: float = DEFAULT_PLATEAU_THRESHOLD,
    plateau_window: int = DEFAULT_PLATEAU_WINDOW,
) -> dict[str, Any]:
    baseline_quality = _read_json(baseline_quality_path)
    postchange_quality = _read_json(postchange_quality_path)
    baseline_latency = _read_json(baseline_latency_path)
    postchange_latency = _read_json(postchange_latency_path)
    failure_taxonomy = _read_json(failure_taxonomy_path)
    loop_state = _load_or_init_loop_state(loop_state_path)

    baseline_primary = _metric_from_row(baseline_quality.get("overall_metrics", {}), PRIMARY_METRIC_NAME)
    postchange_primary = _metric_from_row(postchange_quality.get("overall_metrics", {}), PRIMARY_METRIC_NAME)
    if baseline_primary is None:
        baseline_primary = _metric_from_row(
            baseline_quality.get("overall_metrics", {}),
            "factual_accuracy",
        )
    if postchange_primary is None:
        postchange_primary = _metric_from_row(
            postchange_quality.get("overall_metrics", {}),
            "factual_accuracy",
        )
    primary_metric_delta = None
    if baseline_primary is not None and postchange_primary is not None:
        primary_metric_delta = round(postchange_primary - baseline_primary, 4)

    quality_regressions: list[str] = []
    metric_deltas: dict[str, float | None] = {}
    for metric_name in [PRIMARY_METRIC_NAME, *QUALITY_METRIC_NAMES]:
        baseline_value = _metric_from_row(baseline_quality.get("overall_metrics", {}), metric_name)
        post_value = _metric_from_row(postchange_quality.get("overall_metrics", {}), metric_name)
        if baseline_value is None or post_value is None:
            metric_deltas[metric_name] = None
            continue
        delta = round(post_value - baseline_value, 4)
        metric_deltas[metric_name] = delta
        if delta < -0.01:
            quality_regressions.append(f"{metric_name}: {delta:+.3f}")

    baseline_mean_latency = _metric_from_row(baseline_latency.get("runtime", {}), "mean_ms")
    postchange_mean_latency = _metric_from_row(postchange_latency.get("runtime", {}), "mean_ms")
    baseline_p95_latency = _metric_from_row(baseline_latency.get("runtime", {}), "p95_ms")
    postchange_p95_latency = _metric_from_row(postchange_latency.get("runtime", {}), "p95_ms")
    latency_regressions: list[str] = []
    if (
        baseline_mean_latency is not None
        and postchange_mean_latency is not None
        and postchange_mean_latency > baseline_mean_latency * 1.05
    ):
        latency_regressions.append(
            f"mean_latency_ms: {postchange_mean_latency - baseline_mean_latency:+.1f}"
        )
    if (
        baseline_p95_latency is not None
        and postchange_p95_latency is not None
        and postchange_p95_latency > baseline_p95_latency * 1.05
    ):
        latency_regressions.append(
            f"p95_latency_ms: {postchange_p95_latency - baseline_p95_latency:+.1f}"
        )

    baseline_failure_count = _failure_count_from_quality(baseline_quality)
    postchange_failure_count = _failure_count_from_quality(postchange_quality)
    repro = postchange_quality.get("reproducibility", {})
    abstention_failures = 0
    for item in failure_taxonomy.get("quality_failures", []):
        if item.get("category") == "abstention_failure":
            abstention_failures = int(item.get("count", 0))
            break

    success_criteria_met: list[int] = []
    success_criteria_unmet: list[int] = []

    criterion_1 = primary_metric_delta is not None and primary_metric_delta >= 0.02
    criterion_2 = (
        _metric_from_row(postchange_quality.get("overall_metrics", {}), "citation_accuracy") or 0.0
    ) >= 0.9 and (
        _metric_from_row(postchange_quality.get("overall_metrics", {}), "evidence_fabrication") or 0.0
    ) >= 0.9
    criterion_3 = (
        (_metric_from_row(postchange_quality.get("overall_metrics", {}), "provenance_structure_compliance") or 0.0) >= 0.8
        and (_metric_from_row(postchange_quality.get("overall_metrics", {}), "provenance_content_quality") or 0.0) >= 0.4
    )
    criterion_4 = abstention_failures == 0
    criterion_5 = (
        (_safe_float(repro.get("exact_match_rate")) or 0.0) >= 0.8
        and (_safe_float(repro.get("token_jaccard_mean")) or 0.0) >= 0.9
    )
    criterion_6 = postchange_failure_count < baseline_failure_count
    criterion_7 = (
        baseline_mean_latency is not None
        and postchange_mean_latency is not None
        and postchange_mean_latency <= baseline_mean_latency * 0.95
        and (primary_metric_delta is None or primary_metric_delta >= 0.0)
    )

    for idx, passed in enumerate(
        [
            criterion_1,
            criterion_2,
            criterion_3,
            criterion_4,
            criterion_5,
            criterion_6,
            criterion_7,
        ],
        start=1,
    ):
        if passed:
            success_criteria_met.append(idx)
        else:
            success_criteria_unmet.append(idx)

    top_blockers = []
    for failure_class in failure_taxonomy.get("ranked_by_impact", [])[:5]:
        meta = FAILURE_CLASS_METADATA.get(failure_class, {})
        top_blockers.append(
            meta.get("default_fix", failure_class.replace("_", " "))
        )

    next_fixes = []
    for failure_class in failure_taxonomy.get("ranked_by_impact", [])[:3]:
        meta = FAILURE_CLASS_METADATA.get(failure_class, {})
        if meta.get("default_fix"):
            next_fixes.append(meta["default_fix"])

    if primary_metric_delta is None:
        plateau_detected = False
    else:
        deltas: list[float] = []
        for entry in loop_state.get("history", []):
            assessment = entry.get("assessment", {})
            delta = assessment.get("primary_metric_delta")
            if delta is None:
                continue
            try:
                deltas.append(abs(float(delta)))
            except (TypeError, ValueError):
                continue
        deltas.append(abs(primary_metric_delta))
        plateau_detected = (
            len(deltas) >= plateau_window
            and all(delta < plateau_threshold for delta in deltas[-plateau_window:])
        )
    verdict = "READY" if not success_criteria_unmet else "NOT_READY"
    if plateau_detected and verdict != "READY":
        recommendation = "Plateau detected — stop unless a new traced high-confidence fix emerges."
    elif verdict == "READY":
        recommendation = "Ready to stop — current slice meets the configured success criteria."
    else:
        recommendation = "Continue iteration. The top blockers still have traced, high-leverage fixes."

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "iteration": loop_state.get("iteration", 0),
        "verdict": verdict,
        "primary_metric": PRIMARY_METRIC_NAME,
        "primary_metric_delta": primary_metric_delta,
        "metric_deltas": metric_deltas,
        "regressions": quality_regressions + latency_regressions,
        "success_criteria_met": success_criteria_met,
        "success_criteria_unmet": success_criteria_unmet,
        "top_blockers": top_blockers[:5],
        "next_fixes": next_fixes[:3],
        "plateau_detected": plateau_detected,
        "plateau_threshold": plateau_threshold,
        "plateau_window": plateau_window,
        "recommendation": recommendation,
        "inputs": {
            "baseline_quality": str(baseline_quality_path),
            "postchange_quality": str(postchange_quality_path),
            "baseline_latency": str(baseline_latency_path),
            "postchange_latency": str(postchange_latency_path),
            "failure_taxonomy": str(failure_taxonomy_path),
            "loop_state": str(loop_state_path),
        },
    }
    _write_json(output_path, payload)
    return payload


def archive_iteration_artifacts(
    iteration: int,
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    archive_root = artifact_root / "iterations" / f"iter_{iteration}"
    archive_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for filename in KNOWN_ARTIFACTS:
        source = artifact_root / filename
        if not source.exists():
            continue
        shutil.copy2(source, archive_root / filename)
        copied.append(filename)

    payload = {
        "iteration": iteration,
        "archived_at": _utc_now(),
        "archive_dir": str(archive_root),
        "copied_files": copied,
    }
    _write_json(archive_root / "archive_manifest.json", payload)
    return payload


def _load_or_init_loop_state(path: str | Path) -> dict[str, Any]:
    loop_state_path = Path(path)
    if loop_state_path.exists():
        return _read_json(loop_state_path)
    return {
        "version": "1.0",
        "updated_at": _utc_now(),
        "iteration": 0,
        "phase": "INIT",
        "history": [],
    }


def _ensure_history_entry(
    loop_state: dict[str, Any],
    *,
    iteration: int,
    phase: str,
    label: str | None = None,
) -> dict[str, Any]:
    history = loop_state.setdefault("history", [])
    for entry in history:
        if entry.get("iteration") != iteration:
            continue
        entry["phase"] = phase
        entry.setdefault("artifacts", {})
        entry.setdefault("started_at", _utc_now())
        labels = entry.setdefault("labels", [])
        if entry.get("label") and not labels:
            labels.append(entry["label"])
        if label:
            entry.setdefault("label", label)
            if label not in labels:
                labels.append(label)
        return entry

    entry = {
        "iteration": iteration,
        "label": label,
        "labels": [label] if label else [],
        "phase": phase,
        "started_at": _utc_now(),
        "artifacts": {},
    }
    history.append(entry)
    return entry


def _archive_iteration_if_present(
    iteration: int,
    *,
    artifact_dir: str | Path,
) -> dict[str, Any] | None:
    artifact_root = Path(artifact_dir)
    if not any((artifact_root / filename).exists() for filename in KNOWN_ARTIFACTS):
        return None
    return archive_iteration_artifacts(iteration, artifact_dir=artifact_root)


def orchestrate_measure(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    iteration: int = 1,
    label: str = "baseline",
    limit: int | None = None,
    parallel_subagents: bool = True,
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
    max_concurrent_judge_calls: int = DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
    latency_mode: str = "isolated",
    latency_sla_ms: int = DEFAULT_LATENCY_SLA_MS,
    subagent_timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)

    benchmark_path = artifact_root / DEFAULT_BENCHMARK_PATH.name
    quality_path = artifact_root / f"factual_{label}_quality.json"
    latency_path = artifact_root / f"factual_{label}_latency.json"
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name

    if not benchmark_path.exists():
        build_benchmark_definition(benchmark_path, limit=limit)
    _require_artifact(
        benchmark_path,
        ARTIFACT_REQUIRED_KEYS["benchmark_definition"],
        "benchmark definition",
    )

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "MEASURE"
    loop_state["updated_at"] = _utc_now()
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=label,
        phase="MEASURE",
    )
    entry["artifacts"]["benchmark_definition"] = str(benchmark_path)
    _write_json(loop_state_path, loop_state)

    def _run_quality() -> dict[str, Any]:
        return run_quality_evaluation(
            benchmark_path,
            quality_path,
            max_concurrent_questions=max_concurrent_questions,
            max_concurrent_judge_calls=max_concurrent_judge_calls,
            limit=limit,
        )

    def _run_latency() -> dict[str, Any]:
        return run_latency_evaluation(
            benchmark_path,
            latency_path,
            mode=latency_mode,
            max_concurrent_questions=max_concurrent_questions,
            limit=limit,
            sla_ms=latency_sla_ms,
            precomputed_rows=None,
        )

    if parallel_subagents:
        with ThreadPoolExecutor(max_workers=2) as pool:
            quality_future = pool.submit(_run_quality)
            latency_future = pool.submit(_run_latency)
            quality_payload = quality_future.result(timeout=subagent_timeout_seconds)
            latency_payload = latency_future.result(timeout=subagent_timeout_seconds)
    else:
        quality_payload = _run_quality()
        latency_payload = _run_latency()

    _require_artifact(quality_path, ARTIFACT_REQUIRED_KEYS["quality"], f"{label} quality")
    _require_artifact(latency_path, ARTIFACT_REQUIRED_KEYS["latency"], f"{label} latency")

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["phase"] = "MEASURE_COMPLETE"
    loop_state["updated_at"] = _utc_now()
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=label,
        phase="MEASURE_COMPLETE",
    )
    entry["completed_at"] = _utc_now()
    entry["artifacts"].update(
        {
            "quality": str(quality_path),
            "latency": str(latency_path),
            f"{label}_quality": str(quality_path),
            f"{label}_latency": str(latency_path),
        }
    )
    _write_json(loop_state_path, loop_state)

    return {
        "benchmark_definition": str(benchmark_path),
        "quality": str(quality_path),
        "latency": str(latency_path),
        "loop_state": str(loop_state_path),
        "quality_summary": quality_payload.get("overall_metrics", {}),
        "latency_summary": latency_payload.get("runtime", {}),
    }


def orchestrate_analyze(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    quality_label: str = "baseline",
    iteration: int = 1,
    prior_assessment_path: str | Path | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    quality_path = artifact_root / f"factual_{quality_label}_quality.json"
    latency_path = artifact_root / f"factual_{quality_label}_latency.json"
    failure_path = artifact_root / "failure_taxonomy.json"
    root_cause_path = artifact_root / "root_cause_report.json"
    plan_path = artifact_root / "improvement_plan.json"
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "DIAGNOSE"
    loop_state["updated_at"] = _utc_now()
    _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=quality_label,
        phase="DIAGNOSE",
    )
    _write_json(loop_state_path, loop_state)

    failure_payload = analyze_failures(
        quality_path,
        latency_path,
        failure_path,
        iteration=iteration,
        prior_assessment_path=prior_assessment_path,
    )
    loop_state["phase"] = "ROOT_CAUSE"
    loop_state["updated_at"] = _utc_now()
    _write_json(loop_state_path, loop_state)

    root_cause_payload = investigate_root_causes(
        failure_path,
        quality_path,
        latency_path,
        root_cause_path,
    )
    loop_state["phase"] = "PLAN"
    loop_state["updated_at"] = _utc_now()
    _write_json(loop_state_path, loop_state)

    plan_payload = plan_improvements(
        root_cause_path,
        failure_path,
        loop_state_path,
        plan_path,
    )
    _require_artifact(
        failure_path,
        ARTIFACT_REQUIRED_KEYS["failure_taxonomy"],
        "failure taxonomy",
    )
    _require_artifact(
        root_cause_path,
        ARTIFACT_REQUIRED_KEYS["root_cause_report"],
        "root cause report",
    )
    _require_artifact(
        plan_path,
        ARTIFACT_REQUIRED_KEYS["improvement_plan"],
        "improvement plan",
    )
    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["phase"] = "PLAN_COMPLETE"
    loop_state["updated_at"] = _utc_now()
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=quality_label,
        phase="PLAN_COMPLETE",
    )
    entry["artifacts"].update(
        {
            "failure_taxonomy": str(failure_path),
            "root_cause_report": str(root_cause_path),
            "improvement_plan": str(plan_path),
        }
    )
    entry["improvement_plan"] = plan_payload
    _write_json(loop_state_path, loop_state)

    return {
        "failure_taxonomy": str(failure_path),
        "root_cause_report": str(root_cause_path),
        "improvement_plan": str(plan_path),
        "loop_state": str(loop_state_path),
        "ranked_failures": failure_payload.get("ranked_by_impact", []),
        "planned_changes": len(plan_payload.get("changes", [])),
    }


def orchestrate_assess(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    iteration: int = 1,
    baseline_label: str = "baseline",
    postchange_label: str = "postchange",
    plateau_threshold: float = DEFAULT_PLATEAU_THRESHOLD,
    plateau_window: int = DEFAULT_PLATEAU_WINDOW,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    assessment_path = artifact_root / "assessment.json"
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "ASSESS"
    loop_state["updated_at"] = _utc_now()
    _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=postchange_label,
        phase="ASSESS",
    )
    _write_json(loop_state_path, loop_state)

    payload = assess_iteration(
        artifact_root / f"factual_{baseline_label}_quality.json",
        artifact_root / f"factual_{postchange_label}_quality.json",
        artifact_root / f"factual_{baseline_label}_latency.json",
        artifact_root / f"factual_{postchange_label}_latency.json",
        artifact_root / "failure_taxonomy.json",
        loop_state_path,
        assessment_path,
        plateau_threshold=plateau_threshold,
        plateau_window=plateau_window,
    )
    _require_artifact(assessment_path, ARTIFACT_REQUIRED_KEYS["assessment"], "assessment")

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "ASSESS_COMPLETE"
    loop_state["updated_at"] = _utc_now()
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=postchange_label,
        phase="ASSESS_COMPLETE",
    )
    entry["artifacts"]["assessment"] = str(assessment_path)
    entry["assessment"] = {
        "verdict": payload.get("verdict"),
        "primary_metric_delta": payload.get("primary_metric_delta"),
    }
    _write_json(loop_state_path, loop_state)
    promotion_manifest = emit_promotion_manifest(
        artifact_dir=artifact_root,
        candidate_label=postchange_label,
        output_path=artifact_root / DEFAULT_PROMOTION_MANIFEST_PATH.name,
    )
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        label=postchange_label,
        phase="ASSESS_COMPLETE",
    )
    entry["artifacts"]["promotion_manifest"] = promotion_manifest[
        "manifest_path"
    ]
    _write_json(loop_state_path, loop_state)
    return {
        "assessment": str(assessment_path),
        "loop_state": str(loop_state_path),
        "verdict": payload.get("verdict"),
        "primary_metric_delta": payload.get("primary_metric_delta"),
        "promotion_manifest": promotion_manifest["manifest_path"],
    }


def emit_promotion_manifest(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    candidate_label: str = "postchange",
    output_path: str | Path = DEFAULT_PROMOTION_MANIFEST_PATH,
) -> dict[str, Any]:
    return build_promotion_manifest(
        artifact_dir=artifact_dir,
        candidate_label=candidate_label,
        output_path=output_path,
    )


def orchestrate_implement(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    iteration: int = 1,
    implementer_mode: str = "manual",
    implementer_command: str | None = None,
    output_path: str | Path | None = None,
    subagent_timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    plan_path = artifact_root / "improvement_plan.json"
    implementation_log_path = (
        Path(output_path)
        if output_path is not None
        else artifact_root / "implementation_log.json"
    )
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name

    plan_payload = _require_artifact(
        plan_path,
        ARTIFACT_REQUIRED_KEYS["improvement_plan"],
        "improvement plan",
    )

    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "IMPLEMENT"
    loop_state["updated_at"] = _utc_now()
    _ensure_history_entry(
        loop_state,
        iteration=iteration,
        phase="IMPLEMENT",
    )
    _write_json(loop_state_path, loop_state)

    if plan_payload.get("plan_empty"):
        payload = {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": implementer_mode,
            "status": "no_changes_required",
            "plan_empty": True,
            "changes_implemented": [],
            "changes_skipped": [],
            "total_files_modified": 0,
            "total_lines_changed": 0,
        }
        _write_json(implementation_log_path, payload)
    elif implementer_mode == "noop":
        payload = {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": implementer_mode,
            "status": "skipped",
            "plan_empty": False,
            "changes_implemented": [],
            "changes_skipped": [
                {
                    "fix_id": change.get("id"),
                    "description": change.get("description"),
                    "reason": "No external implementer configured for this run.",
                }
                for change in plan_payload.get("changes", [])
            ],
            "total_files_modified": 0,
            "total_lines_changed": 0,
        }
        _write_json(implementation_log_path, payload)
    elif implementer_mode == "manual":
        payload = _wait_for_artifact(
            implementation_log_path,
            ARTIFACT_REQUIRED_KEYS["implementation_log"],
            "implementation log",
            timeout_seconds=subagent_timeout_seconds,
        )
        payload = dict(payload)
        artifact_iteration = payload.get("iteration")
        if artifact_iteration not in (None, iteration):
            raise ValueError(
                "Manual implementation log iteration mismatch: "
                f"expected {iteration}, found {artifact_iteration}"
            )
        payload.setdefault("version", "1.0")
        payload.setdefault("created_at", _utc_now())
        payload["iteration"] = iteration
        payload.setdefault(
            "inputs",
            {
                "improvement_plan": str(plan_path),
            },
        )
        payload.setdefault("implementer_mode", implementer_mode)
        payload.setdefault("plan_empty", False)
        payload.setdefault(
            "status",
            "implemented" if payload.get("changes_implemented") else "skipped",
        )
        if "total_files_modified" not in payload:
            payload["total_files_modified"] = len(
                {
                    file_path
                    for change in payload.get("changes_implemented", [])
                    for file_path in change.get("files_modified", [])
                }
            )
        payload.setdefault("total_lines_changed", 0)
        _write_json(implementation_log_path, payload)
    elif implementer_mode == "command":
        if not implementer_command:
            raise ValueError(
                "implementer_mode='command' requires implementer_command."
            )
        _run_external_command(
            implementer_command,
            iteration=iteration,
            artifact_dir=artifact_root,
            improvement_plan=plan_path,
            implementation_log=implementation_log_path,
            loop_state=loop_state_path,
            timeout_seconds=subagent_timeout_seconds,
        )
        payload = _wait_for_artifact(
            implementation_log_path,
            ARTIFACT_REQUIRED_KEYS["implementation_log"],
            "implementation log",
            timeout_seconds=subagent_timeout_seconds,
        )
        payload = dict(payload)
        artifact_iteration = payload.get("iteration")
        if artifact_iteration not in (None, iteration):
            raise ValueError(
                "Command implementation log iteration mismatch: "
                f"expected {iteration}, found {artifact_iteration}"
            )
        payload.setdefault("version", "1.0")
        payload.setdefault("created_at", _utc_now())
        payload["iteration"] = iteration
        payload.setdefault(
            "inputs",
            {
                "improvement_plan": str(plan_path),
            },
        )
        payload.setdefault("implementer_mode", implementer_mode)
        payload.setdefault("implementer_command", implementer_command)
        payload.setdefault(
            "status",
            "implemented" if payload.get("changes_implemented") else "skipped",
        )
        if "total_files_modified" not in payload:
            payload["total_files_modified"] = len(
                {
                    file_path
                    for change in payload.get("changes_implemented", [])
                    for file_path in change.get("files_modified", [])
                }
            )
        payload.setdefault("total_lines_changed", 0)
        _write_json(implementation_log_path, payload)
    else:
        raise ValueError(
            f"Unsupported implementer mode: {implementer_mode!r}. "
            "Use 'manual', 'noop', or 'command'."
        )

    payload = _require_artifact(
        implementation_log_path,
        ARTIFACT_REQUIRED_KEYS["implementation_log"],
        "implementation log",
    )
    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["iteration"] = iteration
    loop_state["phase"] = "IMPLEMENT_COMPLETE"
    loop_state["updated_at"] = _utc_now()
    entry = _ensure_history_entry(
        loop_state,
        iteration=iteration,
        phase="IMPLEMENT_COMPLETE",
    )
    entry["artifacts"]["implementation_log"] = str(implementation_log_path)
    entry["changes_implemented"] = payload.get("changes_implemented", [])
    entry["changes_skipped"] = payload.get("changes_skipped", [])
    entry["implementer_mode"] = implementer_mode
    if implementer_command:
        entry["implementer_command"] = implementer_command
    _write_json(loop_state_path, loop_state)

    return {
        "implementation_log": str(implementation_log_path),
        "loop_state": str(loop_state_path),
        "status": payload.get("status"),
        "plan_empty": payload.get("plan_empty", False),
        "changes_implemented": len(payload.get("changes_implemented", [])),
        "changes_skipped": len(payload.get("changes_skipped", [])),
    }


def seed_next_iteration(
    assessment_payload: dict[str, Any],
    *,
    loop_state_path: str | Path = DEFAULT_LOOP_STATE_PATH,
) -> dict[str, Any]:
    priors = {
        "seeded_at": _utc_now(),
        "from_iteration": assessment_payload.get("iteration"),
        "top_blockers": assessment_payload.get("top_blockers", [])[:5],
        "next_fixes": assessment_payload.get("next_fixes", [])[:3],
    }
    loop_state = _load_or_init_loop_state(loop_state_path)
    loop_state["next_iteration_priors"] = priors
    if loop_state.get("history"):
        loop_state["history"][-1]["next_iteration_priors"] = priors
    _write_json(loop_state_path, loop_state)
    return priors


def emit_final_report(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output_path: str | Path = DEFAULT_FINAL_REPORT_PATH,
    termination_reason: str | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name
    assessment_path = artifact_root / "assessment.json"
    loop_state = _load_or_init_loop_state(loop_state_path)
    assessment = _read_json(assessment_path) if assessment_path.exists() else None

    history_summary = []
    seen_iterations: set[int] = set()
    for entry in loop_state.get("history", []):
        iteration = entry.get("iteration")
        if iteration is None or iteration in seen_iterations:
            continue
        seen_iterations.add(iteration)
        history_summary.append(
            {
                "iteration": iteration,
                "labels": entry.get("labels", []),
                "phase": entry.get("phase"),
                "assessment": entry.get("assessment"),
                "planned_changes": len(
                    entry.get("improvement_plan", {}).get("changes", [])
                ),
                "implemented_changes": len(entry.get("changes_implemented", [])),
                "skipped_changes": len(entry.get("changes_skipped", [])),
                "artifacts": entry.get("artifacts", {}),
            }
        )

    if termination_reason is None:
        if assessment and assessment.get("verdict") == "READY":
            termination_reason = "READY"
        elif assessment and assessment.get("plateau_detected"):
            termination_reason = "PLATEAU"
        elif history_summary and loop_state.get("iteration", 0) >= (max_iterations or 0):
            termination_reason = "CAP"
        elif history_summary and history_summary[-1].get("planned_changes") == 0:
            termination_reason = "EXHAUSTED"
        else:
            termination_reason = "UNKNOWN"

    payload = {
        "version": "1.0",
        "created_at": _utc_now(),
        "artifact_dir": str(artifact_root),
        "termination_reason": termination_reason,
        "iterations_completed": len(history_summary),
        "latest_iteration": loop_state.get("iteration", 0),
        "final_phase": loop_state.get("phase"),
        "final_verdict": (assessment or {}).get("verdict"),
        "plateau_detected": bool((assessment or {}).get("plateau_detected")),
        "top_blockers": (assessment or {}).get("top_blockers", []),
        "next_fixes": (assessment or {}).get("next_fixes", []),
        "history": history_summary,
        "latest_artifacts": history_summary[-1]["artifacts"] if history_summary else {},
        "inputs": {
            "loop_state": str(loop_state_path),
            "assessment": str(assessment_path) if assessment_path.exists() else None,
        },
    }
    _write_json(output_path, payload)
    return payload


def orchestrate_loop(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    limit: int | None = None,
    parallel_subagents: bool = True,
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
    max_concurrent_judge_calls: int = DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
    latency_mode: str = "isolated",
    latency_sla_ms: int = DEFAULT_LATENCY_SLA_MS,
    implementer_mode: str = "manual",
    implementer_command: str | None = None,
    implementation_log_path: str | Path | None = None,
    subagent_timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    plateau_threshold: float = DEFAULT_PLATEAU_THRESHOLD,
    plateau_window: int = DEFAULT_PLATEAU_WINDOW,
    benchmark_refresh_interval: int | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    benchmark_path = artifact_root / DEFAULT_BENCHMARK_PATH.name
    assessment_path = artifact_root / "assessment.json"
    final_report_path = artifact_root / DEFAULT_FINAL_REPORT_PATH.name
    loop_state_path = artifact_root / DEFAULT_LOOP_STATE_PATH.name

    latest_payloads: dict[str, Any] = {}
    termination_reason = "CAP"
    prior_assessment_path = assessment_path if assessment_path.exists() else None

    for iteration in range(1, max_iterations + 1):
        if benchmark_refresh_interval is not None:
            if iteration == 1 or (iteration - 1) % benchmark_refresh_interval == 0:
                build_benchmark_definition(benchmark_path, limit=limit)
        elif not benchmark_path.exists():
            build_benchmark_definition(benchmark_path, limit=limit)

        measure_payload = orchestrate_measure(
            artifact_dir=artifact_root,
            iteration=iteration,
            label="baseline",
            limit=limit,
            parallel_subagents=parallel_subagents,
            max_concurrent_questions=max_concurrent_questions,
            max_concurrent_judge_calls=max_concurrent_judge_calls,
            latency_mode=latency_mode,
            latency_sla_ms=latency_sla_ms,
            subagent_timeout_seconds=subagent_timeout_seconds,
        )
        analyze_payload = orchestrate_analyze(
            artifact_dir=artifact_root,
            quality_label="baseline",
            iteration=iteration,
            prior_assessment_path=prior_assessment_path,
        )
        latest_payloads = {
            "measure": measure_payload,
            "analyze": analyze_payload,
        }

        plan_payload = _require_artifact(
            artifact_root / "improvement_plan.json",
            ARTIFACT_REQUIRED_KEYS["improvement_plan"],
            "improvement plan",
        )

        if plan_payload.get("plan_empty"):
            _copy_measurement_artifacts(
                artifact_root,
                source_label="baseline",
                target_label="postchange",
            )
            assessment_payload = orchestrate_assess(
                artifact_dir=artifact_root,
                iteration=iteration,
                baseline_label="baseline",
                postchange_label="postchange",
                plateau_threshold=plateau_threshold,
                plateau_window=plateau_window,
            )
            archive_payload = _archive_iteration_if_present(
                iteration,
                artifact_dir=artifact_root,
            )
            latest_payloads.update(
                {
                    "assessment": assessment_payload,
                    "archive": archive_payload,
                }
            )
            termination_reason = "EXHAUSTED"
            break

        implement_payload = orchestrate_implement(
            artifact_dir=artifact_root,
            iteration=iteration,
            implementer_mode=implementer_mode,
            implementer_command=implementer_command,
            output_path=implementation_log_path,
            subagent_timeout_seconds=subagent_timeout_seconds,
        )
        postchange_measure_payload = orchestrate_measure(
            artifact_dir=artifact_root,
            iteration=iteration,
            label="postchange",
            limit=limit,
            parallel_subagents=parallel_subagents,
            max_concurrent_questions=max_concurrent_questions,
            max_concurrent_judge_calls=max_concurrent_judge_calls,
            latency_mode=latency_mode,
            latency_sla_ms=latency_sla_ms,
            subagent_timeout_seconds=subagent_timeout_seconds,
        )
        assessment_payload = orchestrate_assess(
            artifact_dir=artifact_root,
            iteration=iteration,
            baseline_label="baseline",
            postchange_label="postchange",
            plateau_threshold=plateau_threshold,
            plateau_window=plateau_window,
        )
        archive_payload = _archive_iteration_if_present(
            iteration,
            artifact_dir=artifact_root,
        )
        latest_payloads.update(
            {
                "implement": implement_payload,
                "postchange_measure": postchange_measure_payload,
                "assessment": assessment_payload,
                "archive": archive_payload,
            }
        )
        prior_assessment_path = assessment_path if assessment_path.exists() else None

        if assessment_payload.get("verdict") == "READY":
            termination_reason = "READY"
            break
        if assessment_payload.get("plateau_detected"):
            termination_reason = "PLATEAU"
            break

        seed_next_iteration(
            assessment_payload,
            loop_state_path=loop_state_path,
        )
    else:
        termination_reason = "CAP"

    final_report = emit_final_report(
        artifact_dir=artifact_root,
        output_path=final_report_path,
        termination_reason=termination_reason,
        max_iterations=max_iterations,
    )
    return {
        "artifact_dir": str(artifact_root),
        "final_report": str(final_report_path),
        "termination_reason": termination_reason,
        "iterations_completed": final_report.get("iterations_completed"),
        "latest_iteration": final_report.get("latest_iteration"),
        "latest": latest_payloads,
    }


def orchestrate_iteration(
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    iteration: int = 1,
    label: str = "baseline",
    limit: int | None = None,
    parallel_subagents: bool = True,
    max_concurrent_questions: int = DEFAULT_MAX_CONCURRENT_QUESTIONS,
    max_concurrent_judge_calls: int = DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
    latency_mode: str = "isolated",
    latency_sla_ms: int = DEFAULT_LATENCY_SLA_MS,
    skip_measure: bool = False,
    subagent_timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)

    benchmark_path = artifact_root / DEFAULT_BENCHMARK_PATH.name
    quality_path = artifact_root / f"factual_{label}_quality.json"
    latency_path = artifact_root / f"factual_{label}_latency.json"
    assessment_path = artifact_root / "assessment.json"

    measure_payload: dict[str, Any] | None = None
    if not skip_measure or not (benchmark_path.exists() and quality_path.exists() and latency_path.exists()):
        measure_payload = orchestrate_measure(
            artifact_dir=artifact_root,
            iteration=iteration,
            label=label,
            limit=limit,
            parallel_subagents=parallel_subagents,
            max_concurrent_questions=max_concurrent_questions,
            max_concurrent_judge_calls=max_concurrent_judge_calls,
            latency_mode=latency_mode,
            latency_sla_ms=latency_sla_ms,
            subagent_timeout_seconds=subagent_timeout_seconds,
        )

    analyze_payload = orchestrate_analyze(
        artifact_dir=artifact_root,
        quality_label=label,
        iteration=iteration,
        prior_assessment_path=assessment_path if assessment_path.exists() else None,
    )

    assessment_payload: dict[str, Any] | None = None
    baseline_quality = artifact_root / "factual_baseline_quality.json"
    baseline_latency = artifact_root / "factual_baseline_latency.json"
    current_quality = artifact_root / f"factual_{label}_quality.json"
    current_latency = artifact_root / f"factual_{label}_latency.json"
    if (
        baseline_quality.exists()
        and baseline_latency.exists()
        and current_quality.exists()
        and current_latency.exists()
        and label == "postchange"
    ):
        assessment_payload = orchestrate_assess(
            artifact_dir=artifact_root,
            iteration=iteration,
            baseline_label="baseline",
            postchange_label="postchange",
        )

    return {
        "measure": measure_payload,
        "analyze": analyze_payload,
        "assessment": assessment_payload,
        "artifact_dir": str(artifact_root),
        "label": label,
        "iteration": iteration,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel factual hardening orchestration helpers",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    curate_parser = subparsers.add_parser(
        "curate-benchmark",
        help="Build the factual benchmark definition artifact.",
    )
    curate_parser.add_argument(
        "--output",
        default=str(DEFAULT_BENCHMARK_PATH),
        help="Output path for factual_benchmark_definition.json",
    )
    curate_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit benchmark to the first N questions (useful for smoke tests).",
    )

    quality_parser = subparsers.add_parser(
        "evaluate-quality",
        help="Run MLflow-backed quality evaluation for the factual slice.",
    )
    quality_parser.add_argument(
        "--benchmark",
        default=str(DEFAULT_BENCHMARK_PATH),
        help="Benchmark definition artifact path.",
    )
    quality_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json"),
        help="Output path for the quality artifact.",
    )
    quality_parser.add_argument(
        "--max-concurrent-questions",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_QUESTIONS,
        help="Maximum concurrent answer-generation workers.",
    )
    quality_parser.add_argument(
        "--max-concurrent-judge-calls",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
        help="Maximum concurrent MLflow/judge scoring threads.",
    )
    quality_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N benchmark questions.",
    )
    quality_parser.add_argument(
        "--repro-runs",
        type=int,
        default=DEFAULT_REPRO_RUNS,
        help="Number of repeated runs per reproducibility question.",
    )

    latency_parser = subparsers.add_parser(
        "evaluate-latency",
        help="Run latency evaluation for the factual slice.",
    )
    latency_parser.add_argument(
        "--benchmark",
        default=str(DEFAULT_BENCHMARK_PATH),
        help="Benchmark definition artifact path.",
    )
    latency_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json"),
        help="Output path for the latency artifact.",
    )
    latency_parser.add_argument(
        "--mode",
        choices=["isolated", "throughput"],
        default="isolated",
        help="Latency measurement mode.",
    )
    latency_parser.add_argument(
        "--max-concurrent-questions",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_QUESTIONS,
        help="Maximum concurrent answer-generation workers in throughput mode.",
    )
    latency_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N benchmark questions.",
    )
    latency_parser.add_argument(
        "--sla-ms",
        type=int,
        default=DEFAULT_LATENCY_SLA_MS,
        help="Flag questions exceeding this latency SLA.",
    )

    failure_parser = subparsers.add_parser(
        "analyze-failures",
        help="Classify current quality and latency failures into a taxonomy artifact.",
    )
    failure_parser.add_argument(
        "--quality",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json"),
        help="Quality artifact path.",
    )
    failure_parser.add_argument(
        "--latency",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json"),
        help="Latency artifact path.",
    )
    failure_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json"),
        help="Output path for the failure taxonomy artifact.",
    )
    failure_parser.add_argument(
        "--prior-assessment",
        default=None,
        help="Optional prior assessment artifact for blocker carry-forward.",
    )

    root_cause_parser = subparsers.add_parser(
        "investigate-root-causes",
        help="Generate a traced root-cause report from the failure taxonomy.",
    )
    root_cause_parser.add_argument(
        "--failure-taxonomy",
        default=str(DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json"),
        help="Failure taxonomy artifact path.",
    )
    root_cause_parser.add_argument(
        "--quality",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json"),
        help="Quality artifact path.",
    )
    root_cause_parser.add_argument(
        "--latency",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json"),
        help="Latency artifact path.",
    )
    root_cause_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "root_cause_report.json"),
        help="Output path for the root cause artifact.",
    )
    root_cause_parser.add_argument(
        "--max-failure-classes",
        type=int,
        default=5,
        help="Maximum number of failure classes to investigate.",
    )
    root_cause_parser.add_argument(
        "--max-examples-per-class",
        type=int,
        default=5,
        help="Maximum traced examples per failure class.",
    )

    plan_parser = subparsers.add_parser(
        "plan-improvements",
        help="Produce an improvement plan from traced root causes.",
    )
    plan_parser.add_argument(
        "--root-causes",
        default=str(DEFAULT_ARTIFACT_DIR / "root_cause_report.json"),
        help="Root cause report path.",
    )
    plan_parser.add_argument(
        "--failure-taxonomy",
        default=str(DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json"),
        help="Failure taxonomy path.",
    )
    plan_parser.add_argument(
        "--loop-state",
        default=str(DEFAULT_LOOP_STATE_PATH),
        help="Loop state path.",
    )
    plan_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "improvement_plan.json"),
        help="Output path for the improvement plan artifact.",
    )
    plan_parser.add_argument(
        "--max-changes",
        type=int,
        default=5,
        help="Maximum number of plan items to emit.",
    )

    assess_parser = subparsers.add_parser(
        "assess-iteration",
        help="Compare baseline and post-change artifacts and emit an assessment.",
    )
    assess_parser.add_argument(
        "--baseline-quality",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_quality.json"),
        help="Baseline quality artifact path.",
    )
    assess_parser.add_argument(
        "--postchange-quality",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_postchange_quality.json"),
        help="Post-change quality artifact path.",
    )
    assess_parser.add_argument(
        "--baseline-latency",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_baseline_latency.json"),
        help="Baseline latency artifact path.",
    )
    assess_parser.add_argument(
        "--postchange-latency",
        default=str(DEFAULT_ARTIFACT_DIR / "factual_postchange_latency.json"),
        help="Post-change latency artifact path.",
    )
    assess_parser.add_argument(
        "--failure-taxonomy",
        default=str(DEFAULT_ARTIFACT_DIR / "failure_taxonomy.json"),
        help="Failure taxonomy artifact path.",
    )
    assess_parser.add_argument(
        "--loop-state",
        default=str(DEFAULT_LOOP_STATE_PATH),
        help="Loop state path.",
    )
    assess_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "assessment.json"),
        help="Output path for the assessment artifact.",
    )
    assess_parser.add_argument(
        "--plateau-threshold",
        type=float,
        default=DEFAULT_PLATEAU_THRESHOLD,
        help="Per-iteration improvement threshold used for plateau detection.",
    )
    assess_parser.add_argument(
        "--plateau-window",
        type=int,
        default=DEFAULT_PLATEAU_WINDOW,
        help="How many trailing iterations to inspect for plateau detection.",
    )

    measure_parser = subparsers.add_parser(
        "orchestrate-measure",
        help="Run benchmark curation if needed, then quality and latency evaluators.",
    )
    measure_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory (defaults to data/).",
    )
    measure_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number to record in loop_state.json.",
    )
    measure_parser.add_argument(
        "--label",
        choices=["baseline", "postchange"],
        default="baseline",
        help="Artifact label to write (baseline or postchange).",
    )
    measure_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N benchmark questions.",
    )
    measure_parser.add_argument(
        "--max-concurrent-questions",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_QUESTIONS,
        help="Maximum concurrent answer-generation workers.",
    )
    measure_parser.add_argument(
        "--max-concurrent-judge-calls",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
        help="Maximum concurrent MLflow/judge scoring threads.",
    )
    measure_parser.add_argument(
        "--latency-mode",
        choices=["isolated", "throughput"],
        default="isolated",
        help="Latency evaluator mode.",
    )
    measure_parser.add_argument(
        "--latency-sla-ms",
        type=int,
        default=DEFAULT_LATENCY_SLA_MS,
        help="Latency SLA threshold in milliseconds.",
    )
    measure_parser.add_argument(
        "--serial-subagents",
        action="store_true",
        help="Run quality and latency evaluators sequentially instead of in parallel.",
    )
    measure_parser.add_argument(
        "--timeout-per-subagent-seconds",
        type=int,
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        help="Timeout for each evaluator subprocess pair.",
    )

    analyze_parser = subparsers.add_parser(
        "orchestrate-analyze",
        help="Run failure taxonomy, root cause, and improvement planning in sequence.",
    )
    analyze_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    analyze_parser.add_argument(
        "--quality-label",
        choices=["baseline", "postchange"],
        default="baseline",
        help="Which quality/latency label to analyze.",
    )
    analyze_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number to record in loop_state.json.",
    )
    analyze_parser.add_argument(
        "--prior-assessment",
        default=None,
        help="Optional prior assessment artifact path.",
    )

    implement_parser = subparsers.add_parser(
        "orchestrate-implement",
        help="Consume an improvement plan and record the implementation stage artifact.",
    )
    implement_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    implement_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number to record in loop_state.json.",
    )
    implement_parser.add_argument(
        "--implementer-mode",
        choices=["manual", "noop", "command"],
        default="manual",
        help="Implementation stage mode.",
    )
    implement_parser.add_argument(
        "--implementer-command",
        default=None,
        help=(
            "External command template to produce implementation_log.json. "
            "Available placeholders: {iteration}, {artifact_dir}, "
            "{improvement_plan}, {implementation_log}, {loop_state}."
        ),
    )
    implement_parser.add_argument(
        "--output",
        default=str(DEFAULT_ARTIFACT_DIR / "implementation_log.json"),
        help="Implementation log artifact path.",
    )
    implement_parser.add_argument(
        "--timeout-per-subagent-seconds",
        type=int,
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        help="Timeout when waiting for a manual implementation log.",
    )

    orchestrate_assess_parser = subparsers.add_parser(
        "orchestrate-assess",
        help="Run the assessment stage and update loop_state.json.",
    )
    orchestrate_assess_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    orchestrate_assess_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number to record in loop_state.json.",
    )
    orchestrate_assess_parser.add_argument(
        "--baseline-label",
        choices=["baseline", "postchange"],
        default="baseline",
        help="Baseline artifact label.",
    )
    orchestrate_assess_parser.add_argument(
        "--postchange-label",
        choices=["baseline", "postchange"],
        default="postchange",
        help="Post-change artifact label.",
    )
    orchestrate_assess_parser.add_argument(
        "--plateau-threshold",
        type=float,
        default=DEFAULT_PLATEAU_THRESHOLD,
        help="Per-iteration improvement threshold used for plateau detection.",
    )
    orchestrate_assess_parser.add_argument(
        "--plateau-window",
        type=int,
        default=DEFAULT_PLATEAU_WINDOW,
        help="How many trailing iterations to inspect for plateau detection.",
    )

    iteration_parser = subparsers.add_parser(
        "orchestrate-iteration",
        help="Run the current iteration through measure, analyze, and conditional assess.",
    )
    iteration_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    iteration_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number to record in loop_state.json.",
    )
    iteration_parser.add_argument(
        "--label",
        choices=["baseline", "postchange"],
        default="baseline",
        help="Artifact label to write (baseline or postchange).",
    )
    iteration_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N benchmark questions.",
    )
    iteration_parser.add_argument(
        "--max-concurrent-questions",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_QUESTIONS,
        help="Maximum concurrent answer-generation workers.",
    )
    iteration_parser.add_argument(
        "--max-concurrent-judge-calls",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
        help="Maximum concurrent MLflow/judge scoring threads.",
    )
    iteration_parser.add_argument(
        "--latency-mode",
        choices=["isolated", "throughput"],
        default="isolated",
        help="Latency evaluator mode.",
    )
    iteration_parser.add_argument(
        "--latency-sla-ms",
        type=int,
        default=DEFAULT_LATENCY_SLA_MS,
        help="Latency SLA threshold in milliseconds.",
    )
    iteration_parser.add_argument(
        "--serial-subagents",
        action="store_true",
        help="Run quality and latency evaluators sequentially instead of in parallel.",
    )
    iteration_parser.add_argument(
        "--skip-measure",
        action="store_true",
        help="Skip measure if the current iteration artifacts already exist.",
    )
    iteration_parser.add_argument(
        "--timeout-per-subagent-seconds",
        type=int,
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        help="Timeout for each evaluator subprocess pair.",
    )

    manifest_parser = subparsers.add_parser(
        "emit-promotion-manifest",
        help="Emit the Enron promotion manifest from the latest local artifacts.",
    )
    manifest_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    manifest_parser.add_argument(
        "--candidate-label",
        choices=["baseline", "postchange"],
        default="postchange",
        help="Artifact label to package for promotion.",
    )
    manifest_parser.add_argument(
        "--output",
        default=str(DEFAULT_PROMOTION_MANIFEST_PATH),
        help="Output path for the promotion manifest.",
    )

    final_report_parser = subparsers.add_parser(
        "emit-final-report",
        help="Aggregate loop history into data/final_report.json.",
    )
    final_report_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    final_report_parser.add_argument(
        "--output",
        default=str(DEFAULT_FINAL_REPORT_PATH),
        help="Output path for the final report artifact.",
    )
    final_report_parser.add_argument(
        "--termination-reason",
        default=None,
        help="Optional explicit termination reason.",
    )
    final_report_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Optional max-iteration cap for report inference.",
    )

    loop_parser = subparsers.add_parser(
        "orchestrate-loop",
        help="Run the full factual hardening loop until READY, PLATEAU, EXHAUSTED, or CAP.",
    )
    loop_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    loop_parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="Maximum hardening iterations to run.",
    )
    loop_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N benchmark questions.",
    )
    loop_parser.add_argument(
        "--max-concurrent-questions",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_QUESTIONS,
        help="Maximum concurrent answer-generation workers.",
    )
    loop_parser.add_argument(
        "--max-concurrent-judge-calls",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_JUDGE_CALLS,
        help="Maximum concurrent MLflow/judge scoring threads.",
    )
    loop_parser.add_argument(
        "--latency-mode",
        choices=["isolated", "throughput"],
        default="isolated",
        help="Latency evaluator mode.",
    )
    loop_parser.add_argument(
        "--latency-sla-ms",
        type=int,
        default=DEFAULT_LATENCY_SLA_MS,
        help="Latency SLA threshold in milliseconds.",
    )
    loop_parser.add_argument(
        "--implementer-mode",
        choices=["manual", "noop", "command"],
        default="manual",
        help="Implementation stage mode.",
    )
    loop_parser.add_argument(
        "--implementer-command",
        default=None,
        help=(
            "External command template to produce implementation_log.json. "
            "Available placeholders: {iteration}, {artifact_dir}, "
            "{improvement_plan}, {implementation_log}, {loop_state}."
        ),
    )
    loop_parser.add_argument(
        "--implementation-log",
        default=None,
        help="Optional path for the shared implementation log artifact.",
    )
    loop_parser.add_argument(
        "--timeout-per-subagent-seconds",
        type=int,
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        help="Timeout for each evaluator/implementer stage.",
    )
    loop_parser.add_argument(
        "--plateau-threshold",
        type=float,
        default=DEFAULT_PLATEAU_THRESHOLD,
        help="Per-iteration improvement threshold used for plateau detection.",
    )
    loop_parser.add_argument(
        "--plateau-window",
        type=int,
        default=DEFAULT_PLATEAU_WINDOW,
        help="How many trailing iterations to inspect for plateau detection.",
    )
    loop_parser.add_argument(
        "--benchmark-refresh-interval",
        type=int,
        default=None,
        help="Rebuild the benchmark every N iterations. Default: reuse unless missing.",
    )
    loop_parser.add_argument(
        "--serial-subagents",
        action="store_true",
        help="Run quality and latency evaluators sequentially instead of in parallel.",
    )

    archive_parser = subparsers.add_parser(
        "archive-iteration",
        help="Archive current artifacts into data/iterations/iter_N/.",
    )
    archive_parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Artifact directory root.",
    )
    archive_parser.add_argument(
        "--iteration",
        type=int,
        required=True,
        help="Iteration number to archive under.",
    )

    args = parser.parse_args()

    if args.command == "curate-benchmark":
        payload = build_benchmark_definition(args.output, limit=args.limit)
    elif args.command == "evaluate-quality":
        payload = run_quality_evaluation(
            args.benchmark,
            args.output,
            max_concurrent_questions=args.max_concurrent_questions,
            max_concurrent_judge_calls=args.max_concurrent_judge_calls,
            limit=args.limit,
            repro_runs=args.repro_runs,
        )
    elif args.command == "evaluate-latency":
        payload = run_latency_evaluation(
            args.benchmark,
            args.output,
            mode=args.mode,
            max_concurrent_questions=args.max_concurrent_questions,
            limit=args.limit,
            sla_ms=args.sla_ms,
        )
    elif args.command == "analyze-failures":
        payload = analyze_failures(
            args.quality,
            args.latency,
            args.output,
            prior_assessment_path=args.prior_assessment,
        )
    elif args.command == "investigate-root-causes":
        payload = investigate_root_causes(
            args.failure_taxonomy,
            args.quality,
            args.latency,
            args.output,
            max_failure_classes=args.max_failure_classes,
            max_examples_per_class=args.max_examples_per_class,
        )
    elif args.command == "plan-improvements":
        payload = plan_improvements(
            args.root_causes,
            args.failure_taxonomy,
            args.loop_state,
            args.output,
            max_changes=args.max_changes,
        )
    elif args.command == "assess-iteration":
        payload = assess_iteration(
            args.baseline_quality,
            args.postchange_quality,
            args.baseline_latency,
            args.postchange_latency,
            args.failure_taxonomy,
            args.loop_state,
            args.output,
            plateau_threshold=args.plateau_threshold,
            plateau_window=args.plateau_window,
        )
    elif args.command == "orchestrate-measure":
        payload = orchestrate_measure(
            artifact_dir=args.artifact_dir,
            iteration=args.iteration,
            label=args.label,
            limit=args.limit,
            parallel_subagents=not args.serial_subagents,
            max_concurrent_questions=args.max_concurrent_questions,
            max_concurrent_judge_calls=args.max_concurrent_judge_calls,
            latency_mode=args.latency_mode,
            latency_sla_ms=args.latency_sla_ms,
            subagent_timeout_seconds=args.timeout_per_subagent_seconds,
        )
    elif args.command == "orchestrate-analyze":
        payload = orchestrate_analyze(
            artifact_dir=args.artifact_dir,
            quality_label=args.quality_label,
            iteration=args.iteration,
            prior_assessment_path=args.prior_assessment,
        )
    elif args.command == "orchestrate-implement":
        payload = orchestrate_implement(
            artifact_dir=args.artifact_dir,
            iteration=args.iteration,
            implementer_mode=args.implementer_mode,
            implementer_command=args.implementer_command,
            output_path=args.output,
            subagent_timeout_seconds=args.timeout_per_subagent_seconds,
        )
    elif args.command == "orchestrate-assess":
        payload = orchestrate_assess(
            artifact_dir=args.artifact_dir,
            iteration=args.iteration,
            baseline_label=args.baseline_label,
            postchange_label=args.postchange_label,
            plateau_threshold=args.plateau_threshold,
            plateau_window=args.plateau_window,
        )
    elif args.command == "orchestrate-iteration":
        payload = orchestrate_iteration(
            artifact_dir=args.artifact_dir,
            iteration=args.iteration,
            label=args.label,
            limit=args.limit,
            parallel_subagents=not args.serial_subagents,
            max_concurrent_questions=args.max_concurrent_questions,
            max_concurrent_judge_calls=args.max_concurrent_judge_calls,
            latency_mode=args.latency_mode,
            latency_sla_ms=args.latency_sla_ms,
            skip_measure=args.skip_measure,
            subagent_timeout_seconds=args.timeout_per_subagent_seconds,
        )
    elif args.command == "emit-promotion-manifest":
        payload = emit_promotion_manifest(
            artifact_dir=args.artifact_dir,
            candidate_label=args.candidate_label,
            output_path=args.output,
        )
    elif args.command == "emit-final-report":
        payload = emit_final_report(
            artifact_dir=args.artifact_dir,
            output_path=args.output,
            termination_reason=args.termination_reason,
            max_iterations=args.max_iterations,
        )
    elif args.command == "orchestrate-loop":
        payload = orchestrate_loop(
            artifact_dir=args.artifact_dir,
            max_iterations=args.max_iterations,
            limit=args.limit,
            parallel_subagents=not args.serial_subagents,
            max_concurrent_questions=args.max_concurrent_questions,
            max_concurrent_judge_calls=args.max_concurrent_judge_calls,
            latency_mode=args.latency_mode,
            latency_sla_ms=args.latency_sla_ms,
            implementer_mode=args.implementer_mode,
            implementer_command=args.implementer_command,
            implementation_log_path=args.implementation_log,
            subagent_timeout_seconds=args.timeout_per_subagent_seconds,
            plateau_threshold=args.plateau_threshold,
            plateau_window=args.plateau_window,
            benchmark_refresh_interval=args.benchmark_refresh_interval,
        )
    elif args.command == "archive-iteration":
        payload = archive_iteration_artifacts(
            args.iteration,
            artifact_dir=args.artifact_dir,
        )
    else:  # pragma: no cover - argparse enforces subcommands
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
