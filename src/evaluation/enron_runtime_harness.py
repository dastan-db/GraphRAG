from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import mlflow
import pandas as pd

from src.evaluation.enron_evaluation import (
    build_enron_scorers,
    summarize_enron_eval_results,
)
from src.evaluation.question_bank import ENRON_CORE_EVAL_DATA
from src.evaluation.runtime_baselines import load_runtime_baselines


def filter_enron_eval_rows(
    *,
    dataset: list[dict] | None = None,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
) -> list[dict]:
    rows = list(dataset or ENRON_CORE_EVAL_DATA)
    if category:
        rows = [row for row in rows if row["category"] == category]
    if split:
        rows = [row for row in rows if row.get("eval_split") == split]
    if cases:
        rows = rows[:cases]
    return rows


def build_runtime_eval_records(rows: list[dict]) -> list[dict]:
    return [
        {
            "inputs": {"question": row["question"]},
            "expectations": {
                "expected_entities": row["expected_entities"],
                "graph_ground_truth": row["graph_ground_truth"],
                "historical_ground_truth": row["historical_ground_truth"],
                "evidence_required": row["evidence_required"],
                "category": row["category"],
            },
        }
        for row in rows
    ]


def run_enron_runtime_evaluation(
    predict_fn: Callable[[str], str],
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "enron_runtime_eval",
    output_json: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = filter_enron_eval_rows(
        cases=cases,
        category=category,
        split=split,
    )
    if not rows:
        raise ValueError("No evaluation questions matched the requested filters.")

    eval_df = pd.DataFrame(build_runtime_eval_records(rows))
    scorers = build_enron_scorers(judge_model=judge)

    started = time.time()
    with mlflow.start_run(run_name=run_name):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=predict_fn,
            scorers=scorers,
        )
    elapsed = time.time() - started

    payload = summarize_enron_eval_results(results, eval_df, elapsed)
    payload.update(
        {
            "version": "1.0",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "judge_endpoint": judge or "databricks-claude-sonnet-4-6",
            "category": category,
            "split": split,
            "baseline_registry": load_runtime_baselines(),
        }
    )
    if metadata:
        payload.update(metadata)
    if output_json:
        Path(output_json).resolve().write_text(json.dumps(payload, indent=2))
    return payload
