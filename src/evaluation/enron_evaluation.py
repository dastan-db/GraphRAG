# Databricks notebook source
# MAGIC %md
# MAGIC ### Enron Evaluation — Scorers & Dataset
# MAGIC Reusable LLM-as-judge scorers for the Enron GraphRAG agent.
# MAGIC Moved from notebook `08_Enron_Evaluation.py` to enable cross-notebook reuse
# MAGIC and to establish Enron as the PRIMARY evaluation benchmark (Cycle 5 / REQ-C5-07).

# COMMAND ----------

import json
import re as _re
from mlflow.genai.scorers import scorer
from mlflow.entities import Feedback

from src.evaluation.question_bank import ENRON_EVAL_DATASET

# COMMAND ----------

# DBTITLE 1,Judge Configuration

DATA_CONTEXT = """CRITICAL CONTEXT: The agent is a QA system built on a knowledge graph derived from ~20,000 Enron emails (2000-2002). It can ONLY access:
1. Email content and metadata from the corpus
2. Entities and relationships extracted from those emails
3. A curated org hierarchy table (24 entries from public record)
4. A curated investigation timeline (28 events from public record)
5. Pre-aggregated communication statistics (dyads, person activity)

Do NOT penalize the agent for:
- Missing facts that require external sources (SEC filings, court records, news)
- Saying "not found in graph" when the information genuinely isn't in the email data
- Providing fewer details than historical ground truth when those details are external

DO penalize the agent for:
- Fabricating entities, relationships, or email citations not supported by data
- Contradicting facts that ARE in the graph or curated tables
- Failing to find information that IS available in the knowledge graph"""

_DEFAULT_JUDGE_ENDPOINT = "databricks-claude-sonnet-4-6"


def _make_judge_caller(judge_endpoint: str = None):
    """Return a (_call_judge, endpoint) pair for the given judge model."""
    endpoint = judge_endpoint or _DEFAULT_JUDGE_ENDPOINT

    def _call_judge(prompt: str) -> dict:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint}/invocations",
            body={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 512,
            },
        )
        result_text = resp["choices"][0]["message"]["content"].strip()
        if result_text.startswith("```"):
            result_text = _re.sub(r"^```(?:json)?\s*", "", result_text)
            result_text = _re.sub(r"\s*```$", "", result_text)
        return json.loads(result_text)

    return _call_judge, endpoint

# COMMAND ----------

# DBTITLE 1,Evidence Quality Scorer
@scorer
def evidence_quality(inputs, outputs, expectations=None):
    """LLM judge evaluating whether claims are backed by specific evidence."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Participant Verification Scorer
@scorer
def participant_verification(inputs, outputs, expectations=None):
    """Check if all expected entities are mentioned in the response."""
    expected_entities = (expectations or {}).get("expected_entities", [])
    if not expected_entities:
        return Feedback(value=1.0, rationale="No expected entities to verify")

    text = outputs if isinstance(outputs, str) else str(outputs)
    text_lower = text.lower()

    found = []
    missing = []
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

    return Feedback(
        value=score,
        rationale=f"Found {len(found)}/{len(expected_entities)}: {found}. Missing: {missing}",
    )

# COMMAND ----------

# DBTITLE 1,Organizational Accuracy Scorer
@scorer
def organizational_accuracy(inputs, outputs, expectations=None):
    """LLM judge evaluating whether reported hierarchy matches known Enron structure."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Grounding Integrity Scorer
@scorer
def grounding_integrity(inputs, outputs, expectations=None):
    """LLM judge evaluating whether agent properly distinguishes graph-derived vs external knowledge."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Factual Accuracy Scorer
@scorer
def factual_accuracy(inputs, outputs, expectations=None):
    """LLM judge comparing agent response against dual ground truth with data-limitation awareness."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Hallucination Detection Scorer
