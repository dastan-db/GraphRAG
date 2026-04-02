"""Adversarial latency verification loop for GraphRAG agent.

Runs a battery of questions across all 7 PDES primitives, measures per-stage
latency, and reports p50/p95 breakdowns.  Designed for local iteration
(DuckDB + gateway/openai) or remote (databricks backend + FMAPI).

Usage:
    # Local fast iteration (gateway LLM + DuckDB)
    GRAPHRAG_BACKEND=local GRAPHRAG_LLM_PROVIDER=gateway \
        python -m src.agent.latency_benchmark

    # Remote data, remote LLMs (no Model Serving deploy)
    GRAPHRAG_BACKEND=databricks GRAPHRAG_LLM_PROVIDER=databricks \
        python -m src.agent.latency_benchmark

    # Single question probe
    python -m src.agent.latency_benchmark --question "Who reported to Jeff Skilling?"
"""
import argparse
import json
import math
import os
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
sys.path.insert(0, _project_root)

env_file = os.path.join(_project_root, ".env.local")
if os.path.isfile(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


BENCHMARK_QUESTIONS = [
    # entity_structure
    {"q": "Who reported to Jeff Skilling?", "expected_pattern": "entity_structure"},
    {"q": "What was Kenneth Lay's title at Enron?", "expected_pattern": "entity_structure"},
    # entity_explore
    {"q": "What were Dave Delainey's main activities?", "expected_pattern": "entity_explore"},
    {"q": "Who did Jeff Dasovich communicate with most?", "expected_pattern": "entity_explore"},
    # entity_pair
    {"q": "How are Kenneth Lay and Jeff Skilling connected?", "expected_pattern": "entity_pair"},
    {"q": "Did Andrew Fastow and Dave Delainey communicate directly?", "expected_pattern": "entity_pair"},
    # timeline
    {"q": "What happened at Enron in August 2001?", "expected_pattern": "timeline"},
    # keyword_search
    {"q": "What was discussed about the California energy crisis?", "expected_pattern": "keyword_search"},
    # genie_analytics
    {"q": "How many emails did Kenneth Lay send?", "expected_pattern": "genie_analytics"},
    # general
    {"q": "What led to Enron's collapse?", "expected_pattern": "general"},
]


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = math.ceil(pct / 100 * len(values)) - 1
    return values[max(0, min(idx, len(values) - 1))]


def run_benchmark(questions: list[dict], verbose: bool = False) -> dict:
    from mlflow.types.responses import ResponsesAgentRequest
    import src.agent.agent_serving as _mod
    from src.agent.agent_serving import AGENT

    print(f"\n{'='*70}")
    print(f"LATENCY BENCHMARK — {len(questions)} questions")
    print(f"  Backend:    {os.environ.get('GRAPHRAG_BACKEND', 'databricks')}")
    print(f"  LLM:        {os.environ.get('GRAPHRAG_LLM_PROVIDER', 'databricks')}")
    print(f"  Corpus:     {_mod.CORPUS}")
    print(f"  Classifier: {_mod.SMALL_LLM_ENDPOINT}")
    print(f"  Planner:    {_mod.PLANNER_ENDPOINT}")
    print(f"  Synthesis:  {_mod.SYNTHESIS_ENDPOINT}")
    print(f"  ReAct:      {_mod.REACT_ENDPOINT}")
    print(f"{'='*70}\n")

    results = []

    for i, qobj in enumerate(questions, 1):
        q = qobj["q"]
        expected = qobj.get("expected_pattern", "")
        print(f"[{i}/{len(questions)}] {q}")

        t0 = time.perf_counter()
        request = ResponsesAgentRequest(
            input=[{"role": "user", "content": q}]
        )

        tool_calls = []
        answer_text = ""
        try:
            response = AGENT.predict(request)
            for item in response.output:
                item_type = getattr(item, "type", "")
                if item_type == "message":
                    for block in getattr(item, "content", []):
                        text = None
                        if isinstance(block, dict) and block.get("text"):
                            text = block["text"]
                        elif hasattr(block, "text"):
                            text = block.text
                        if text:
                            answer_text += text
                elif item_type == "function_call":
                    tool_calls.append(getattr(item, "name", "?"))
            status = "ok"
        except Exception as exc:
            status = f"error: {exc}"
            answer_text = ""

        elapsed_ms = (time.perf_counter() - t0) * 1000

        cache_stats = ""
        if hasattr(_mod._backend, "_hits"):
            hits = _mod._backend._hits
            misses = _mod._backend._misses
            total = hits + misses
            rate = (100 * hits / total) if total > 0 else 0
            cache_stats = f"cache={hits}/{total} ({rate:.0f}%)"

        result = {
            "question": q,
            "expected_pattern": expected,
            "latency_ms": round(elapsed_ms, 1),
            "status": status,
            "tools": tool_calls,
            "tool_count": len(tool_calls),
            "answer_len": len(answer_text),
            "cache_stats": cache_stats,
        }
        results.append(result)

        status_icon = "OK" if status == "ok" else "ERR"
        print(f"  [{status_icon}] {elapsed_ms:,.0f}ms | {len(tool_calls)} tools | "
              f"answer={len(answer_text)} chars | {cache_stats}")

        if verbose and answer_text:
            print(f"  Answer preview: {answer_text[:200]}...")
        print()

    latencies = [r["latency_ms"] for r in results if r["status"] == "ok"]
    error_count = sum(1 for r in results if r["status"] != "ok")

    summary = {
        "total_questions": len(questions),
        "successful": len(latencies),
        "errors": error_count,
        "p50_ms": round(_percentile(latencies, 50), 1),
        "p95_ms": round(_percentile(latencies, 95), 1),
        "p99_ms": round(_percentile(latencies, 99), 1),
        "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "min_ms": round(min(latencies), 1) if latencies else 0,
        "max_ms": round(max(latencies), 1) if latencies else 0,
    }

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Questions: {summary['total_questions']} ({summary['successful']} ok, {summary['errors']} errors)")
    print(f"  p50:  {summary['p50_ms']:>8,.1f} ms")
    print(f"  p95:  {summary['p95_ms']:>8,.1f} ms")
    print(f"  p99:  {summary['p99_ms']:>8,.1f} ms")
    print(f"  mean: {summary['mean_ms']:>8,.1f} ms")
    print(f"  min:  {summary['min_ms']:>8,.1f} ms")
    print(f"  max:  {summary['max_ms']:>8,.1f} ms")
    print(f"{'='*70}\n")

    by_pattern: dict[str, list[float]] = {}
    for r in results:
        if r["status"] == "ok":
            p = r["expected_pattern"] or "unknown"
            by_pattern.setdefault(p, []).append(r["latency_ms"])

    if by_pattern:
        print("BY PATTERN:")
        for pattern, lats in sorted(by_pattern.items()):
            p50 = _percentile(lats, 50)
            print(f"  {pattern:<20s} n={len(lats)} p50={p50:>8,.1f}ms "
                  f"min={min(lats):>8,.1f}ms max={max(lats):>8,.1f}ms")
        print()

    return {"summary": summary, "results": results, "by_pattern": by_pattern}


def main():
    parser = argparse.ArgumentParser(description="GraphRAG latency benchmark")
    parser.add_argument("--question", "-q", help="Run a single question instead of the full suite")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print answer previews")
    parser.add_argument("--output", "-o", help="Write JSON results to file")
    parser.add_argument("--repeat", "-r", type=int, default=1, help="Repeat each question N times")
    args = parser.parse_args()

    if args.question:
        questions = [{"q": args.question, "expected_pattern": "unknown"}]
    else:
        questions = BENCHMARK_QUESTIONS

    if args.repeat > 1:
        questions = questions * args.repeat

    report = run_benchmark(questions, verbose=args.verbose)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
    os._exit(0)
