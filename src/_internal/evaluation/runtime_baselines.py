from __future__ import annotations

import json
from pathlib import Path


# Baselines stay under the shipped ``src/evaluation/baselines`` tree.
DEFAULT_BASELINE_PATH = (
    Path(__file__).resolve().parents[2]
    / "evaluation"
    / "baselines"
    / "runtime_eval_baseline.json"
)


DEFAULT_RUNTIME_BASELINES = {
    "version": "1.0",
    "enron": {
        "local_core_eval": {
            "slice": "balanced_test",
            "question_count": 9,
            "judge_endpoint": "databricks-claude-sonnet-4-6",
            "overall_score_min": 0.64,
            "elapsed_s_max": 240.0,
            "score_delta_tolerance": 0.08,
            "notes": "Frozen from the current Enron factual QA slice before modular cutover.",
        },
        "deployed_core_eval": {
            "slice": "balanced_test",
            "question_count": 9,
            "judge_endpoint": "databricks-claude-sonnet-4-6",
            "overall_score_min": 0.58,
            "elapsed_s_max": 320.0,
            "score_delta_tolerance": 0.10,
            "notes": "Deployment parity gate relative to the in-process local runtime.",
        },
        "genie_quantitative_iteration0": {
            "slice": "governed_genie_iteration0",
            "question_count": 6,
            "artifact_path": "src/evaluation/baselines/genie_iteration0_baseline.json",
            "sql_correctness_current": 0.50,
            "benchmark_pass_rate_current": 0.50,
            "failure_rate_current": 0.50,
            "target_sql_correctness": 0.95,
            "target_benchmark_pass_rate": 0.90,
            "target_failure_rate_max": 0.02,
            "target_latency_p95_multiplier_max": 1.10,
            "notes": "Iteration 0 frozen benchmark and scorecard for the governed Enron quantitative/Genie slice.",
        },
    },
    "bible": {
        "local_quality_gate": {
            "entity_recall_min": 0.60,
            "citations_min": 1.0,
            "success_rate_min": 0.80,
            "notes": "Existing local validation suite remains the frozen Bible runtime baseline.",
        },
        "local_databricks_parity": {
            "recall_parity_min": 0.80,
            "tool_match_rate_min": 0.60,
            "notes": "Parity guard between DuckDB local-fast and Databricks-backed local-integration.",
        },
    },
}


def load_runtime_baselines(path: str | Path = DEFAULT_BASELINE_PATH) -> dict:
    baseline_path = Path(path)
    if baseline_path.exists():
        try:
            return json.loads(baseline_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(DEFAULT_RUNTIME_BASELINES))


def write_runtime_baselines(
    path: str | Path = DEFAULT_BASELINE_PATH,
    payload: dict | None = None,
) -> Path:
    baseline_path = Path(path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(payload or DEFAULT_RUNTIME_BASELINES, indent=2)
    )
    return baseline_path
