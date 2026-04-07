"""Send a question to a raw LLM — no graph tools, no agent framework.

This is the "no calculator" control: same LLM, same question, but without
access to the knowledge graph. Used to demonstrate that even SOTA models
struggle on graph-dependent questions without structured retrieval.

Usage:
    # Databricks Foundation Model API (default)
    python scripts/test_raw_llm.py "How many entities connect to Moses?"

    # OpenAI
    GRAPHRAG_LLM_PROVIDER=openai OPENAI_MODEL=gpt-4o-mini \
        python scripts/test_raw_llm.py "How many entities connect to Moses?"
"""

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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


_load_env_file(os.path.join(_SCRIPT_DIR, "..", "..", ".env.local"))

SYSTEM_PROMPT = (
    "You are a biblical scholar. Answer the question using ONLY your training knowledge. "
    "You do NOT have access to any database, knowledge graph, or search tools. "
    "When a question specifies particular books (e.g. Genesis, Exodus, Ruth, Matthew, Acts, or any other book of the KJV Bible), "
    "restrict your answer strictly to information found in those books — do not use "
    "knowledge from other biblical books or external sources. "
    "Be as specific and precise as possible — include exact counts, verse references, "
    "and complete lists when asked."
)


def _get_llm(provider: str, endpoint: str):
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        return ChatOpenAI(model=model, temperature=0.0)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return ChatOllama(model=model, temperature=0.0)
    from databricks_langchain import ChatDatabricks
    return ChatDatabricks(endpoint=endpoint, temperature=0.0)


def main():
    parser = argparse.ArgumentParser(description="Test raw LLM without graph tools")
    parser.add_argument("question", nargs="?", default="Who is Abraham?")
    parser.add_argument("--llm", choices=["databricks", "openai", "ollama"],
                        help="Override LLM provider")
    args = parser.parse_args()

    provider = args.llm or os.environ.get("GRAPHRAG_LLM_PROVIDER", "databricks")
    endpoint = os.environ.get("GRAPHRAG_LLM_ENDPOINT",
                              "databricks-meta-llama-3-3-70b-instruct")

    print(f"Provider: {provider}")
    print(f"Endpoint: {endpoint}")
    print(f"Mode:     RAW LLM (no graph tools)")
    print(f"Question: {args.question}")
    print("-" * 60)

    llm = _get_llm(provider, endpoint)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": args.question},
    ]
    response = llm.invoke(messages)
    print(response.content)

    print(f"\nANSWER:{json.dumps(response.content)}")
    usage = getattr(response, "usage_metadata", None) or {}
    metrics = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    print(f"METRICS:{json.dumps(metrics)}")


if __name__ == "__main__":
    main()
