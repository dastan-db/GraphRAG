"""Local quality gate for the shared GraphRAG runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")
_SRC_DIR = os.path.join(_ROOT_DIR, "src")


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
os.environ.setdefault("GRAPHRAG_BACKEND", "local")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

sys.path.insert(0, _SCRIPT_DIR)
sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _ROOT_DIR)

from runtime import RuntimeQuery, SharedRuntimeOrchestrator
from test_cases import (
    ENRON_TEST_CASES,
    TEST_CASES,
    check_enron_quality_gates,
    check_quality_gates,
    score_enron_response,
    score_response,
)


def _apply_local_tool_cap():
    if (
        os.environ.get("GRAPHRAG_LLM_PROVIDER", "").strip().lower() == "databricks"
        and not os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT")
    ):
        os.environ["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = "32"


def _run_runtime(corpus: str, question: str):
    start = time.time()
    response = SharedRuntimeOrchestrator().query(
        RuntimeQuery(question=question, corpus=corpus)
    )
    elapsed = time.time() - start
    tool_calls = [tc.name for tc in response.tool_calls]
    return response.full_text, tool_calls, elapsed


def _run_bible_suite(verbose: bool) -> tuple[list[dict], tuple[bool, list[dict]]]:
    results = []
    for i, case in enumerate(TEST_CASES, 1):
        question = case["question"]
        print(f"\n{'─' * 80}")
        print(f"  Bible Q{i} [{case['category']}]")
        print(f"  {question}")
        print(f"{'─' * 80}")
        try:
            text, tool_calls, elapsed = _run_runtime("bible", question)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            results.append({"question": question[:60], "category": case["category"], "status": "ERROR", "error": str(exc)})
            continue

        scores = score_response(text, case["expected_entities"])
        scores["question"] = question[:60]
        scores["category"] = case["category"]
        scores["latency"] = round(elapsed, 1)
        scores["tool_calls"] = tool_calls
        results.append(scores)

        print(f"  Latency:         {elapsed:.1f}s")
        print(f"  Entity recall:   {scores['entity_recall']:.0%}  hits={scores['entity_hits']}")
        print(f"  Citations:       {scores['citations']}")
        print(f"  Tools used:      {tool_calls}")
        if verbose:
            print(f"\n  --- Response ---\n  {text[:800].replace(chr(10), chr(10) + '  ')}")

    return results, check_quality_gates(results)


def _run_enron_suite(verbose: bool) -> tuple[list[dict], tuple[bool, list[dict]]]:
    results = []
    for i, case in enumerate(ENRON_TEST_CASES, 1):
        question = case["question"]
        print(f"\n{'─' * 80}")
        print(f"  Enron Q{i} [{case['category']}]")
        print(f"  {question}")
        print(f"{'─' * 80}")
        try:
            text, tool_calls, elapsed = _run_runtime("enron", question)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            results.append({"question": question[:60], "category": case["category"], "status": "ERROR", "error": str(exc)})
            continue

        scores = score_enron_response(text, tool_calls, case)
        scores["latency"] = round(elapsed, 1)
        results.append(scores)

        print(f"  Latency:               {elapsed:.1f}s")
        print(f"  Expected tool hit:     {scores['expected_tool_hit']:.0%} ({scores['expected_tool']})")
        if scores["forbidden_tool"]:
            print(f"  Forbidden tool avoided:{scores['forbidden_tool_avoided']:.0%} ({scores['forbidden_tool']})")
        print(f"  Tools used:            {tool_calls}")
        if verbose:
            print(f"\n  --- Response ---\n  {text[:800].replace(chr(10), chr(10) + '  ')}")

    return results, check_enron_quality_gates(results)


def main():
    parser = argparse.ArgumentParser(description="Local quality gate for GraphRAG")
    parser.add_argument("--backend", choices=["local", "databricks", "lakebase"], help="Override GRAPHRAG_BACKEND")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], help="Override GRAPHRAG_LLM_PROVIDER")
    parser.add_argument("--corpus", choices=["bible", "enron", "both"], default="both", help="Which validation suites to run")
    parser.add_argument("--output", "-o", default="data/validation_results.json", help="JSON output path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full response text")
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm
    _apply_local_tool_cap()

    backend = os.environ.get("GRAPHRAG_BACKEND")
    llm = os.environ.get("GRAPHRAG_LLM_PROVIDER")

    print("=" * 80)
    print(f"  LOCAL VALIDATION — backend: {backend}, llm: {llm}, corpus: {args.corpus}")
    print("=" * 80)

    suite_results: dict[str, dict] = {}
    all_passed = True

    if args.corpus in {"bible", "both"}:
        results, gate_tuple = _run_bible_suite(args.verbose)
        passed, gates = gate_tuple
        suite_results["bible"] = {"passed": passed, "gates": gates, "results": results}
        all_passed = all_passed and passed

    if args.corpus in {"enron", "both"}:
        results, gate_tuple = _run_enron_suite(args.verbose)
        passed, gates = gate_tuple
        suite_results["enron"] = {"passed": passed, "gates": gates, "results": results}
        all_passed = all_passed and passed

    print(f"\n{'=' * 80}")
    print("  QUALITY GATES")
    print(f"{'=' * 80}")
    for corpus, payload in suite_results.items():
        print(f"  {corpus.upper()}")
        for gate in payload["gates"]:
            status = "PASS" if gate["passed"] else "FAIL"
            print(f"    [{status}] {gate['label']}: {gate['value']:.2f} (threshold: {gate['threshold']})")

    print(f"\n  {'ALL GATES PASSED' if all_passed else 'SOME GATES FAILED'}")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend": backend,
        "llm": llm,
        "runtime_transport": os.environ.get("GRAPHRAG_RUNTIME_TRANSPORT", "direct"),
        "gates_passed": all_passed,
        "suites": suite_results,
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results written to {args.output}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
