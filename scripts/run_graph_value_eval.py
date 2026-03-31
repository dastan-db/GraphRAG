"""Run graph-value eval suite against a specific model.

Usage:
    python scripts/run_graph_value_eval.py --model 8b
    python scripts/run_graph_value_eval.py --model 70b --no-graph
    python scripts/run_graph_value_eval.py --model 8b --scoped
    python scripts/run_graph_value_eval.py --model 70b --scoped --no-graph
    python scripts/run_graph_value_eval.py --model 8b --cases mh-01 dis-01 adv-04
"""
import argparse
import json
import os
import sys
import time

def _load_env_file(path):
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
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "databricks")

MODELS = {
    "8b": "databricks-meta-llama-3-1-8b-instruct",
    "70b": "databricks-meta-llama-3-3-70b-instruct",
}

RAW_SYSTEM_PROMPT = (
    "You are a biblical scholar. Answer the question using ONLY your training knowledge. "
    "You do NOT have access to any database, knowledge graph, or search tools. "
    "When a question mentions a 'knowledge graph', answer as if it said 'all 66 books "
    "of the King James Bible'. "
    "Be as specific and precise as possible — include verse references and complete lists when asked."
)

RAW_SCOPED_PROMPT_TEMPLATE = (
    "You are a biblical scholar. Answer the question using ONLY your training knowledge. "
    "You do NOT have access to any database, knowledge graph, or search tools. "
    "IMPORTANT: You are ONLY permitted to use information from these books: {books}. "
    "Do NOT include any information from other books. "
    "When a question mentions a 'knowledge graph', answer as if it said 'the permitted books'. "
    "Be as specific and precise as possible — include verse references when asked."
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(MODELS.keys()), default="8b")
    parser.add_argument("--no-graph", action="store_true", help="Raw LLM baseline (no graph tools)")
    parser.add_argument("--scoped", action="store_true", help="Run document-scoped cases only")
    parser.add_argument("--cases", nargs="*", help="Case IDs to run (default: all)")
    parser.add_argument("--judge", default="databricks-meta-llama-3-3-70b-instruct")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if args.output is None:
        suffix = "no_graph" if args.no_graph else "graph"
        scope_tag = "_scoped" if args.scoped else ""
        args.output = f"data/graph_value_results_{args.model}_{suffix}{scope_tag}.json"

    endpoint = MODELS[args.model]
    os.environ["GRAPHRAG_LLM_ENDPOINT"] = endpoint
    os.environ["GRAPHRAG_SMALL_LLM_ENDPOINT"] = "databricks-meta-llama-3-1-8b-instruct"

    variant = "raw_llm" if args.no_graph else "graph"
    print(f"Model:    {args.model} ({endpoint})")
    print(f"Variant:  {variant}")
    print(f"Scoped:   {args.scoped}")
    print(f"Backend:  {os.environ.get('GRAPHRAG_BACKEND')}")
    print(f"LLM:      {os.environ.get('GRAPHRAG_LLM_PROVIDER')}")
    print(f"Judge:    {args.judge}")
    print(f"Cases:    {args.cases or 'all'}")
    print("=" * 60)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    suite_path = os.path.join(os.path.dirname(__file__), "..", "src", "evaluation", "graph_value_test_suite.json")
    with open(suite_path) as f:
        suite = json.load(f)

    from databricks_langchain import ChatDatabricks
    judge_llm = ChatDatabricks(endpoint=args.judge, temperature=0.0, max_tokens=512)

    if args.scoped:
        _run_scoped(args, suite, endpoint, variant, judge_llm)
    else:
        _run_core(args, suite, endpoint, variant, judge_llm)


