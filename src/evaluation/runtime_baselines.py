from __future__ import annotations

import json
from pathlib import Path


DEFAULT_BASELINE_PATH = (
    Path(__file__).resolve().parent / "baselines" / "runtime_eval_baseline.json"
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
