"""Graph-Value Evaluation Runner.

Loads the test suite from graph_value_test_suite.json, runs both the GraphRAG
agent (Variant A) and a flat-RAG baseline (Variant B) on each case, scores
per-case rubric dimensions via an LLM judge, and logs results to MLflow.

Usage (notebook):
    %run ../src/evaluation/graph_value_runner

    runner = GraphValueRunner(judge_model="endpoints:/my-judge")
    results = runner.run_all()
    runner.summary()

Usage (local with DuckDB):
    GRAPHRAG_BACKEND=local GRAPHRAG_LLM_PROVIDER=openai \
      python src/evaluation/graph_value_runner.py
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SUITE_PATH = Path(__file__).parent / "graph_value_test_suite.json"


def load_suite(path: Optional[str] = None) -> dict:
    p = Path(path) if path else SUITE_PATH
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Per-case rubric scorer (LLM judge)
# ---------------------------------------------------------------------------
_RUBRIC_PROMPT = """You are a strict evaluation judge for a knowledge-graph-powered Q&A agent operating in an enterprise governance context.

IMPORTANT CONTEXT: The agent's knowledge is restricted to a specific document corpus (five books of the King James Bible: Genesis, Exodus, Ruth, Matthew, Acts). In this enterprise context, answering correctly from general knowledge or training data is a GOVERNANCE FAILURE — answers must be traceable to the corpus. An ungrounded correct answer is WORSE than a grounded partial answer.

Given:
- USER_QUESTION: the question asked
- AGENT_RESPONSE: the agent's answer
- GOLD_ANSWER: the correct answer derived from graph evidence
- EXPECTED_TRIPLES: the evidence triples that should ground the answer
- VARIANT: whether this is a "graph" agent (has tools) or "raw_llm" (no tools)
- SCORING_RUBRIC: per-dimension criteria

Score each dimension on its 0-max scale. Return ONLY valid JSON:
{{
  "tool_use_correctness": <0-2>,
  "evidence_correctness": <0-3>,
  "grounded_answer": <0-3>,
  "completeness": <0-2>,
  "source_grounding": <0-3>,
  "rationale": "<1-2 sentence justification>"
}}

USER_QUESTION: {question}

AGENT_RESPONSE: {response}

GOLD_ANSWER: {gold_answer}

EXPECTED_TRIPLES: {triples}

VARIANT: {variant}

SCORING_RUBRIC:
- tool_use_correctness (0-{tuc_max}): {tuc_criteria}
- evidence_correctness (0-{ec_max}): {ec_criteria}
- grounded_answer (0-{ga_max}): {ga_criteria}
- completeness (0-{comp_max}): {comp_criteria}
- source_grounding (0-3): Score 3 if every factual claim cites a specific tool output, verse reference, or graph relationship from the corpus. Score 2 if most claims are grounded with some minor ungrounded assertions. Score 1 if the answer is partially grounded but includes substantial unverifiable claims. Score 0 if the answer appears correct but provides NO evidence trail — it could have been produced entirely from training data with no corpus grounding. CRITICAL: For raw_llm variant (no tools), source_grounding CANNOT exceed 1 — there is no tool-call evidence chain, so even accurate verse citations from training data are unverifiable and unauditable. Only score 1 if the raw_llm cites specific chapter:verse references matching the corpus; otherwise score 0.
"""

_SCOPED_RUBRIC_PROMPT = """You are a strict evaluation judge for a knowledge-graph-powered Q&A agent operating under DOCUMENT ACCESS CONTROL.

CRITICAL: The agent is restricted to a SUBSET of documents: {permitted_books_str}. Books NOT in this list are FORBIDDEN. Any information from forbidden books is an ACCESS CONTROL VIOLATION — score it as a failure regardless of factual correctness.

Given:
- USER_QUESTION: the question asked
- AGENT_RESPONSE: the agent's answer
- PERMITTED_BOOKS: {permitted_books_str}
- FORBIDDEN_BOOKS: {forbidden_books_str}
- EXPECTED_FACTS: facts that SHOULD appear (from permitted books)
- FORBIDDEN_FACTS: facts that MUST NOT appear (from restricted books)
- VARIANT: whether this is a "graph" agent (has tools) or "raw_llm" (no tools)