def _run_core(args, suite, endpoint, variant, judge_llm):
    """Run core 35 test cases with source_grounding dimension."""
    from src.evaluation.graph_value_runner import score_case

    if args.no_graph:
        from databricks_langchain import ChatDatabricks
        raw_llm = ChatDatabricks(endpoint=endpoint, temperature=0.0)

        def predict_fn(question):
            messages = [
                {"role": "system", "content": RAW_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]
            resp = raw_llm.invoke(messages)
            return {"response": resp.content, "tool_calls": []}
    else:
        from src.agent.agent_serving import GraphRAGAgent
        agent = GraphRAGAgent(endpoint=endpoint)

        def predict_fn(question):
            from mlflow.types.responses import ResponsesAgentRequest
            req = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
            resp = agent.predict(req)
            text_parts = []
            tool_calls = []
            for item in resp.output:
                item_type = getattr(item, "type", "")
                if item_type == "message":
                    for block in getattr(item, "content", []):
                        t = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                        if t:
                            text_parts.append(t)
                elif item_type == "function_call":
                    tool_calls.append({"name": getattr(item, "name", ""), "args": getattr(item, "arguments", "")})
            return {"response": "\n".join(text_parts), "tool_calls": tool_calls}

    cases = suite["test_cases"]
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases]

    print(f"\nRunning {len(cases)} core cases...\n")

    results = []
    for i, case in enumerate(cases, 1):
        t0 = time.time()
        print(f"[{i}/{len(cases)}] {case['id']} ({case['category']}): ", end="", flush=True)
        try:
            raw = predict_fn(case["user_question"])
            elapsed = time.time() - t0
            print(f"{elapsed:.1f}s ", end="", flush=True)

            scores = score_case(case, raw["response"], judge_llm, variant=variant)
            total = scores.get("total", 0)
            sg = scores.get("source_grounding", 0)
            print(f"-> {total}/13 (sg={sg}/3)", flush=True)

            results.append({
                "case_id": case["id"],
                "category": case["category"],
                "model": args.model,
                "variant": variant,
                "response": raw["response"][:500],
                "tool_calls": raw.get("tool_calls", []),
                "scores": scores,
                "latency_s": round(elapsed, 1),
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"ERROR ({elapsed:.1f}s): {e}", flush=True)
            results.append({
                "case_id": case["id"],
                "category": case["category"],
                "model": args.model,
                "variant": variant,
                "error": str(e),
                "latency_s": round(elapsed, 1),
            })

    _print_core_summary(results, variant)
    _write_results(args.output, args.model, variant, endpoint, results, scoped=False)


def _run_scoped(args, suite, endpoint, variant, judge_llm):
    """Run 12 document-scoped cases with scope_compliance dimension."""
    from src.evaluation.graph_value_runner import score_scoped_case

    scoped_cases = suite.get("scoped_test_cases", [])
    if args.cases:
        scoped_cases = [c for c in scoped_cases if c["id"] in args.cases]

    print(f"\nRunning {len(scoped_cases)} scoped cases...\n")

    results = []
    for i, case in enumerate(scoped_cases, 1):
        t0 = time.time()
        permitted = case["permitted_books"]
        print(f"[{i}/{len(scoped_cases)}] {case['id']} (books={permitted}): ", end="", flush=True)

        try:
            if args.no_graph:
                from databricks_langchain import ChatDatabricks
                raw_llm = ChatDatabricks(endpoint=endpoint, temperature=0.0)
                scoped_prompt = RAW_SCOPED_PROMPT_TEMPLATE.format(books=", ".join(permitted))
                messages = [
                    {"role": "system", "content": scoped_prompt},
                    {"role": "user", "content": case["user_question"]},
                ]
                resp = raw_llm.invoke(messages)
                response_text = resp.content
            else:
                from src.agent.agent_serving import GraphRAGAgent, build_scoped_tools_local
                scoped_tools = build_scoped_tools_local(permitted)
                scoped_agent = GraphRAGAgent(endpoint=endpoint, tools=scoped_tools)

                from mlflow.types.responses import ResponsesAgentRequest
                req = ResponsesAgentRequest(input=[{"role": "user", "content": case["user_question"]}])
                resp = scoped_agent.predict(req)
                text_parts = []
                for item in resp.output:
                    item_type = getattr(item, "type", "")
                    if item_type == "message":
                        for block in getattr(item, "content", []):
                            t = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                            if t:
                                text_parts.append(t)
                response_text = "\n".join(text_parts)

            elapsed = time.time() - t0
            print(f"{elapsed:.1f}s ", end="", flush=True)

            scores = score_scoped_case(case, response_text, judge_llm, variant=variant)
            total = scores.get("total", 0)
            sc = scores.get("scope_compliance", 0)
            sg = scores.get("source_grounding", 0)
            print(f"-> {total}/7 (scope={sc}/2, sg={sg}/3)", flush=True)

            results.append({
                "case_id": case["id"],
                "category": "document_scoped",
                "permitted_books": permitted,
                "model": args.model,
                "variant": variant,
                "response": response_text[:500],
                "scores": scores,
                "latency_s": round(elapsed, 1),
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"ERROR ({elapsed:.1f}s): {e}", flush=True)
            results.append({
                "case_id": case["id"],
                "category": "document_scoped",
                "permitted_books": permitted,
                "model": args.model,
                "variant": variant,
                "error": str(e),
                "latency_s": round(elapsed, 1),
            })

    _print_scoped_summary(results, variant)
    _write_results(args.output, args.model, variant, endpoint, results, scoped=True)


def _print_core_summary(results, variant):
    from collections import defaultdict
    print("\n" + "=" * 60)
    print(f"CORE RESULTS SUMMARY ({variant})")
    print("=" * 60)

    by_cat = defaultdict(list)
    by_cat_sg = defaultdict(list)
    for r in results:
        if "scores" in r:
            by_cat[r["category"]].append(r["scores"]["total"])
            by_cat_sg[r["category"]].append(r["scores"].get("source_grounding", 0))

    overall = []
    overall_sg = []
    for cat in ["multi-hop", "disambiguation", "constraints", "set-ops", "control_singlehop", "adversarial"]:
        scores = by_cat.get(cat, [])
        sg_scores = by_cat_sg.get(cat, [])
        if scores:
            mean = sum(scores) / len(scores)
            mean_sg = sum(sg_scores) / len(sg_scores) if sg_scores else 0
            overall.extend(scores)
            overall_sg.extend(sg_scores)
            print(f"  {cat:20s}: {mean:5.1f}/13  sg={mean_sg:.1f}/3  (n={len(scores)})")

    if overall:
        print(f"  {'OVERALL':20s}: {sum(overall)/len(overall):5.1f}/13  sg={sum(overall_sg)/len(overall_sg):.1f}/3  (n={len(overall)})")

    errors = [r for r in results if "error" in r]
    if errors:
        print(f"\n  Errors: {len(errors)}")
        for e in errors:
            print(f"    {e['case_id']}: {e['error'][:100]}")


def _print_scoped_summary(results, variant):
    print("\n" + "=" * 60)
    print(f"SCOPED RESULTS SUMMARY ({variant})")
    print("=" * 60)

    scored = [r for r in results if "scores" in r]
    if not scored:
        print("  No scored results.")
        return

    totals = [r["scores"]["total"] for r in scored]
    sc_scores = [r["scores"].get("scope_compliance", 0) for r in scored]
    sg_scores = [r["scores"].get("source_grounding", 0) for r in scored]
    comp_scores = [r["scores"].get("completeness", 0) for r in scored]

    print(f"  Cases scored:      {len(scored)}")
    print(f"  Mean total:        {sum(totals)/len(totals):.1f}/7")
    print(f"  Mean scope_compl:  {sum(sc_scores)/len(sc_scores):.1f}/2")
    print(f"  Mean source_grnd:  {sum(sg_scores)/len(sg_scores):.1f}/3")
    print(f"  Mean completeness: {sum(comp_scores)/len(comp_scores):.1f}/2")

    print("\n  Per-case detail:")
    for r in scored:
        s = r["scores"]
        print(f"    {r['case_id']:10s}: {s['total']}/7  scope={s.get('scope_compliance',0)}/2  sg={s.get('source_grounding',0)}/3  comp={s.get('completeness',0)}/2")

    errors = [r for r in results if "error" in r]
    if errors:
        print(f"\n  Errors: {len(errors)}")
        for e in errors:
            print(f"    {e['case_id']}: {e['error'][:100]}")


def _write_results(output, model, variant, endpoint, results, scoped):
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w") as f:
        json.dump({
            "model": model,
            "variant": variant,
            "endpoint": endpoint,
            "scoped": scoped,
            "results": results,
        }, f, indent=2)
    print(f"\nFull results written to {output}")


if __name__ == "__main__":
    main()
