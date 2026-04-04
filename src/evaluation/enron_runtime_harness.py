from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Semaphore
from typing import Any, Callable

import mlflow
import pandas as pd

from src.evaluation.enron_evaluation import (
    build_enron_scorers,
    get_enron_score_columns,
)
from src.evaluation.question_bank import ENRON_CORE_EVAL_DATA
from src.evaluation.runtime_baselines import load_runtime_baselines


def _extract_nested_mapping_value(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


def _coerce_int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _progress_enabled() -> bool:
    raw = str(os.environ.get("GRAPHRAG_EVAL_PROGRESS", "") or "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _emit_progress(event: str, **payload: Any) -> None:
    if not _progress_enabled():
        return
    print(json.dumps({"event": event, **payload}), flush=True)


def filter_enron_eval_rows(
    *,
    dataset: list[dict] | None = None,
    cases: int | None = None,
    category: str | None = None,
    attorney_category: str | None = None,
    split: str | None = None,
) -> list[dict]:
    rows = list(dataset or ENRON_CORE_EVAL_DATA)
    if category:
        rows = [row for row in rows if row["category"] == category]
    if attorney_category:
        rows = [
            row for row in rows
            if row.get("attorney_category") == attorney_category
        ]
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
                "question_id": row.get("question_id"),
                "corpus": row.get("corpus", "enron"),
                "eval_split": row.get("eval_split"),
                "attorney_category": row.get("attorney_category"),
                "architecture_primary": row.get("architecture_primary"),
                "architecture_secondary": row.get("architecture_secondary", []),
                "domain_primary": row.get("domain_primary"),
                "domain_secondary": row.get("domain_secondary", []),
                "source_type": row.get("source_type"),
                "coverage_policy": row.get("coverage_policy"),
                "suite_tags": row.get("suite_tags", []),
                "expected_entities": row["expected_entities"],
                "graph_ground_truth": row["graph_ground_truth"],
                "historical_ground_truth": row["historical_ground_truth"],
                "evidence_required": row["evidence_required"],
                "category": row.get("attorney_category") or row["category"],
            },
        }
        for row in rows
    ]


def _prepare_results_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    if prepared.empty:
        return prepared

    if "question_id" not in prepared.columns:
        if "expectations/question_id" in prepared.columns:
            prepared["question_id"] = prepared["expectations/question_id"]
        elif "expectations" in prepared.columns:
            prepared["question_id"] = prepared["expectations"].apply(
                lambda payload: _extract_nested_mapping_value(payload, "question_id")
            )

    if "question_text" not in prepared.columns:
        if "inputs/question" in prepared.columns:
            prepared["question_text"] = prepared["inputs/question"]
        elif "inputs" in prepared.columns:
            prepared["question_text"] = prepared["inputs"].apply(
                lambda payload: _extract_nested_mapping_value(payload, "question", "")
            )

    expectation_fields = [
        "corpus",
        "eval_split",
        "attorney_category",
        "architecture_primary",
        "architecture_secondary",
        "domain_primary",
        "domain_secondary",
        "source_type",
        "coverage_policy",
        "suite_tags",
        "expected_entities",
        "category",
    ]
    for field in expectation_fields:
        if field in prepared.columns:
            continue
        flat_column = f"expectations/{field}"
        if flat_column in prepared.columns:
            prepared[field] = prepared[flat_column]
        elif "expectations" in prepared.columns:
            prepared[field] = prepared["expectations"].apply(
                lambda payload, key=field: _extract_nested_mapping_value(payload, key)
            )

    if "category" not in prepared.columns:
        prepared["category"] = prepared.get("attorney_category", pd.Series(["unknown"] * len(prepared)))
    else:
        prepared["category"] = prepared["category"].fillna(prepared.get("attorney_category"))

    score_cols = get_enron_score_columns(prepared)
    if score_cols:
        prepared["avg_score"] = (
            prepared[score_cols]
            .apply(pd.to_numeric, errors="coerce")
            .mean(axis=1)
        )
    elif "avg_score" not in prepared.columns:
        prepared["avg_score"] = pd.NA

    return prepared


