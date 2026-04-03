"""Evaluate the deployed Enron GraphRAG agent endpoint.

Equivalent to notebook 08_Enron_Evaluation.py but runs locally
against the deployed Model Serving endpoint instead of in-process.

Usage:
    python scripts/eval_deployed.py                 # full 30-question eval
    python scripts/eval_deployed.py --cases 5       # quick subset
    python scripts/eval_deployed.py --category path # filter by category
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mlflow
import pandas as pd
from databricks.sdk import WorkspaceClient
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer
from src.evaluation.enron_evaluation import DATA_CONTEXT, _make_judge_caller
from src.evaluation.question_bank import ENRON_CORE_EVAL_DATA

ENDPOINT_NAME = "graphrag-enron-agent"
JUDGE_ENDPOINT = os.environ.get(
    "GRAPHRAG_JUDGE_ENDPOINT", "databricks-claude-sonnet-4-6"
)
_call_judge, JUDGE_ENDPOINT = _make_judge_caller(JUDGE_ENDPOINT)


def predict_deployed(question: str, endpoint_name: str = ENDPOINT_NAME) -> str:
    """Query the deployed Enron GraphRAG agent endpoint."""
    w = WorkspaceClient()
    try:
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={"input": [{"role": "user", "content": question}]},
        )
        texts = []
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            elif "text" in item:
                texts.append(item["text"])
        return "\n".join(texts) if texts else str(resp)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Evaluation dataset — canonical bank view
# ---------------------------------------------------------------------------
EVAL_DATA = ENRON_CORE_EVAL_DATA

@scorer
def evidence_quality(inputs, outputs, expectations=None):
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required for this question")
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response provides sufficient EVIDENCE for its claims.
Strong evidence includes: specific email dates, sender/recipient pairs,
Subject: lines, relationship types from the graph (REPORTS_TO, MANAGES, etc.),
email counts, provenance sections with tool citations.

Scoring rubric (0.0 to 1.0):
- 1.0: Most claims supported by specific evidence (dates, names, tool results). Has provenance section.
- 0.7: Key claims have evidence, some minor claims unsupported. May have provenance.
- 0.5: Some evidence present but many claims lack support.
- 0.3: Minimal evidence. Mostly assertions without data backing.
- 0.0: No evidence at all, or response is an error.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def participant_verification(inputs, outputs, expectations=None):
    expected_entities = (expectations or {}).get("expected_entities", [])
    if not expected_entities:
        return Feedback(value=1.0, rationale="No expected entities to verify")
    text = outputs if isinstance(outputs, str) else str(outputs)
    text_lower = text.lower()
    found, missing = [], []
    for entity in expected_entities:
        if entity.lower() in text_lower:
            found.append(entity)
        else:
            last_name = entity.split()[-1] if " " in entity else entity
            if last_name.lower() in text_lower:
                found.append(entity)
            else:
                missing.append(entity)
    score = round(len(found) / len(expected_entities), 2)
    return Feedback(value=score, rationale=f"Found {len(found)}/{len(expected_entities)}: {found}. Missing: {missing}")


@scorer
def organizational_accuracy(inputs, outputs, expectations=None):
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    hist_gt = (expectations or {}).get("historical_ground_truth", "")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response accurately represents organizational structure and relationships.
Check for: correct reporting lines, accurate titles, proper use of relationship types
(REPORTS_TO, MANAGES, etc.), and consistency (no contradictions within the response).

Graph Ground Truth (what the agent should find): {graph_gt}
Historical Ground Truth (for reference — do NOT penalize for missing external facts): {hist_gt}

Scoring rubric (0.0 to 1.0):
- 1.0: Organizational claims are accurate, consistent, and well-structured with relationship types shown.
- 0.7: Most org claims correct, minor omissions, no contradictions.
- 0.5: Core structure correct but missing significant details or has minor inaccuracies.
- 0.3: Partial accuracy with some incorrect relationships.
- 0.0: Significantly wrong hierarchy, contradictions, or fabricated relationships.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def grounding_integrity(inputs, outputs, expectations=None):
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response properly GROUNDS its claims. A well-grounded response:
1. Has a Provenance section with Sources, Grounding level, and Confidence
2. Clearly labels which claims come from graph data vs general knowledge
3. States "All claims grounded in graph data" OR explicitly flags external knowledge
4. Includes tool call citations in the provenance (e.g., "find_connections returned...")

Scoring rubric (0.0 to 1.0):
- 1.0: Has provenance section, all claims properly attributed, grounding level stated.
- 0.7: Has provenance section but some claims lack clear attribution.
- 0.5: Partial provenance or some grounding indicators but not systematic.
- 0.3: Minimal grounding. Unclear what's from graph vs training data.
- 0.0: No grounding, no provenance, or presents training data as graph evidence.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def factual_accuracy(inputs, outputs, expectations=None):
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    hist_gt = (expectations or {}).get("historical_ground_truth", "")
    if not graph_gt and not hist_gt:
        return Feedback(value=1.0, rationale="No ground truth provided")
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    prompt = f"""{DATA_CONTEXT}

