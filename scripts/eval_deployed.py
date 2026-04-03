"""Evaluate the deployed Enron GraphRAG runtime via endpoint transport."""

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
from runtime.config import RuntimeConfig
from src.evaluation.enron_runtime_harness import run_enron_runtime_evaluation

ENDPOINT_NAME = "graphrag-enron-agent"

os.environ.setdefault("GRAPHRAG_CORPUS", "enron")


def _build_endpoint_orchestrator() -> SharedRuntimeOrchestrator:
    env = dict(os.environ)
    env["GRAPHRAG_RUNTIME_TRANSPORT"] = "endpoint"
    return SharedRuntimeOrchestrator(RuntimeConfig.from_env(env))


def predict_deployed(question: str, endpoint_name: str = ENDPOINT_NAME) -> str:
    orchestrator = _build_endpoint_orchestrator()
    response = orchestrator.query(
        RuntimeQuery(
            question=question,
            corpus="enron",
            endpoint_name=endpoint_name,
        )
    )
    return response.full_text


def run_deployed_evaluation(
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "deployed_eval",
    endpoint_name: str = ENDPOINT_NAME,
    output_json: str | None = None,
) -> dict:
    return run_enron_runtime_evaluation(
        lambda question: predict_deployed(question, endpoint_name=endpoint_name),
        cases=cases,
        category=category,
        split=split,
        judge=judge,
        run_name=run_name,
        output_json=output_json,
        metadata={
            "endpoint_name": endpoint_name,
            "corpus": "enron",
            "runtime_transport": "endpoint",
        },
    )


def main():
    parser = argparse.ArgumentParser(description="Evaluate deployed Enron GraphRAG agent")
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument(
        "--split",
        type=str,
        default=None,
        choices=["train", "test", "holdout"],
        help="Filter by eval split",
    )
    parser.add_argument("--judge", type=str, default=None, help="Judge endpoint name")
    parser.add_argument("--run-name", type=str, default="deployed_eval", help="MLflow run name")
    parser.add_argument("--endpoint-name", type=str, default=ENDPOINT_NAME, help="Serving endpoint name")
    parser.add_argument("--output-json", type=str, default=None, help="Optional JSON summary path")
    args = parser.parse_args()

    payload = run_deployed_evaluation(
        cases=args.cases,
        category=args.category,
        split=args.split,
        judge=args.judge,
        run_name=args.run_name,
        endpoint_name=args.endpoint_name,
        output_json=args.output_json,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