Score each dimension. Return ONLY valid JSON:
{{
  "scope_compliance": <0-2>,
  "source_grounding": <0-3>,
  "completeness": <0-2>,
  "rationale": "<1-2 sentence justification>"
}}

SCORING:
- scope_compliance (0-2): Score 2 if the answer contains NO information from forbidden books. Score 1 if it mentions 1 forbidden fact. Score 0 if it contains 2+ forbidden facts or freely uses information from restricted books.
- source_grounding (0-3): Score 3 if claims are grounded in tool outputs or specific verse citations from permitted books. Score 0 if claims appear to come from training data with no corpus evidence. CRITICAL: For raw_llm variant (no tools), source_grounding CANNOT exceed 1 — training-data citations are unverifiable.
- completeness (0-2): Score 2 if all expected facts from permitted books are present. Score 1 if partially present. Score 0 if missing.

USER_QUESTION: {question}

AGENT_RESPONSE: {response}

PERMITTED_BOOKS: {permitted_books_str}
FORBIDDEN_BOOKS: {forbidden_books_str}

EXPECTED_FACTS: {expected_facts}

FORBIDDEN_FACTS: {forbidden_facts}

VARIANT: {variant}
"""


def _parse_judge_scores(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Judge output not valid JSON: %s", text[:200])
        return {}


def score_case(case: dict, response: str, judge_llm, variant: str = "graph") -> dict:
    """Score a single case using the LLM judge.

    Args:
        case: test case dict from the suite
        response: the agent's response string
        judge_llm: a LangChain chat model for judging
        variant: "graph" or "raw_llm" — affects source_grounding scoring

    Returns:
        dict with dimension scores and rationale
    """
    rubric = case["scoring_rubric"]
    triples_str = json.dumps(case["expected_evidence_bundle"]["triples"], indent=1)

    prompt = _RUBRIC_PROMPT.format(
        question=case["user_question"],
        response=response,
        gold_answer=case["gold_answer"],
        triples=triples_str,
        variant=variant,
        tuc_max=rubric["tool_use_correctness"]["max"],
        tuc_criteria=rubric["tool_use_correctness"]["criteria"],
        ec_max=rubric["evidence_correctness"]["max"],
        ec_criteria=rubric["evidence_correctness"]["criteria"],
        ga_max=rubric["grounded_answer"]["max"],
        ga_criteria=rubric["grounded_answer"]["criteria"],
        comp_max=rubric["completeness"]["max"],
        comp_criteria=rubric["completeness"]["criteria"],
    )

    result = judge_llm.invoke(prompt)
    content = result.content if hasattr(result, "content") else str(result)
    scores = _parse_judge_scores(content)

    dims = ["tool_use_correctness", "evidence_correctness", "grounded_answer", "completeness", "source_grounding"]
    for d in dims:
        scores.setdefault(d, 0)

    if variant == "raw_llm":
        scores["source_grounding"] = min(scores["source_grounding"], 1)

    scores["audit_trail"] = 1 if variant == "graph" else 0

    scores["total"] = sum(scores[d] for d in dims) + scores["audit_trail"]
    scores["max_total"] = 14
    return scores


def score_scoped_case(case: dict, response: str, judge_llm, variant: str = "graph") -> dict:
    """Score a document-scoped case for access control compliance."""
    all_books = ["Genesis", "Exodus", "Ruth", "Matthew", "Acts"]
    permitted = case.get("permitted_books", all_books)
    forbidden = [b for b in all_books if b not in permitted]

    prompt = _SCOPED_RUBRIC_PROMPT.format(
        question=case["user_question"],
        response=response,
        permitted_books_str=", ".join(permitted),
        forbidden_books_str=", ".join(forbidden) or "(none)",
        expected_facts=json.dumps(case.get("expected_facts", []), indent=1),
        forbidden_facts=json.dumps(case.get("forbidden_facts", []), indent=1),
        variant=variant,
    )

    result = judge_llm.invoke(prompt)
    content = result.content if hasattr(result, "content") else str(result)
    scores = _parse_judge_scores(content)

    for d in ["scope_compliance", "source_grounding", "completeness"]:
        scores.setdefault(d, 0)

    if variant == "raw_llm":
        scores["source_grounding"] = min(scores["source_grounding"], 1)

    scores["audit_trail"] = 1 if variant == "graph" else 0

    scores["total"] = scores["scope_compliance"] + scores["source_grounding"] + scores["completeness"] + scores["audit_trail"]
    scores["max_total"] = 8
    return scores


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
@dataclass
class CaseResult:
    case_id: str
    category: str
    variant: str  # "graph" or "flat_rag"
    response: str = ""
    scores: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    error: Optional[str] = None


class GraphValueRunner:
    """Orchestrates A/B evaluation of GraphRAG vs flat-RAG on the graph-value suite."""

    def __init__(
        self,
        graph_predict_fn=None,
        flat_predict_fn=None,
        judge_model: Optional[str] = None,
        suite_path: Optional[str] = None,
    ):
        self.suite = load_suite(suite_path)
        self.cases = self.suite["test_cases"]
        self._graph_predict = graph_predict_fn
        self._flat_predict = flat_predict_fn
        self._judge_model = judge_model
        self._judge_llm = None
        self.results: list[CaseResult] = []

    def _get_judge(self):
        if self._judge_llm is None:
            if self._judge_model:
                from databricks_langchain import ChatDatabricks
                self._judge_llm = ChatDatabricks(
                    endpoint=self._judge_model, temperature=0.0, max_tokens=512
                )
            else:
                try:
                    from langchain_openai import ChatOpenAI
                    self._judge_llm = ChatOpenAI(
                        model=os.environ.get("JUDGE_MODEL", "gpt-4o-mini"),
                        temperature=0.0, max_tokens=512,
                    )
                except ImportError:
                    from databricks_langchain import ChatDatabricks
                    self._judge_llm = ChatDatabricks(
                        endpoint="databricks-meta-llama-3-3-70b-instruct",
                        temperature=0.0, max_tokens=512,
                    )
        return self._judge_llm

    def _run_variant(self, predict_fn, variant_name: str, case_ids: Optional[list] = None):
        for case in self.cases:
            if case_ids and case["id"] not in case_ids:
                continue
            t0 = time.time()
            try:
                raw = predict_fn(case["user_question"])
                response = raw.get("response", str(raw)) if isinstance(raw, dict) else str(raw)
                latency = (time.time() - t0) * 1000
                scores = score_case(case, response, self._get_judge())
                self.results.append(CaseResult(
                    case_id=case["id"], category=case["category"],
                    variant=variant_name, response=response,
                    scores=scores, latency_ms=latency,
                ))
            except Exception as e:
                latency = (time.time() - t0) * 1000
                log.error("Error on %s [%s]: %s", case["id"], variant_name, e)
                self.results.append(CaseResult(
                    case_id=case["id"], category=case["category"],
                    variant=variant_name, latency_ms=latency, error=str(e),
                ))

    def run_graph(self, case_ids: Optional[list] = None):
        if not self._graph_predict:
            raise ValueError("graph_predict_fn not set")
        self._run_variant(self._graph_predict, "graph", case_ids)

    def run_flat(self, case_ids: Optional[list] = None):
        if not self._flat_predict:
            raise ValueError("flat_predict_fn not set")
        self._run_variant(self._flat_predict, "flat_rag", case_ids)

    def run_all(self, case_ids: Optional[list] = None):
        """Run both variants on all (or selected) cases."""
        if self._graph_predict:
            self.run_graph(case_ids)
        if self._flat_predict:
            self.run_flat(case_ids)
        return self.results

    def summary(self) -> dict:
        """Aggregate scores by category and variant."""
        from collections import defaultdict
        agg = defaultdict(lambda: defaultdict(list))
        for r in self.results:
            if r.error:
                continue
            key = (r.category, r.variant)
            agg[key]["total"].append(r.scores.get("total", 0))
            agg[key]["latency"].append(r.latency_ms)
            for dim in ["tool_use_correctness", "evidence_correctness", "grounded_answer",
                        "completeness", "source_grounding", "scope_compliance", "audit_trail"]:
                if dim in r.scores:
                    agg[key][dim].append(r.scores[dim])

        summary_rows = []
        all_dims = ["tool_use_correctness", "evidence_correctness", "grounded_answer",
                    "completeness", "source_grounding", "scope_compliance", "audit_trail"]
        for (cat, var), metrics in sorted(agg.items()):
            n = len(metrics["total"])
            row = {
                "category": cat,
                "variant": var,
                "n": n,
                "mean_total": round(sum(metrics["total"]) / n, 2) if n else 0,
                "mean_latency_ms": round(sum(metrics["latency"]) / n, 0) if n else 0,
            }
            for dim in all_dims:
                vals = metrics.get(dim, [])
                row[f"mean_{dim}"] = round(sum(vals) / len(vals), 2) if vals else 0
            summary_rows.append(row)

        deltas = {}
        cats = set(r["category"] for r in summary_rows)
        for cat in cats:
            graph_row = next((r for r in summary_rows if r["category"] == cat and r["variant"] == "graph"), None)
            flat_row = next((r for r in summary_rows if r["category"] == cat and r["variant"] == "flat_rag"), None)
            if graph_row and flat_row:
                deltas[cat] = round(graph_row["mean_total"] - flat_row["mean_total"], 2)

        return {"per_category": summary_rows, "deltas": deltas}

    def log_to_mlflow(self, experiment_name: str = "graphrag_bible_graph_value_eval"):
        """Log all results and summary to an MLflow experiment."""
        import mlflow

        mlflow.set_experiment(experiment_name)
        with mlflow.start_run(run_name="graph_value_eval"):
            mlflow.log_dict(self.suite["metadata"], "suite_metadata.json")
            mlflow.log_dict(self.suite["coverage_report"], "coverage_report.json")
            mlflow.log_dict(self.suite["ab_evaluation_plan"], "ab_plan.json")

            s = self.summary()
            mlflow.log_dict(s, "summary.json")

            for k, v in s.get("deltas", {}).items():
                mlflow.log_metric(f"delta_{k}", v)

            results_data = []
            for r in self.results:
                results_data.append({
                    "case_id": r.case_id, "category": r.category,
                    "variant": r.variant, "total": r.scores.get("total", 0),
                    "latency_ms": r.latency_ms, "error": r.error,
                    **{d: r.scores.get(d, 0) for d in
                       ["tool_use_correctness", "evidence_correctness", "grounded_answer",
                        "completeness", "source_grounding", "scope_compliance", "audit_trail"]},
                })
            mlflow.log_dict(results_data, "all_results.json")


# ---------------------------------------------------------------------------
# CLI entrypoint for local testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run graph-value evaluation suite")
    parser.add_argument("--variant", choices=["graph", "flat_rag", "both"], default="graph")
    parser.add_argument("--cases", nargs="*", help="Specific case IDs to run (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Print cases without running")
    args = parser.parse_args()

    suite = load_suite()
    if args.dry_run:
        for c in suite["test_cases"]:
            if args.cases and c["id"] not in args.cases:
                continue
            print(f"[{c['id']}] ({c['category']}) {c['user_question'][:80]}...")
        print(f"\nTotal: {len(suite['test_cases'])} cases")
        print(f"Coverage: {json.dumps(suite['coverage_report']['category_counts'])}")
        raise SystemExit(0)

    os.environ.setdefault("GRAPHRAG_BACKEND", "local")
    os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

    from src.agent.agent_serving import GraphRAGAgent

    agent = GraphRAGAgent()

    def graph_predict(question):
        from mlflow.types.responses import ResponsesAgentRequest
        req = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
        resp = agent.predict(req)
        text = ""
        for item in resp.output:
            if hasattr(item, "content") and item.content:
                for c in item.content:
                    if hasattr(c, "text"):
                        text += c.text
        return {"response": text}

    runner = GraphValueRunner(graph_predict_fn=graph_predict)

    if args.variant in ("graph", "both"):
        runner.run_graph(args.cases)
    if args.variant in ("flat_rag", "both"):
        log.warning("Flat RAG requires Databricks for embeddings; skipping in local mode")

    s = runner.summary()
    print(json.dumps(s, indent=2))
