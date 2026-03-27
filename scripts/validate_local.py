"""Local quality gate for GraphRAG agent.

Runs the agent locally against a curated test suite and enforces quality
thresholds. Must pass before deploying to Databricks Model Serving.

Usage:
    # Default: local backend + OpenAI
    python scripts/validate_local.py

    # Against Databricks backend (no deploy)
    python scripts/validate_local.py --backend databricks --llm databricks

    # Custom output path
    python scripts/validate_local.py --output data/my_results.json

Exit codes:
    0 — all quality gates passed
    1 — one or more gates failed
"""
import argparse
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")


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
os.environ.setdefault("GRAPHRAG_BACKEND", "local")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")


def _run_agent(agent, question: str) -> tuple:
    """Run agent.predict and return (response_obj, answer_text, tool_calls, elapsed)."""
    from mlflow.types.responses import ResponsesAgentRequest
    from test_cases import extract_answer_text, extract_tool_calls

    request = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
    start = time.time()
    response = agent.predict(request)
    elapsed = time.time() - start
    text = extract_answer_text(response)
    tools = extract_tool_calls(response)
    return response, text, tools, elapsed


def main():
    parser = argparse.ArgumentParser(description="Local quality gate for GraphRAG agent")
    parser.add_argument("--backend", choices=["local", "databricks"], help="Override GRAPHRAG_BACKEND")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], help="Override GRAPHRAG_LLM_PROVIDER")
    parser.add_argument("--output", "-o", default="data/validation_results.json", help="JSON output path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full response text")
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm

    sys.path.insert(0, _SCRIPT_DIR)
    sys.path.insert(0, _ROOT_DIR)

    from test_cases import TEST_CASES, score_response, check_quality_gates
    from src.agent.agent_serving import AGENT

    backend = os.environ.get("GRAPHRAG_BACKEND")
    llm = os.environ.get("GRAPHRAG_LLM_PROVIDER")

    print("=" * 80)
    print(f"  LOCAL VALIDATION — backend: {backend}, llm: {llm}")
    print(f"  {len(TEST_CASES)} test cases")
    print("=" * 80)

    results = []

    for i, case in enumerate(TEST_CASES, 1):
        q = case["question"]
        print(f"\n{'─' * 80}")
        print(f"  Q{i} [{case['category']}]")
        print(f"  {q}")
        print(f"{'─' * 80}")

        try:
            _resp, text, tool_calls, elapsed = _run_agent(AGENT, q)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"question": q[:60], "category": case["category"], "status": "ERROR", "error": str(e)})
            continue

        scores = score_response(text, case["expected_entities"])
        scores["question"] = q[:60]
        scores["category"] = case["category"]
        scores["latency"] = round(elapsed, 1)
        scores["tool_calls"] = tool_calls
        scores["backend"] = backend
        scores["llm"] = llm
        results.append(scores)

        print(f"  Latency:         {elapsed:.1f}s")
        print(f"  Entity recall:   {scores['entity_recall']:.0%}  hits={scores['entity_hits']}")
        if scores["entity_misses"]:
            print(f"                   misses={scores['entity_misses']}")
        print(f"  Citations:       {scores['citations']}")
        print(f"  Tools used:      {tool_calls}")

        if args.verbose:
            print(f"\n  --- Response ---")
            print(f"  {text[:800].replace(chr(10), chr(10) + '  ')}")
            if len(text) > 800:
                print(f"  ... ({len(text) - 800} more chars)")

    print(f"\n{'=' * 80}")
    print("  QUALITY GATES")
    print(f"{'=' * 80}")

    all_passed, gates = check_quality_gates(results)

    for g in gates:
        status = "PASS" if g["passed"] else "FAIL"
        print(f"  [{status}] {g['label']}: {g['value']:.2f} (threshold: {g['threshold']})")

    print(f"\n  {'ALL GATES PASSED' if all_passed else 'SOME GATES FAILED'}")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend": backend,
        "llm": llm,
        "gates_passed": all_passed,
        "gates": gates,
        "results": results,
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results written to {args.output}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
