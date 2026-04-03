"""Latency benchmark for the shared GraphRAG runtime."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _PROJECT_ROOT)

_ENV_FILE = os.path.join(_PROJECT_ROOT, ".env.local")
if os.path.isfile(_ENV_FILE):
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("GRAPHRAG_RUNTIME_TRANSPORT", "direct")
os.environ.setdefault("GRAPHRAG_BACKEND", "local")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

from runtime import RuntimeQuery, SharedRuntimeOrchestrator

BENCHMARK_QUESTIONS = [
    {"q": "Who reported to Jeff Skilling?", "expected_pattern": "entity_structure"},
    {"q": "What was Kenneth Lay's title at Enron?", "expected_pattern": "entity_structure"},
    {"q": "What were Dave Delainey's main activities?", "expected_pattern": "entity_explore"},
    {"q": "Who did Jeff Dasovich communicate with most?", "expected_pattern": "entity_explore"},
    {"q": "How are Kenneth Lay and Jeff Skilling connected?", "expected_pattern": "entity_pair"},
    {"q": "Did Andrew Fastow and Dave Delainey communicate directly?", "expected_pattern": "entity_pair"},
    {"q": "What happened at Enron in August 2001?", "expected_pattern": "timeline"},
    {"q": "What was discussed about the California energy crisis?", "expected_pattern": "keyword_search"},
    {"q": "How many emails did Kenneth Lay send?", "expected_pattern": "genie_analytics"},
    {"q": "What led to Enron's collapse?", "expected_pattern": "general"},
]

_TOOL_CALL_PATTERN = re.compile(r"\b([a-z]+(?:_[a-z0-9]+)+)\s*\(")


def _apply_local_tool_cap():
    if (
        os.environ.get("GRAPHRAG_LLM_PROVIDER", "").strip().lower() == "databricks"
        and not os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT")
    ):
        os.environ["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = "32"


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = math.ceil(pct / 100 * len(values)) - 1
    return values[max(0, min(idx, len(values) - 1))]


def _infer_tool_calls(text: str) -> list[str]:
    seen: set[str] = set()
    tool_calls: list[str] = []
    for match in _TOOL_CALL_PATTERN.finditer(text or ""):
        tool_name = match.group(1)
        if tool_name not in seen:
            seen.add(tool_name)
            tool_calls.append(tool_name)
    return tool_calls


def run_benchmark(questions: list[dict], *, corpus: str, verbose: bool = False) -> dict:
    print(f"\n{'=' * 70}")
    print(f"LATENCY BENCHMARK — {len(questions)} questions")
    print(f"  Backend:   {os.environ.get('GRAPHRAG_BACKEND', 'local')}")
    print(f"  LLM:       {os.environ.get('GRAPHRAG_LLM_PROVIDER', 'openai')}")
    print(f"  Transport: {os.environ.get('GRAPHRAG_RUNTIME_TRANSPORT', 'direct')}")
    print(f"  Corpus:    {corpus}")
    print(f"{'=' * 70}\n")

    results = []

    for i, qobj in enumerate(questions, 1):
        question = qobj["q"]
        expected = qobj.get("expected_pattern", "")
        print(f"[{i}/{len(questions)}] {question}")

        started = time.perf_counter()
        try:
            response = SharedRuntimeOrchestrator().query(
                RuntimeQuery(question=question, corpus=corpus)
            )
            answer_text = response.full_text
            tool_calls = [tc.name for tc in response.tool_calls] or _infer_tool_calls(answer_text)
            status = "ok"
        except Exception as exc:
            answer_text = ""
            tool_calls = []
            status = f"error: {exc}"

        elapsed_ms = (time.perf_counter() - started) * 1000
        result = {
            "question": question,
            "expected_pattern": expected,
            "latency_ms": round(elapsed_ms, 1),
            "status": status,
            "tools": tool_calls,
            "tool_count": len(tool_calls),
            "answer_len": len(answer_text),
        }
        results.append(result)

        status_icon = "OK" if status == "ok" else "ERR"
        print(
            f"  [{status_icon}] {elapsed_ms:,.0f}ms | {len(tool_calls)} tools | "
            f"answer={len(answer_text)} chars"
        )
        if verbose and answer_text:
            print(f"  Answer preview: {answer_text[:200]}...")
        print()

    latencies = [row["latency_ms"] for row in results if row["status"] == "ok"]
    summary = {
        "total_questions": len(questions),
        "successful": len(latencies),
        "errors": sum(1 for row in results if row["status"] != "ok"),
        "p50_ms": round(_percentile(latencies, 50), 1),
        "p95_ms": round(_percentile(latencies, 95), 1),
        "p99_ms": round(_percentile(latencies, 99), 1),
        "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "min_ms": round(min(latencies), 1) if latencies else 0.0,
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
    }

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(
        f"  Questions: {summary['total_questions']} "
        f"({summary['successful']} ok, {summary['errors']} errors)"
    )
    print(f"  p50:  {summary['p50_ms']:>8,.1f} ms")
    print(f"  p95:  {summary['p95_ms']:>8,.1f} ms")
    print(f"  p99:  {summary['p99_ms']:>8,.1f} ms")
    print(f"  mean: {summary['mean_ms']:>8,.1f} ms")
    print(f"  min:  {summary['min_ms']:>8,.1f} ms")
    print(f"  max:  {summary['max_ms']:>8,.1f} ms")
    print(f"{'=' * 70}\n")

    by_pattern: dict[str, list[float]] = {}
    for row in results:
        if row["status"] == "ok":
            by_pattern.setdefault(row["expected_pattern"] or "unknown", []).append(row["latency_ms"])

    if by_pattern:
        print("BY PATTERN:")
        for pattern, lats in sorted(by_pattern.items()):
            print(
                f"  {pattern:<20s} n={len(lats)} p50={_percentile(lats, 50):>8,.1f}ms "
                f"min={min(lats):>8,.1f}ms max={max(lats):>8,.1f}ms"
            )
        print()

    return {
        "summary": summary,
        "results": results,
        "by_pattern": by_pattern,
        "config": {
            "backend": os.environ.get("GRAPHRAG_BACKEND", "local"),
            "llm": os.environ.get("GRAPHRAG_LLM_PROVIDER", "openai"),
            "transport": os.environ.get("GRAPHRAG_RUNTIME_TRANSPORT", "direct"),
            "corpus": corpus,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="GraphRAG latency benchmark")
    parser.add_argument("--question", "-q", help="Run a single question instead of the full suite")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print answer previews")
    parser.add_argument("--output", "-o", help="Write JSON results to file")
    parser.add_argument("--repeat", "-r", type=int, default=1, help="Repeat each question N times")
    parser.add_argument("--corpus", choices=["bible", "enron"], default="enron")
    parser.add_argument("--backend", choices=["local", "databricks", "lakebase"], default=None)
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], default=None)
    parser.add_argument("--transport", choices=["direct", "endpoint"], default=None)
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm
    if args.transport:
        os.environ["GRAPHRAG_RUNTIME_TRANSPORT"] = args.transport
    _apply_local_tool_cap()

    if args.question:
        questions = [{"q": args.question, "expected_pattern": "unknown"}]
    else:
        questions = BENCHMARK_QUESTIONS
    if args.repeat > 1:
        questions = questions * args.repeat

    report = run_benchmark(questions, corpus=args.corpus, verbose=args.verbose)
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