def summarize_runtime_eval_rows(
    rows: list[dict[str, Any]],
    *,
    elapsed_s: float,
) -> dict[str, Any]:
    results_df = _prepare_results_frame(pd.DataFrame(rows))
    score_cols = get_enron_score_columns(results_df)
    overall_metrics: dict[str, float] = {}
    overall_score = None
    score_matrix: dict[str, dict[str, float]] = {}
    worst_questions: list[dict[str, Any]] = []

    if score_cols and not results_df.empty:
        overall = results_df[score_cols].mean()
        overall_metrics = {
            col.replace("/value", ""): round(float(overall[col]), 4)
            for col in score_cols
        }
        overall_score = round(float(overall.mean()), 4)

        score_agg = {col: "mean" for col in score_cols}
        summary = results_df.groupby("category").agg(score_agg).round(2)
        summary.columns = [col.replace("/value", "") for col in summary.columns]
        score_matrix = {
            str(index): {
                str(col): round(float(value), 4)
                for col, value in row.items()
            }
            for index, row in summary.to_dict(orient="index").items()
        }

        worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
        for _, row in worst.iterrows():
            worst_questions.append(
                {
                    "category": row.get("category", "unknown"),
                    "question": row.get("question_text", ""),
                    "avg_score": round(float(row["avg_score"]), 4),
                }
            )

    return {
        "score_columns": [col.replace("/value", "") for col in score_cols],
        "overall_metrics": overall_metrics,
        "overall_score": overall_score,
        "score_matrix_by_category": score_matrix,
        "worst_questions": worst_questions,
        "slice_question_count": len(rows),
        "successful_question_count": int(
            sum(1 for row in rows if str(row.get("state", "")).upper() == "OK")
        ),
        "error_question_count": int(
            sum(1 for row in rows if str(row.get("state", "")).upper() != "OK")
        ),
        "elapsed_s": round(float(elapsed_s), 1),
    }


def _normalize_metric_name(name: str) -> str:
    return name[:-2] if name.endswith("_j") else name


def _evaluate_single_row(
    row: dict[str, Any],
    predict_fn: Callable[[str], str],
    scorers: list[Callable[..., Any]],
    judge_semaphore: Semaphore,
) -> dict[str, Any]:
    eval_record = build_runtime_eval_records([row])[0]
    inputs = dict(eval_record["inputs"])
    expectations = dict(eval_record["expectations"])
    question = str(inputs.get("question", "") or "")

    started = time.perf_counter()
    error = ""
    try:
        output = predict_fn(question)
        status = "ok"
    except Exception as exc:
        output = f"ERROR: {exc}"
        status = "error"
        error = str(exc)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)

    result = {
        "question_id": expectations.get("question_id"),
        "question_text": question,
        "corpus": expectations.get("corpus"),
        "eval_split": expectations.get("eval_split"),
        "attorney_category": expectations.get("attorney_category"),
        "architecture_primary": expectations.get("architecture_primary"),
        "architecture_secondary": expectations.get("architecture_secondary", []),
        "domain_primary": expectations.get("domain_primary"),
        "domain_secondary": expectations.get("domain_secondary", []),
        "source_type": expectations.get("source_type"),
        "coverage_policy": expectations.get("coverage_policy"),
        "suite_tags": expectations.get("suite_tags", []),
        "expected_entities": expectations.get("expected_entities", []),
        "category": expectations.get("category") or expectations.get("attorney_category"),
        "inputs/question": question,
        "expectations/question_id": expectations.get("question_id"),
        "expectations/corpus": expectations.get("corpus"),
        "expectations/eval_split": expectations.get("eval_split"),
        "expectations/attorney_category": expectations.get("attorney_category"),
        "expectations/architecture_primary": expectations.get("architecture_primary"),
        "expectations/architecture_secondary": expectations.get("architecture_secondary", []),
        "expectations/domain_primary": expectations.get("domain_primary"),
        "expectations/domain_secondary": expectations.get("domain_secondary", []),
        "expectations/source_type": expectations.get("source_type"),
        "expectations/coverage_policy": expectations.get("coverage_policy"),
        "expectations/suite_tags": expectations.get("suite_tags", []),
        "expectations/expected_entities": expectations.get("expected_entities", []),
        "inputs": inputs,
        "expectations": expectations,
        "response_text": output if isinstance(output, str) else str(output),
        "status": status,
        "state": "OK" if status == "ok" else "ERROR",
        "error": error,
        "latency_ms": latency_ms,
    }

    output_text = result["response_text"]
    for scorer in scorers:
        raw_metric_name = (
            getattr(scorer, "name", None)
            or getattr(scorer, "__name__", None)
            or "metric"
        )
        metric_name = _normalize_metric_name(str(raw_metric_name))
        try:
            with judge_semaphore:
                feedback = scorer(inputs, output_text, expectations)
            result[f"{metric_name}/value"] = float(getattr(feedback, "value", 0.0))
            rationale = getattr(feedback, "rationale", "")
            if rationale:
                result[f"{metric_name}/rationale"] = str(rationale)
        except Exception as exc:  # pragma: no cover - network-bound
            result[f"{metric_name}/value"] = 0.0
            result[f"{metric_name}/rationale"] = f"Scorer failed: {exc}"

    return result


