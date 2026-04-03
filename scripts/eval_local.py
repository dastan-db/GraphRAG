"""Local evaluation harness for the shared Enron GraphRAG runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")
_SRC_DIR = os.path.join(_ROOT_DIR, "src")
sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _ROOT_DIR)

from runtime import RuntimeQuery, SharedRuntimeOrchestrator
from src.evaluation.enron_runtime_harness import run_enron_runtime_evaluation

os.environ.setdefault("GRAPHRAG_RUNTIME_TRANSPORT", "direct")
os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")


def _apply_local_tool_cap():
    if (
        os.environ.get("GRAPHRAG_LLM_PROVIDER", "").strip().lower() == "databricks"
        and not os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT")
    ):
        os.environ["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = "32"


def predict_fn(question: str) -> str:
    response = SharedRuntimeOrchestrator().query(
        RuntimeQuery(question=question, corpus="enron")
    )
    return response.full_text


def run_local_evaluation(
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "local_eval",
    output_json: str | None = None,
) -> dict:
    return run_enron_runtime_evaluation(
        predict_fn,
        cases=cases,
        category=category,
        split=split,
        judge=judge,
        run_name=run_name,
        output_json=output_json,
        metadata={
            "backend": os.environ.get("GRAPHRAG_BACKEND", "lakebase"),
            "corpus": os.environ.get("GRAPHRAG_CORPUS", "enron"),
            "runtime_transport": os.environ.get("GRAPHRAG_RUNTIME_TRANSPORT", "direct"),
        },
    )


def main():
    parser = argparse.ArgumentParser(description="Local Enron GraphRAG evaluation")
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--backend", choices=["local", "databricks", "lakebase"], default=None, help="Override GRAPHRAG_BACKEND")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], default=None, help="Override GRAPHRAG_LLM_PROVIDER")
    parser.add_argument(
        "--split",
        type=str,
        default=None,
        choices=["train", "test", "holdout"],
        help="Filter by eval split",
    )
    parser.add_argument("--judge", type=str, default=None, help="Judge endpoint name")
    parser.add_argument("--run-name", type=str, default="local_eval", help="MLflow run name")
    parser.add_argument("--output-json", type=str, default=None, help="Optional JSON summary path")
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm
    _apply_local_tool_cap()

    payload = run_local_evaluation(
        cases=args.cases,
        category=args.category,
        split=args.split,
        judge=args.judge,
        run_name=args.run_name,
        output_json=args.output_json,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
