"""Test GraphRAG agent locally without deploying to Model Serving.

Usage:
    # Fully local (DuckDB + OpenAI) — fastest iteration loop
    GRAPHRAG_BACKEND=local GRAPHRAG_LLM_PROVIDER=openai \
        python scripts/test_local.py "Who is Abraham?"

    # Remote data + remote LLM, no Model Serving deploy
    python scripts/test_local.py "Who is Abraham?"

Prerequisites:
    pip install -e ".[local]"
    python scripts/export_local_data.py   # for GRAPHRAG_BACKEND=local
"""
import argparse
import json
import os
import sys


def _load_env_file(path: str):
    """Load key=value pairs from a file into os.environ (setdefault, no overwrite)."""
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(os.path.dirname(__file__), "..", ".env.local"))
os.environ.setdefault("GRAPHRAG_BACKEND", "local")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")


def main():
    parser = argparse.ArgumentParser(description="Test GraphRAG agent locally")
    parser.add_argument("question", nargs="?", default="Who is Abraham?")
    parser.add_argument(
        "--backend",
        choices=["local", "databricks"],
        help="Override GRAPHRAG_BACKEND",
    )
    parser.add_argument(
        "--llm",
        choices=["databricks", "openai", "ollama", "gateway"],
        help="Override GRAPHRAG_LLM_PROVIDER",
    )
    args = parser.parse_args()

    if args.backend:
        os.environ["GRAPHRAG_BACKEND"] = args.backend
    if args.llm:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.llm

    print(f"Backend:  {os.environ.get('GRAPHRAG_BACKEND')}")
    print(f"LLM:      {os.environ.get('GRAPHRAG_LLM_PROVIDER')}")
    print(f"Question: {args.question}")
    print("-" * 60)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    _token_counter = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _wrap_invoke(original_invoke):
        """Return a wrapper that accumulates token usage from usage_metadata."""
        def _tracking_invoke(*a, **kw):
            resp = original_invoke(*a, **kw)
            usage = getattr(resp, "usage_metadata", None) or {}
            _token_counter["input_tokens"] += usage.get("input_tokens", 0)
            _token_counter["output_tokens"] += usage.get("output_tokens", 0)
            _token_counter["total_tokens"] += usage.get("total_tokens", 0)
            return resp
        return _tracking_invoke

    import src.agent.agent_serving as _mod
    from src.agent.agent_serving import AGENT
    from mlflow.types.responses import ResponsesAgentRequest

    _orig_cls_invoke = type(AGENT.llm).invoke
    _llm_class = type(AGENT.llm)

    def _patched_cls_invoke(self, *a, **kw):
        resp = _orig_cls_invoke(self, *a, **kw)
        usage = getattr(resp, "usage_metadata", None) or {}
        _token_counter["input_tokens"] += usage.get("input_tokens", 0)
        _token_counter["output_tokens"] += usage.get("output_tokens", 0)
        _token_counter["total_tokens"] += usage.get("total_tokens", 0)
        return resp

    _llm_class.invoke = _patched_cls_invoke

    _original_get_llm = _mod._get_llm

    def _instrumented_get_llm(**kwargs):
        llm = _original_get_llm(**kwargs)
        cls = type(llm)
        if cls is not _llm_class:
            orig = cls.invoke
            def _other_invoke(self, *a, **kw):
                resp = orig(self, *a, **kw)
                usage = getattr(resp, "usage_metadata", None) or {}
                _token_counter["input_tokens"] += usage.get("input_tokens", 0)
                _token_counter["output_tokens"] += usage.get("output_tokens", 0)
                _token_counter["total_tokens"] += usage.get("total_tokens", 0)
                return resp
            cls.invoke = _other_invoke
        return llm

    _mod._get_llm = _instrumented_get_llm

    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": args.question}]
    )
    response = AGENT.predict(request)

    answer_parts = []
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
                    print(text)
                    answer_parts.append(text)
        elif item_type == "function_call":
            print(f"  [tool] {getattr(item, 'name', '?')}({getattr(item, 'arguments', '')})")
        elif item_type == "function_call_output":
            print(f"  [result] {getattr(item, 'output', '')[:200]}")
        elif hasattr(item, "text"):
            print(item.text)

    print(f"\nANSWER:{json.dumps(chr(10).join(answer_parts))}")
    print(f"METRICS:{json.dumps(_token_counter)}")


if __name__ == "__main__":
    main()
