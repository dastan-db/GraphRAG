"""Parity check: compare local (DuckDB) vs Databricks backend responses.

Runs the same test cases against both backends and flags divergences in
entity recall and tool usage. Catches SQL translation bugs in LocalBackend
(FQN stripping, param syntax rewriting) before deployment.

Usage:
    python scripts/validate_parity.py
    python scripts/validate_parity.py --output data/parity_results.json

Prerequisites:
    - Local DB exported:  python scripts/export_local_data.py
    - Databricks auth configured (DATABRICKS_HOST + DATABRICKS_TOKEN)
    - LLM provider configured in .env.local

Exit codes:
    0 — parity within tolerance
    1 — significant divergences detected
"""
import argparse
import importlib
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")

PARITY_THRESHOLD = 0.80


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
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")


def _run_with_backend(backend: str, question: str):
    """Run the agent with a specific backend and return (text, tool_calls, elapsed).

    Reloads the agent module to pick up the new BACKEND_TYPE.
    """
    os.environ["GRAPHRAG_BACKEND"] = backend

    import src.agent.agent_serving as mod
    importlib.reload(mod)

    from mlflow.types.responses import ResponsesAgentRequest
    from test_cases import extract_answer_text, extract_tool_calls

    agent = mod.GraphRAGAgent()
    request = ResponsesAgentRequest(input=[{"role": "user", "content": question}])

    start = time.time()
    response = agent.predict(request)
    elapsed = time.time() - start

    return extract_answer_text(response), extract_tool_calls(response), elapsed


def _entity_parity(hits_a: list[str], hits_b: list[str], expected: list[str]) -> dict:
    """Compare entity recall between two runs."""
    set_a = set(h.lower() for h in hits_a)
    set_b = set(h.lower() for h in hits_b)
    set_exp = set(e.lower() for e in expected)

    both = set_a & set_b
    local_only = set_a - set_b
    db_only = set_b - set_a

    recall_a = len(set_a & set_exp) / len(set_exp) if set_exp else 1.0
    recall_b = len(set_b & set_exp) / len(set_exp) if set_exp else 1.0
    recall_diff = abs(recall_a - recall_b)

    return {
        "both": sorted(both),
        "local_only": sorted(local_only),
        "databricks_only": sorted(db_only),
        "recall_local": round(recall_a, 2),
        "recall_databricks": round(recall_b, 2),
        "recall_diff": round(recall_diff, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Local-vs-Databricks parity check")
    parser.add_argument("--output", "-o", default="data/parity_results.json", help="JSON output path")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama"], help="Override GRAPHRAG_LLM_PROVIDER")
    args = parser.parse_args()

    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm

    sys.path.insert(0, _SCRIPT_DIR)
    sys.path.insert(0, _ROOT_DIR)

    from test_cases import TEST_CASES, score_response

    llm = os.environ.get("GRAPHRAG_LLM_PROVIDER")

    print("=" * 80)
    print(f"  PARITY CHECK — local vs databricks, llm: {llm}")
    print(f"  {len(TEST_CASES)} test cases, threshold: {PARITY_THRESHOLD:.0%}")
    print("=" * 80)

    results = []
    parity_scores = []

    for i, case in enumerate(TEST_CASES, 1):
        q = case["question"]
        print(f"\n{'─' * 80}")
        print(f"  Q{i} [{case['category']}] {q[:60]}")
        print(f"{'─' * 80}")

        local_text, local_tools, local_time = "", [], 0.0
        db_text, db_tools, db_time = "", [], 0.0

        try:
            print(f"  Running local backend...", end="", flush=True)
            local_text, local_tools, local_time = _run_with_backend("local", q)
            print(f" {local_time:.1f}s")
        except Exception as e:
            print(f" ERROR: {e}")

        try:
            print(f"  Running databricks backend...", end="", flush=True)
            db_text, db_tools, db_time = _run_with_backend("databricks", q)
            print(f" {db_time:.1f}s")
        except Exception as e:
            print(f" ERROR: {e}")

        local_scores = score_response(local_text, case["expected_entities"]) if local_text else {}
        db_scores = score_response(db_text, case["expected_entities"]) if db_text else {}

        parity = _entity_parity(
            local_scores.get("entity_hits", []),
            db_scores.get("entity_hits", []),
            case["expected_entities"],
        )
        parity_scores.append(parity["recall_diff"])

        tool_match = set(local_tools) == set(db_tools)

        result = {
            "question": q[:60],
            "category": case["category"],
            "local": {
                "entity_recall": local_scores.get("entity_recall", 0),
                "citations": local_scores.get("citations", 0),
                "tool_calls": local_tools,
                "latency": round(local_time, 1),
            },
            "databricks": {
                "entity_recall": db_scores.get("entity_recall", 0),
                "citations": db_scores.get("citations", 0),
                "tool_calls": db_tools,
                "latency": round(db_time, 1),
            },
            "parity": parity,
            "tool_parity": tool_match,
        }
        results.append(result)

        print(f"  Entity recall:  local={parity['recall_local']:.0%}  db={parity['recall_databricks']:.0%}  diff={parity['recall_diff']:.0%}")
        if parity["local_only"]:
            print(f"    local-only entities: {parity['local_only']}")
        if parity["databricks_only"]:
            print(f"    db-only entities:    {parity['databricks_only']}")
        print(f"  Tool parity:    {'MATCH' if tool_match else 'MISMATCH'}")
        if not tool_match:
            print(f"    local:  {local_tools}")
            print(f"    db:     {db_tools}")

    print(f"\n{'=' * 80}")
    print("  PARITY SUMMARY")
    print(f"{'=' * 80}")

    avg_diff = sum(parity_scores) / len(parity_scores) if parity_scores else 1.0
    tool_matches = sum(1 for r in results if r["tool_parity"])
    max_diff = max(parity_scores) if parity_scores else 1.0

    parity_ok = all(1.0 - d >= PARITY_THRESHOLD for d in parity_scores)

    print(f"  Avg recall diff:   {avg_diff:.2f}")
    print(f"  Max recall diff:   {max_diff:.2f}")
    print(f"  Tool match rate:   {tool_matches}/{len(results)}")
    print(f"  Parity threshold:  {PARITY_THRESHOLD:.0%}")
    print(f"\n  {'PARITY OK' if parity_ok else 'PARITY FAILED'}")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "llm": llm,
        "parity_threshold": PARITY_THRESHOLD,
        "parity_ok": parity_ok,
        "avg_recall_diff": round(avg_diff, 2),
        "max_recall_diff": round(max_diff, 2),
        "tool_match_rate": f"{tool_matches}/{len(results)}",
        "results": results,
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results written to {args.output}")

    sys.exit(0 if parity_ok else 1)


if __name__ == "__main__":
    main()