def run_enron_runtime_evaluation(
    predict_fn: Callable[[str], str],
    *,
    cases: int | None = None,
    category: str | None = None,
    attorney_category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "enron_runtime_eval",
    output_json: str | None = None,
    metadata: dict[str, Any] | None = None,
    max_concurrent_questions: int | None = None,
    max_concurrent_judge_calls: int | None = None,
) -> dict[str, Any]:
    rows = filter_enron_eval_rows(
        cases=cases,
        category=category,
        attorney_category=attorney_category,
        split=split,
    )
    if not rows:
        raise ValueError("No evaluation questions matched the requested filters.")

    scorers = build_enron_scorers(judge_model=judge)
    question_workers = max_concurrent_questions or _coerce_int_env(
        "GRAPHRAG_EVAL_MAX_CONCURRENT_QUESTIONS",
        1,
    )
    judge_workers = max_concurrent_judge_calls or _coerce_int_env(
        "GRAPHRAG_EVAL_MAX_CONCURRENT_JUDGE_CALLS",
        1,
    )
    judge_semaphore = Semaphore(max(1, judge_workers))

    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(question_workers, len(rows)))) as pool:
        futures = {
            pool.submit(
                _evaluate_single_row,
                row,
                predict_fn,
                scorers,
                judge_semaphore,
            ): row.get("question_id")
            for row in rows
        }
        ordered_results: dict[str, dict[str, Any]] = {}
        for future in as_completed(futures):
            result = future.result()
            question_id = str(result.get("question_id") or futures[future])
            ordered_results[question_id] = result
            _emit_progress(
                "question_complete",
                id=question_id,
                state=result.get("state"),
                latency_ms=result.get("latency_ms"),
                attorney_category=result.get("attorney_category"),
            )
            _emit_progress(
                "batch_progress",
                completed=len(ordered_results),
                total=len(rows),
                wall_clock_seconds=round(time.time() - started, 1),
            )
    elapsed = time.time() - started
    result_rows = [
        ordered_results[str(row.get("question_id"))]
        for row in rows
        if str(row.get("question_id")) in ordered_results
    ]

    payload = summarize_runtime_eval_rows(result_rows, elapsed_s=elapsed)
    with mlflow.start_run(run_name=run_name) as run:
        payload.update(
            {
                "version": "1.0",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "run_name": run_name,
                "run_id": run.info.run_id,
                "judge_endpoint": judge or "databricks-claude-sonnet-4-6",
                "category": category,
                "attorney_category": attorney_category,
                "split": split,
                "baseline_registry": load_runtime_baselines(),
                "max_concurrent_questions": question_workers,
                "max_concurrent_judge_calls": judge_workers,
                "question_count": payload["slice_question_count"],
                "summary": {
                    "score_columns": payload["score_columns"],
                    "overall_metrics": payload["overall_metrics"],
                    "overall_score": payload["overall_score"],
                    "score_matrix_by_category": payload["score_matrix_by_category"],
                    "worst_questions": payload["worst_questions"],
                },
                "rows": result_rows,
            }
        )
        if metadata:
            payload.update(metadata)

        metric_payload = {
            f"runtime_eval.{name}": value
            for name, value in payload.get("overall_metrics", {}).items()
            if value is not None
        }
        if payload.get("overall_score") is not None:
            metric_payload["runtime_eval.overall_score"] = payload["overall_score"]
        metric_payload["runtime_eval.question_count"] = float(payload["slice_question_count"])
        metric_payload["runtime_eval.elapsed_s"] = float(payload["elapsed_s"])
        if metric_payload:
            mlflow.log_metrics(metric_payload)
        mlflow.log_dict(payload, "runtime_eval_payload.json")

    if output_json:
        Path(output_json).resolve().write_text(json.dumps(payload, indent=2))
    return payload