@scorer
def hallucination_detection(inputs, outputs, expectations=None):
    """LLM judge detecting fabricated entities, relationships, or citations."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Answer Completeness Scorer
@scorer
def answer_completeness(inputs, outputs, expectations=None):
    """LLM judge scoring whether the agent addressed all parts of the question."""
    _call_judge, _ = _make_judge_caller()
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

# COMMAND ----------

# DBTITLE 1,Dataset Format Unification (Cycle 6 / REQ-C6-01 / GAP-08)

def normalize_eval_record(record: dict) -> dict:
    """Normalize an eval record so it carries both the compact format
    (expected_facts, expected_entities, category) and the scorer-compatible
    format (graph_ground_truth, historical_ground_truth, evidence_required,
    expected_entities, category).

    Handles records from ENRON_EVAL_DATASET (compact) and from the notebook's
    EVAL_DATA (scorer-format) transparently.
    """
    out = {"inputs": dict(record["inputs"]), "expectations": dict(record.get("expectations", {}))}
    exp = out["expectations"]

    if "graph_ground_truth" not in exp and "expected_facts" in exp:
        exp["graph_ground_truth"] = "Expected facts: " + "; ".join(exp["expected_facts"])

    if "historical_ground_truth" not in exp:
        exp["historical_ground_truth"] = ""

    if "evidence_required" not in exp:
        exp["evidence_required"] = True

    if "expected_entities" not in exp:
        exp["expected_entities"] = []

    return out


def build_eval_dataframe(dataset=None):
    """Build a pandas DataFrame from ENRON_EVAL_DATASET (or custom dataset)
    with all fields normalized for both scorer families.

    Returns a DataFrame ready for mlflow.genai.evaluate(data=...).
    """
    import pandas as pd
    records = dataset if dataset is not None else ENRON_EVAL_DATASET
    return pd.DataFrame([normalize_eval_record(r) for r in records])


# COMMAND ----------

# DBTITLE 1,Scorer Factory
ENRON_SCORERS = [
    evidence_quality,
    participant_verification,
    organizational_accuracy,
    grounding_integrity,
    factual_accuracy,
    hallucination_detection,
    answer_completeness,
]


# COMMAND ----------

# DBTITLE 1,Enron PRIMARY Evaluation Dataset (Cycle 5 / REQ-C5-01 / GAP-01+07)
# 63 questions across 3 mandatory categories.
# This dataset supersedes the Bible eval as the primary benchmark.
# Imported from src.evaluation.question_bank

# COMMAND ----------

# DBTITLE 1,Scorer Factory
def build_enron_scorers(judge_model=None):
    """Build the Enron scorer list, optionally with a custom judge endpoint.

    When judge_model is provided, LLM-judge scorers are recreated with
    that endpoint. String-match scorers (participant_verification) are
    returned as-is.

    Args:
        judge_model: e.g. "databricks-claude-sonnet-4-6" or None for default.

    Returns:
        List of 7 Enron evaluation scorers.
    """
    if judge_model is None:
        return list(ENRON_SCORERS)

    _call_judge, _ = _make_judge_caller(judge_model)

    @scorer
    def evidence_quality_j(inputs, outputs, expectations=None):
        """evidence_quality with custom judge."""
        evidence_required = (expectations or {}).get("evidence_required", True)
        if evidence_required is False:
            return Feedback(value=1.0, rationale="Evidence not required for this question")
        text = outputs if isinstance(outputs, str) else str(outputs)
        if text.startswith("ERROR:") or len(text.strip()) < 20:
            return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
        prompt = f"""{DATA_CONTEXT}
Evaluate whether this response provides sufficient EVIDENCE for its claims.
Scoring: 1.0=most claims supported, 0.7=key claims supported, 0.5=some evidence, 0.3=minimal, 0.0=none.
Agent Response: {text[:3000]}
Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
        try:
            parsed = _call_judge(prompt)
            return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
        except Exception as e:
            return Feedback(value=0.0, rationale=f"Judge failed: {e}")

    return [
        evidence_quality_j,
        participant_verification,
        organizational_accuracy,
        grounding_integrity,
        factual_accuracy,
        hallucination_detection,
        answer_completeness,
    ]


def build_enron_eval_records(dataset=None) -> list[dict]:
    """Return normalized MLflow eval records for the Enron dataset."""
    records = dataset if dataset is not None else ENRON_EVAL_DATASET
    return [normalize_eval_record(record) for record in records]


def get_enron_score_columns(results_df):
    import pandas as pd

    return [
        col
        for col in results_df.columns
        if col.endswith("/value")
        and col != "evidence_required/value"
        and pd.api.types.is_numeric_dtype(results_df[col])
    ]


def summarize_enron_eval_results(results, eval_df, elapsed_s: float) -> dict:
    """Summarize MLflow eval output into a stable JSON payload."""
    import pandas as pd

    results_df = results.tables["eval_results"].copy()
    categories = eval_df["expectations"].apply(
        lambda value: value.get("category", "unknown")
    )
    results_df["category"] = categories.values

    score_cols = get_enron_score_columns(results_df)
    overall_metrics: dict[str, float] = {}
    overall_score = None
    score_matrix: dict[str, dict[str, float]] = {}
    worst_questions: list[dict] = []

    if score_cols:
        overall = results_df[score_cols].mean()
        overall_metrics = {
            col.replace("/value", ""): round(float(overall[col]), 4)
            for col in score_cols
        }
        overall_score = round(float(overall.mean()), 4)

        score_agg = {col: "mean" for col in score_cols}
        summary = results_df.groupby("category").agg(score_agg).round(2)
        summary.columns = [col.replace("/value", "") for col in summary.columns]
        score_matrix = {
            str(index): {
                str(col): round(float(value), 4) for col, value in row.items()
            }
            for index, row in summary.to_dict(orient="index").items()
        }

        results_df["avg_score"] = (
            results_df[score_cols]
            .apply(pd.to_numeric, errors="coerce")
            .mean(axis=1)
        )
        worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
        for _, row in worst.iterrows():
            question = row.get("inputs/question", row.get("inputs", ""))
            if isinstance(question, dict):
                question = question.get("question", str(question))
            worst_questions.append(
                {
                    "category": row["category"],
                    "question": question,
                    "avg_score": round(float(row["avg_score"]), 4),
                }
            )

    return {
        "score_columns": [col.replace("/value", "") for col in score_cols],
        "overall_metrics": overall_metrics,
        "overall_score": overall_score,
        "score_matrix_by_category": score_matrix,
        "worst_questions": worst_questions,
        "slice_question_count": len(eval_df),
        "elapsed_s": round(float(elapsed_s), 1),
    }