Compare the agent's response against BOTH ground truths below. Use the Graph Ground
Truth as the PRIMARY scoring basis — this is what the agent SHOULD be able to find.
Use Historical Ground Truth to detect hallucination — if the agent claims something
from the historical record as graph-derived evidence, that's a grounding violation.

Graph Ground Truth (what the agent CAN find): {graph_gt}
Historical Ground Truth (real-world facts, for hallucination detection): {hist_gt}

Scoring rubric (0.0 to 1.0):
- 1.0: Agent found what the graph contains, correctly stated limitations, no fabrications.
- 0.7: Most graph-derivable facts found, minor omissions, no contradictions.
- 0.5: Core direction correct, some graph data found, some missed.
- 0.3: Few graph facts found, vague answer, or minor factual errors.
- 0.0: Contradicts graph data, fabricates evidence, or presents external facts as graph-derived.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def hallucination_detection(inputs, outputs, expectations=None):
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    hist_gt = (expectations or {}).get("historical_ground_truth", "")
    prompt = f"""{DATA_CONTEXT}

Check this response for HALLUCINATIONS — fabricated information presented as fact.
Types of hallucination to check:
1. Fabricated email citations (dates, senders, subjects that weren't retrieved by tools)
2. Invented relationships not in the graph (e.g., claiming X REPORTS_TO Y with no evidence)
3. External knowledge presented as graph-derived (e.g., SEC filing details claimed as "from email evidence")
4. Made-up entity names or incorrect entity types
5. Invented statistics or counts not from tool results

Graph Ground Truth: {graph_gt}
Historical Ground Truth: {hist_gt}

Scoring rubric (0.0 to 1.0 — higher is BETTER, meaning FEWER hallucinations):
- 1.0: No hallucinations detected. All claims properly sourced or qualified.
- 0.7: Minor unsupported claims but no outright fabrication.
- 0.5: Some claims appear fabricated or external knowledge is presented as graph data.
- 0.3: Multiple hallucinated facts or citations.
- 0.0: Response is mostly fabricated or severely misleading.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def answer_completeness(inputs, outputs, expectations=None):
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response COMPLETELY addresses the user's question.
A complete answer covers all aspects of what was asked, uses multiple relevant
tools/data sources, and doesn't leave obvious follow-up questions.

User Question: {question}
What the graph contains (for reference): {graph_gt}

Scoring rubric (0.0 to 1.0):
- 1.0: All aspects of the question addressed. Comprehensive use of available data.
- 0.7: Main question answered well, minor aspects missing.
- 0.5: Partially answers the question. Key aspects addressed but significant gaps.
- 0.3: Touches on the topic but doesn't really answer what was asked.
- 0.0: Doesn't address the question at all, or only returns an error.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


ALL_SCORERS = [
    evidence_quality,
    participant_verification,
    organizational_accuracy,
    grounding_integrity,
    factual_accuracy,
    hallucination_detection,
    answer_completeness,
]


def _filter_eval_data(
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
) -> list[dict]:
    data = list(EVAL_DATA)
    if category:
        data = [row for row in data if row["category"] == category]
    if split:
        data = [row for row in data if row.get("eval_split") == split]
    if cases:
        data = data[:cases]
    return data


def _build_eval_records(data: list[dict]) -> list[dict]:
    records = []
    for row in data:
        records.append(
            {
                "inputs": {"question": row["question"]},
                "expectations": {
                    "expected_entities": row["expected_entities"],
                    "graph_ground_truth": row["graph_ground_truth"],
                    "historical_ground_truth": row["historical_ground_truth"],
                    "evidence_required": row["evidence_required"],
                    "category": row["category"],
                },
            }
        )
    return records


def run_deployed_evaluation(
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "deployed_eval",
    endpoint_name: str = ENDPOINT_NAME,
    output_json: str | None = None,
) -> dict[str, Any]:
    global JUDGE_ENDPOINT, _call_judge
    if judge:
        _call_judge, JUDGE_ENDPOINT = _make_judge_caller(judge)

    data = _filter_eval_data(cases=cases, category=category, split=split)
    if not data:
        raise ValueError("No evaluation questions matched the requested filters.")

    eval_df = pd.DataFrame(_build_eval_records(data))
    print(
        f"Deployed Evaluation: {len(eval_df)} questions | "
        f"endpoint={endpoint_name} | judge={JUDGE_ENDPOINT}"
    )
    print()

    started = time.time()
    with mlflow.start_run(run_name=run_name):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=lambda question: predict_deployed(question, endpoint_name=endpoint_name),
            scorers=ALL_SCORERS,
        )

    elapsed = time.time() - started
    results_df = results.tables["eval_results"].copy()
    categories = eval_df["expectations"].apply(lambda value: value.get("category", "unknown"))
    results_df["category"] = categories.values

    score_cols = [
        col
        for col in results_df.columns
        if col.endswith("/value")
        and col != "evidence_required/value"
        and pd.api.types.is_numeric_dtype(results_df[col])
    ]

    overall_metrics: dict[str, float] = {}
    overall_score = None
    score_matrix: dict[str, dict[str, float]] = {}
    worst_questions: list[dict[str, Any]] = []

    if score_cols:
        overall = results_df[score_cols].mean()
        print("=== Enron GraphRAG Governance Scores (v2) — DEPLOYED ===")
        for col in score_cols:
            name = col.replace("/value", "")
            overall_metrics[name] = round(float(overall[col]), 4)
            print(f"  {name:35s}: {overall[col]:.2f}")
        overall_score = round(float(overall.mean()), 4)
        print(f"  {'OVERALL':35s}: {overall_score:.2f}")
        print(f"\n  Time: {elapsed:.0f}s ({elapsed / len(eval_df):.1f}s/question)")

        print("\n=== Score Matrix (category x scorer) ===")
        score_agg = {col: "mean" for col in score_cols}
        summary = results_df.groupby("category").agg(score_agg).round(2)
        summary.columns = [col.replace("/value", "") for col in summary.columns]
        print(summary.to_string())
        score_matrix = {
            str(index): {str(col): round(float(value), 4) for col, value in row.items()}
            for index, row in summary.to_dict(orient="index").items()
        }

        results_df["avg_score"] = (
            results_df[score_cols]
            .apply(pd.to_numeric, errors="coerce")
            .mean(axis=1)
        )
        worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
        print("\n=== 5 Lowest Scoring Questions ===")
        for _, row in worst.iterrows():
            question = row.get("inputs/question", row.get("inputs", ""))
            if isinstance(question, dict):
                question = question.get("question", str(question))
            avg_score = round(float(row["avg_score"]), 4)
            print(f"  [{row['category']}] {question[:80]} -> {avg_score:.2f}")
            worst_questions.append(
                {
                    "category": row["category"],
                    "question": question,
                    "avg_score": avg_score,
                }
            )
    else:
        print("No score columns found in results.")

    payload = {
        "version": "1.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint_name": endpoint_name,
        "judge_endpoint": JUDGE_ENDPOINT,
        "slice_question_count": len(eval_df),
        "error_question_count": 0,
        "elapsed_s": round(elapsed, 1),
        "overall_metrics": overall_metrics,
        "overall_score": overall_score,
        "score_matrix_by_category": score_matrix,
        "worst_questions": worst_questions,
        "category": category,
        "split": split,
    }
    if output_json:
        Path(output_json).resolve().write_text(json.dumps(payload, indent=2))
    return payload


def main():
    parser = argparse.ArgumentParser(description="Evaluate deployed Enron GraphRAG agent")
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--split", type=str, default=None, choices=["train", "test", "holdout"], help="Filter by eval split")
    parser.add_argument("--judge", type=str, default=None, help="Judge endpoint name")
    parser.add_argument("--run-name", type=str, default="deployed_eval", help="MLflow run name")
    parser.add_argument("--endpoint-name", type=str, default=ENDPOINT_NAME, help="Serving endpoint name")
    parser.add_argument("--output-json", type=str, default=None, help="Optional JSON summary path")
    args = parser.parse_args()

    payload = run_deployed_evaluation(
        cases=args.cases,
        category=args.category,
        split=args.split,
        judge=args.judge,
        run_name=args.run_name,
        endpoint_name=args.endpoint_name,
        output_json=args.output_json,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
