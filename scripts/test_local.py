"""Test the shared GraphRAG runtime locally without deploying Model Serving."""

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
    """Load key=value pairs from a file into os.environ without overwriting."""
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
os.environ.setdefault("GRAPHRAG_CORPUS", "bible")

sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _ROOT_DIR)

from runtime import RuntimeQuery, SharedRuntimeOrchestrator


def _apply_local_tool_cap():
    # Databricks FM tool calling currently rejects payloads with >32 tools.
    if (
        os.environ.get("GRAPHRAG_LLM_PROVIDER", "").strip().lower() == "databricks"
        and not os.environ.get("GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT")
    ):
        os.environ["GRAPHRAG_MODEL_LOGGING_TOOL_LIMIT"] = "32"


def main():
    parser = argparse.ArgumentParser(description="Test GraphRAG runtime locally")
    parser.add_argument("question", nargs="?", default="Who is Abraham?")
    parser.add_argument("--corpus", choices=["bible", "enron"], default="bible")
    parser.add_argument("--backend", choices=["local", "databricks", "lakebase"], help="Override GRAPHRAG_BACKEND")
    parser.add_argument("--transport", choices=["direct", "endpoint"], help="Override GRAPHRAG_RUNTIME_TRANSPORT")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama", "gateway"], help="Override GRAPHRAG_LLM_PROVIDER")
    parser.add_argument("--tier", default="", help="Optional Enron access tier")
    parser.add_argument("--permitted-books", nargs="*", default=None, help="Optional Bible book allowlist")
    parser.add_argument("--endpoint-name", default="", help="Optional serving endpoint when transport=endpoint")
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.transport:
        os.environ["GRAPHRAG_RUNTIME_TRANSPORT"] = args.transport
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm
    os.environ["GRAPHRAG_CORPUS"] = args.corpus
    _apply_local_tool_cap()

    print(f"Transport: {os.environ.get('GRAPHRAG_RUNTIME_TRANSPORT')}")
    print(f"Backend:   {os.environ.get('GRAPHRAG_BACKEND')}")
    print(f"LLM:       {os.environ.get('GRAPHRAG_LLM_PROVIDER')}")
    print(f"Corpus:    {args.corpus}")
    print(f"Question:  {args.question}")
    print("-" * 60)

    orchestrator = SharedRuntimeOrchestrator()
    started = time.time()
    response = orchestrator.query(
        RuntimeQuery(
            question=args.question,
            corpus=args.corpus,
            user_tier=args.tier,
            permitted_books=args.permitted_books or [],
            endpoint_name=args.endpoint_name,
        )
    )
    elapsed = time.time() - started

    print(response.full_text)
    for tool_call in response.tool_calls:
        print(f"  [tool] {tool_call.name}({json.dumps(tool_call.arguments, sort_keys=True)})")

    metrics = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_s": round(elapsed, 2),
        "tool_call_count": len(response.tool_calls),
        "tool_calls": [tc.name for tc in response.tool_calls],
        "backend": os.environ.get("GRAPHRAG_BACKEND"),
        "llm": os.environ.get("GRAPHRAG_LLM_PROVIDER"),
        "transport": os.environ.get("GRAPHRAG_RUNTIME_TRANSPORT"),
        "corpus": args.corpus,
    }

    print(f"\nANSWER:{json.dumps(response.full_text)}")
    print(f"METRICS:{json.dumps(metrics)}")


if __name__ == "__main__":
    main()
