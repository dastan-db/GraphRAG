"""Governed raw-model baseline runner for side-by-side comparisons.

This reuses the Enron runtime harness but swaps the GraphRAG agent out for a
plain chat model with no graph tools or local-corpus access.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from src.evaluation.enron_runtime_harness import run_enron_runtime_evaluation


_BIBLE_RAW_PROMPT = (
    "You are a biblical scholar. Answer the question using ONLY your training knowledge. "
    "You do NOT have access to any database, knowledge graph, or search tools. "
    "When a question specifies particular books, restrict your answer strictly to those books. "
    "Be as specific and precise as possible."
)

_ENRON_RAW_PROMPT = (
    "You are answering questions about Enron using ONLY your general model knowledge. "
    "You do NOT have access to the user's local Enron email corpus, knowledge graph, or search tools. "
    "Never claim that you inspected local emails, graph paths, message IDs, exact corpus counts, or "
    "other corpus-specific evidence. If a question depends on local-corpus evidence, say you cannot "
    "verify those corpus-specific details and answer only with broadly known public context when helpful."
)


def _resolve_provider() -> str:
    return os.environ.get("GRAPHRAG_LLM_PROVIDER", "databricks").strip().lower() or "databricks"


def _resolve_corpus() -> str:
    return os.environ.get("GRAPHRAG_CORPUS", "enron").strip().lower() or "enron"


def _resolve_endpoint() -> str:
    return os.environ.get("GRAPHRAG_LLM_ENDPOINT", "databricks-gpt-5-4-nano")


def _system_prompt_for_corpus(corpus: str) -> str:
    if corpus == "enron":
        return _ENRON_RAW_PROMPT
    return _BIBLE_RAW_PROMPT


def _build_llm(provider: str, endpoint: str):
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        return ChatOpenAI(model=model, temperature=0.0)
    if provider == "gateway":
        from langchain_openai import ChatOpenAI

        model = os.environ.get("GATEWAY_MODEL", "gpt-4o-mini")
        base_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
        return ChatOpenAI(base_url=base_url, model=model, temperature=0.0)
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return ChatOllama(model=model, temperature=0.0)

    from databricks_langchain import ChatDatabricks

    return ChatDatabricks(endpoint=endpoint, temperature=0.0)


def _response_text(response) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def build_predict_fn(*, provider: str, endpoint: str, corpus: str) -> Callable[[str], str]:
    llm = _build_llm(provider, endpoint)
    system_prompt = _system_prompt_for_corpus(corpus)

    def predict_fn(question: str) -> str:
        response = llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=question),
            ]
        )
        return _response_text(response)

    return predict_fn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a governed raw-model baseline evaluation")
    parser.add_argument("--provider", choices=["databricks", "openai", "gateway", "ollama"], default=None)
    parser.add_argument("--endpoint", default=None, help="Databricks serving endpoint when provider=databricks")
    parser.add_argument("--corpus", choices=["enron", "bible"], default=None)
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--attorney-category", type=str, default=None, help="Filter by attorney_category")
    parser.add_argument("--split", choices=["train", "test", "holdout"], default=None)
    parser.add_argument("--judge", type=str, default=None, help="Optional judge endpoint override")
    parser.add_argument("--run-name", type=str, default="gpt54_side_by_side_raw")
    parser.add_argument("--output-json", type=str, default=None)
    parser.add_argument("--max-concurrent-questions", type=int, default=None)
    parser.add_argument("--max-concurrent-judge-calls", type=int, default=None)
    args = parser.parse_args()

    if args.provider:
        os.environ["GRAPHRAG_LLM_PROVIDER"] = args.provider
    if args.endpoint:
        os.environ["GRAPHRAG_LLM_ENDPOINT"] = args.endpoint
    if args.corpus:
        os.environ["GRAPHRAG_CORPUS"] = args.corpus

    provider = _resolve_provider()
    corpus = _resolve_corpus()
    endpoint = _resolve_endpoint()
    predict_fn = build_predict_fn(provider=provider, endpoint=endpoint, corpus=corpus)

    payload = run_enron_runtime_evaluation(
        predict_fn,
        cases=args.cases,
        category=args.category,
        attorney_category=args.attorney_category,
        split=args.split,
        judge=args.judge,
        run_name=args.run_name,
        output_json=args.output_json,
        metadata={
            "comparison_mode": "raw_model_baseline",
            "raw_model_provider": provider,
            "raw_model_endpoint": endpoint,
            "raw_model_corpus": corpus,
            "raw_model_prompt_mode": corpus,
        },
        max_concurrent_questions=args.max_concurrent_questions,
        max_concurrent_judge_calls=args.max_concurrent_judge_calls,
    )
    print(
        json.dumps(
            {
                "overall_score": payload.get("overall_score"),
                "overall_metrics": payload.get("overall_metrics"),
                "slice_question_count": payload.get("slice_question_count"),
                "elapsed_s": payload.get("elapsed_s"),
                "worst_questions": payload.get("worst_questions"),
                "provider": provider,
                "endpoint": endpoint,
                "corpus": corpus,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
