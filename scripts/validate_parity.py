"""Parity check for the shared GraphRAG runtime across local and Databricks backends."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")
_SRC_DIR = os.path.join(_ROOT_DIR, "src")

PARITY_THRESHOLD = 0.80
ENRON_PARITY_THRESHOLD = 0.67


def _load_env_file(path: str):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(_ROOT_DIR, ".env.local"))
os.environ.setdefault("GRAPHRAG_RUNTIME_TRANSPORT", "direct")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

sys.path.insert(0, _SCRIPT_DIR)
sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _ROOT_DIR)

from runtime import RuntimeQuery, SharedRuntimeOrchestrator
from test_cases import ENRON_TEST_CASES, TEST_CASES, score_enron_response, score_response


def _apply_local_tool_cap():
    if (
        os.environ.get("GRAPHRAG_LLM_PROVIDER", "").strip().lower() == "databricks"
        and not os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT")
    ):
        os.environ["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = "32"


def _run_with_backend(backend: str, corpus: str, question: str):
    os.environ["GRAPHRAG_BACKEND"] = backend
    orchestrator = SharedRuntimeOrchestrator()
    start = time.time()
    response = orchestrator.query(RuntimeQuery(question=question, corpus=corpus))
    elapsed = time.time() - start
    return response.full_text, [tc.name for tc in response.tool_calls], elapsed


def _safe_run_with_backend(backend: str, corpus: str, question: str):
    try:
        text, tools, elapsed = _run_with_backend(backend, corpus, question)
        return text, tools, elapsed, ""
    except Exception as exc:
        return "", [], 0.0, str(exc)


def _entity_parity(hits_a: list[str], hits_b: list[str], expected: list[str]) -> dict:
    set_a = set(hit.lower() for hit in hits_a)
    set_b = set(hit.lower() for hit in hits_b)
    set_expected = set(exp.lower() for exp in expected)
    recall_a = len(set_a & set_expected) / len(set_expected) if set_expected else 1.0
    recall_b = len(set_b & set_expected) / len(set_expected) if set_expected else 1.0
    return {
        "both": sorted(set_a & set_b),
        "local_only": sorted(set_a - set_b),
        "databricks_only": sorted(set_b - set_a),
        "recall_local": round(recall_a, 2),
        "recall_databricks": round(recall_b, 2),
        "recall_diff": round(abs(recall_a - recall_b), 2),
    }


def _run_bible_parity() -> dict:
    results = []
    parity_scores = []
    for case in TEST_CASES:
        question = case["question"]
        local_text, local_tools, local_time, local_error = _safe_run_with_backend("local", "bible", question)
        db_text, db_tools, db_time, db_error = _safe_run_with_backend("databricks", "bible", question)
        local_scores = score_response(local_text, case["expected_entities"])
        db_scores = score_response(db_text, case["expected_entities"])
        parity = _entity_parity(
            local_scores.get("entity_hits", []),
            db_scores.get("entity_hits", []),
            case["expected_entities"],
        )
        tool_match = set(local_tools) == set(db_tools)
        parity_scores.append(1.0 - parity["recall_diff"])
        results.append(
            {
                "question": question[:60],
                "category": case["category"],
                "local": {
                    "entity_recall": local_scores.get("entity_recall", 0.0),
                    "citations": local_scores.get("citations", 0),
                    "tool_calls": local_tools,
                    "latency": round(local_time, 1),
                    "error": local_error,
                },
                "databricks": {
                    "entity_recall": db_scores.get("entity_recall", 0.0),
                    "citations": db_scores.get("citations", 0),
                    "tool_calls": db_tools,
                    "latency": round(db_time, 1),
                    "error": db_error,
                },
                "parity": parity,
                "tool_parity": tool_match,
            }
        )

    return {
        "threshold": PARITY_THRESHOLD,
        "parity_ok": all(score >= PARITY_THRESHOLD for score in parity_scores),
        "results": results,
        "avg_score": round(sum(parity_scores) / len(parity_scores), 2) if parity_scores else 0.0,
    }


def _run_enron_parity() -> dict:
    results = []
    parity_scores = []
    for case in ENRON_TEST_CASES:
        question = case["question"]
        local_text, local_tools, local_time, local_error = _safe_run_with_backend("local", "enron", question)
        db_text, db_tools, db_time, db_error = _safe_run_with_backend("databricks", "enron", question)
        local_scores = score_enron_response(local_text, local_tools, case)
        db_scores = score_enron_response(db_text, db_tools, case)

        expected_agreement = float(local_scores["expected_tool_hit"] == db_scores["expected_tool_hit"])
        forbidden_agreement = float(local_scores["forbidden_tool_avoided"] == db_scores["forbidden_tool_avoided"])
        tool_match = float(set(local_tools) == set(db_tools))
        parity_score = round((expected_agreement + forbidden_agreement + tool_match) / 3, 2)
        parity_scores.append(parity_score)

        results.append(
            {
                "question": question[:60],
                "category": case["category"],
                "local": {
                    "expected_tool_hit": local_scores["expected_tool_hit"],
                    "forbidden_tool_avoided": local_scores["forbidden_tool_avoided"],
                    "tool_calls": local_tools,
                    "latency": round(local_time, 1),
                    "error": local_error,
                },
                "databricks": {
                    "expected_tool_hit": db_scores["expected_tool_hit"],
                    "forbidden_tool_avoided": db_scores["forbidden_tool_avoided"],
                    "tool_calls": db_tools,
                    "latency": round(db_time, 1),
                    "error": db_error,
                },
                "parity_score": parity_score,
            }
        )

    return {
        "threshold": ENRON_PARITY_THRESHOLD,
        "parity_ok": all(score >= ENRON_PARITY_THRESHOLD for score in parity_scores),
        "results": results,
        "avg_score": round(sum(parity_scores) / len(parity_scores), 2) if parity_scores else 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description="Local-vs-Databricks parity check")
    parser.add_argument("--output", "-o", default="data/parity_results.json", help="JSON output path")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], help="Override GRAPHRAG_LLM_PROVIDER")
    args = parser.parse_args()

    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm
    _apply_local_tool_cap()

    llm = os.environ.get("GRAPHRAG_LLM_PROVIDER")

    print("=" * 80)
    print(f"  PARITY CHECK — local vs databricks, llm: {llm}")
    print(f"  Bible threshold: {PARITY_THRESHOLD:.0%} | Enron threshold: {ENRON_PARITY_THRESHOLD:.0%}")
    print("=" * 80)

    bible_payload = _run_bible_parity()
    enron_payload = _run_enron_parity()
    parity_ok = bible_payload["parity_ok"] and enron_payload["parity_ok"]

    print(f"\n{'=' * 80}")
    print("  PARITY SUMMARY")
    print(f"{'=' * 80}")
    print(f"  Bible avg parity: {bible_payload['avg_score']:.2f}")
    print(f"  Enron avg parity: {enron_payload['avg_score']:.2f}")
    print(f"\n  {'PARITY OK' if parity_ok else 'PARITY FAILED'}")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "llm": llm,
        "runtime_transport": os.environ.get("GRAPHRAG_RUNTIME_TRANSPORT", "direct"),
        "parity_ok": parity_ok,
        "bible": bible_payload,
        "enron": enron_payload,
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results written to {args.output}")

    sys.exit(0 if parity_ok else 1)


if __name__ == "__main__":
    main()
