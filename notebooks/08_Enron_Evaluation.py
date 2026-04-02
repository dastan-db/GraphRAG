# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Evaluation (PRIMARY BENCHMARK)
# MAGIC
# MAGIC **PRIMARY evaluation benchmark** for the GraphRAG Legal Intelligence Platform.
# MAGIC The Enron corpus is the proof of generalization; Bible corpus is development/debug only.
# MAGIC
# MAGIC Evaluates the Enron GraphRAG agent using MLflow GenAI evaluation with
# MAGIC LLM-as-judge scorers for evidence quality, organizational accuracy,
# MAGIC grounding integrity, factual accuracy, and governance dimensions
# MAGIC (tool usage correctness, hallucination detection, answer completeness).
# MAGIC
# MAGIC **Cycle 5:** Imported reusable scorers from `src/evaluation/enron_evaluation`,
# MAGIC expanded evaluation dataset (63 questions across 3 categories),
# MAGIC latency SLA compliance, and Jaccard reproducibility threshold.
# MAGIC
# MAGIC **Cycle 6:** Unified eval dataset format, MLflow trace span latency, LLM-judge session isolation, 5-config harness, exhaustion prompting.
# MAGIC
# MAGIC **Cycle 7:** Full-corpus flat RAG baseline, all-63-question cross-config comparison, isolation scorer calibration, provenance structure compliance.
# MAGIC
# MAGIC **Cycle 8:** Semantic provenance content quality (LLM judge), pre-computed embedding cache for flat RAG baseline.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 databricks-langchain langgraph>=0.3.4 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Load Tools (required by agent module)
# MAGIC %run ../src/agent/tools

# COMMAND ----------

# DBTITLE 1,Load Agent (for system prompts and GraphRAGAgent class)
# MAGIC %run ../src/agent/agent

# COMMAND ----------

# DBTITLE 1,Load Enron Scorers (reusable module)
# MAGIC %run ../src/evaluation/enron_evaluation

# COMMAND ----------

# DBTITLE 1,Load Evaluation Framework (new Cycle 5 scorers)
# MAGIC %run ../src/evaluation/evaluation

# COMMAND ----------

# DBTITLE 1,Import Libraries
import json
import mlflow
import pandas as pd
from mlflow.entities import Feedback

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Evaluation Dataset (Dual Ground Truth)
# MAGIC
# MAGIC Each question has both `graph_ground_truth` (what the knowledge graph
# MAGIC actually contains) and `historical_ground_truth` (real-world facts from
# MAGIC public record). The judge uses `graph_ground_truth` for scoring but
# MAGIC references `historical_ground_truth` to detect hallucination.

# COMMAND ----------

# DBTITLE 1,Enron Evaluation Dataset
EVAL_DATA = NOTEBOOK_08_EVAL_DATA

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Predict Function
# MAGIC
# MAGIC Wraps the Enron agent endpoint for MLflow evaluation.

# COMMAND ----------

# DBTITLE 1,Agent Predict Function
ENRON_ENDPOINT = "graphrag-enron-agent"

def predict_enron_agent(question: str) -> str:
    """Query the Enron GraphRAG agent and return the full response text."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    try:
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{ENRON_ENDPOINT}/invocations",
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: LLM-as-Judge Scorers
# MAGIC
# MAGIC All scorers except `participant_verification` use LLM-as-judge via
# MAGIC the configured judge endpoint for semantic evaluation rather than
# MAGIC fragile regex matching.

# COMMAND ----------

# DBTITLE 1,Judge Helper
from mlflow.genai.scorers import scorer

JUDGE_ENDPOINT = config.get("judge_endpoint", "databricks-claude-sonnet-4-6")

def _call_judge(prompt: str) -> dict:
    """Call the judge LLM and parse a JSON response with 'score' and 'justification'."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{JUDGE_ENDPOINT}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 512,
        },
    )
    import re
    result_text = resp["choices"][0]["message"]["content"].strip()
    if result_text.startswith("```"):
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
        result_text = re.sub(r"\s*```$", "", result_text)
    return json.loads(result_text)

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

# COMMAND ----------

# DBTITLE 1,Evidence Quality Scorer (replaces email_citation_completeness + evidence_sufficiency)
@scorer
def evidence_quality(inputs, outputs, expectations=None):
    """LLM judge evaluating whether claims are backed by specific evidence."""
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

# DBTITLE 1,Participant Verification Scorer (kept as string-match)
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

# DBTITLE 1,Organizational Accuracy Scorer (LLM judge)
@scorer
def organizational_accuracy(inputs, outputs, expectations=None):
    """LLM judge evaluating whether reported hierarchy matches known Enron structure."""
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

# DBTITLE 1,Grounding Integrity Scorer (LLM judge)
@scorer
def grounding_integrity(inputs, outputs, expectations=None):
    """LLM judge evaluating whether agent properly distinguishes graph-derived vs external knowledge."""
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

# DBTITLE 1,Factual Accuracy Scorer (LLM judge, data-limitation aware)
@scorer
def factual_accuracy(inputs, outputs, expectations=None):
    """LLM judge comparing agent response against dual ground truth with data-limitation awareness."""
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

# DBTITLE 1,Hallucination Detection Scorer (LLM judge)
@scorer
def hallucination_detection(inputs, outputs, expectations=None):
    """LLM judge detecting fabricated entities, relationships, or citations."""
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

# DBTITLE 1,Answer Completeness Scorer (LLM judge)
@scorer
def answer_completeness(inputs, outputs, expectations=None):
    """LLM judge scoring whether the agent addressed all parts of the question."""
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

# MAGIC %md
# MAGIC ## Step 4: Run Evaluation
# MAGIC
# MAGIC Evaluate the Enron agent with all governance scorers.

# COMMAND ----------

# DBTITLE 1,Execute Evaluation (Cycle 5 — PRIMARY benchmark with imported scorers)
with mlflow.start_run(run_name="enron_graphrag_eval_cycle5"):
    results = mlflow.genai.evaluate(
        data=eval_df,
        predict_fn=predict_enron_agent,
        scorers=build_enron_scorers() + [
            citation_accuracy,
            latency_sla_compliance,
        ],
    )

print("Evaluation complete!")
_drop_cols = [c for c in results.tables["eval_results"].columns if c in ("assessments", "spans", "trace", "tags", "trace_metadata")]
display(results.tables["eval_results"].drop(columns=_drop_cols))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Analyze Results

# COMMAND ----------

# DBTITLE 1,Score Summary by Category
results_df = results.tables["eval_results"]

categories = eval_df["expectations"].apply(lambda x: x.get("category", "unknown"))
results_df = results_df.copy()
results_df["category"] = categories.values

score_cols = [c for c in results_df.columns if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])]
if score_cols:
    score_agg = {col: "mean" for col in score_cols}
    summary = results_df.groupby("category").agg(score_agg).round(2)
    summary.columns = [c.replace("/value", "") for c in summary.columns]
    display(summary)
else:
    print("No score columns found in results.")

# COMMAND ----------

# DBTITLE 1,Overall Governance Score
score_cols = [c for c in results_df.columns if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])]
if score_cols:
    overall = results_df[score_cols].mean()
    print("=== Enron GraphRAG Governance Scores (v2) ===")
    for col in score_cols:
        name = col.replace("/value", "")
        print(f"  {name:35s}: {overall[col]:.2f}")
    print(f"  {'OVERALL':35s}: {overall.mean():.2f}")
else:
    print("No score columns found in results.")

# COMMAND ----------

# DBTITLE 1,Lowest Scoring Questions
if score_cols:
    results_df["avg_score"] = pd.to_numeric(results_df[score_cols].apply(pd.to_numeric, errors="coerce").mean(axis=1), errors="coerce")
    worst = results_df.nsmallest(5, "avg_score")[["category", "avg_score"] + score_cols]
    worst.columns = [c.replace("/value", "") for c in worst.columns]
    display(worst)

# COMMAND ----------

# DBTITLE 1,Category Radar Summary
if score_cols:
    radar_data = results_df.groupby("category")[score_cols].mean().round(2)
    radar_data.columns = [c.replace("/value", "") for c in radar_data.columns]
    print("\n=== Score Matrix (category x scorer) ===")
    display(radar_data)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Step 6: Cycle 5 — Expanded Dataset Evaluation
# MAGIC
# MAGIC Evaluate against the 63-question expanded dataset from `src/evaluation/enron_evaluation.py`.
# MAGIC This is the PRIMARY benchmark for the GraphRAG Legal Intelligence Platform.

# COMMAND ----------

# DBTITLE 1,Expanded Dataset Evaluation (63 questions — unified format)
expanded_df = build_eval_dataframe(ENRON_EVAL_DATASET)
print(f"Expanded dataset: {len(expanded_df)} questions (format unified via Cycle 6 / REQ-C6-01)")
category_counts = expanded_df["expectations"].apply(lambda x: x.get("category", "unknown")).value_counts()
print(f"Categories: {dict(category_counts)}")

# COMMAND ----------

# DBTITLE 1,Run Expanded Evaluation
with mlflow.start_run(run_name="enron_expanded_eval_cycle6"):
    expanded_results = mlflow.genai.evaluate(
        data=expanded_df,
        predict_fn=predict_enron_agent,
        scorers=build_enron_scorers() + [
            citation_accuracy,
            latency_sla_compliance,
            provenance_structure_compliance,
            provenance_content_quality,
        ],
    )

print("Expanded evaluation complete!")
_drop_cols = [c for c in expanded_results.tables["eval_results"].columns if c in ("assessments", "spans", "trace", "tags", "trace_metadata")]
display(expanded_results.tables["eval_results"].drop(columns=_drop_cols))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: Latency SLA Report

# COMMAND ----------

# DBTITLE 1,Latency SLA Report
from src.agent.tools import get_latency_report

latency = get_latency_report()
if latency:
    print("=== Tool Latency SLA Report ===")
    for tool_name, stats in latency.items():
        sla_status = "PASS" if stats.get("sla_compliant") else "FAIL" if stats.get("sla_compliant") is False else "N/A"
        print(f"  {tool_name:30s}: p50={stats['p50_ms']:>8.1f}ms  p95={stats['p95_ms']:>8.1f}ms  p99={stats['p99_ms']:>8.1f}ms  [{sla_status}]")
else:
    print("No latency data available — tools may not have been invoked in this process.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8: Reproducibility Test (Jaccard threshold)

# COMMAND ----------

# DBTITLE 1,Reproducibility Test
repro_rows, overall_jaccard, cert = run_reproducibility_test(predict_enron_agent, num_runs=3)
print(f"=== Reproducibility Test ===")
print(f"  Overall Jaccard: {overall_jaccard}")
print(f"  Threshold:       {cert['threshold']}")
print(f"  Status:          {'CERTIFIED' if cert['passed'] else 'NOT CERTIFIED'}")
for row in repro_rows:
    print(f"  {row['Question']:70s} cite={row['Citation Jaccard']:.3f} path={row['Path Jaccard']:.3f}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9: Session Isolation Calibration (Cycle 7 / REQ-C7-03)

# COMMAND ----------

# DBTITLE 1,Run Isolation Scorer Calibration
from src.evaluation.evaluation import run_isolation_calibration

cal_report = run_isolation_calibration()
print(f"=== Session Isolation Scorer Calibration ===")
print(f"  Accuracy: {cal_report['calibration_accuracy']:.1%} ({cal_report['correct']}/{cal_report['total']})")
for r in cal_report["results"]:
    status = "CORRECT" if r["correct"] else "WRONG"
    print(f"  {r['label']:30s} expected={r['expected']:.1f} actual={r['actual']:.3f} [{status}]")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Step 10: Five-Configuration Comparison (Cycle 6+7 / REQ-C6-04+C7-01+C7-02)
# MAGIC
# MAGIC Parallel to the Bible 5-config harness. Tests: GraphRAG+70B, GraphRAG+8B, FlatRAG+70B, DirectLLM+70B, DirectExternal.

# COMMAND ----------

# DBTITLE 1,Enron Predict Functions (5 configurations)

def predict_enron_graphrag_70b(question: str) -> dict:
    """GraphRAG + 70B via Model Serving endpoint."""
    return {"response": predict_enron_agent(question)}


def predict_enron_graphrag_8b(question: str) -> dict:
    """GraphRAG + 8B (small model + graph structure)."""
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['small_llm_endpoint'],
        inputs={
            "messages": [
                {"role": "system", "content": ENRON_SYSTEM_PROMPT[:2000]},
                {"role": "user", "content": question},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}


_enron_chunk_cache = {"chunks": None, "embeddings": None}


def _build_enron_email_chunks():
    """Build email chunks from the full corpus with metadata, cached in-process."""
    if _enron_chunk_cache["chunks"] is not None:
        return _enron_chunk_cache["chunks"]

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    emails = spark.sql(f"""
        SELECT subject, body, sender, date_sent,
               CONCAT(
                   'Subject: ', COALESCE(subject, '(none)'),
                   '\nFrom: ', COALESCE(sender, 'unknown'),
                   '\nDate: ', COALESCE(CAST(date_sent AS STRING), 'unknown'),
                   '\n\n', SUBSTRING(body, 1, 800)
               ) AS chunk_text
        FROM {config['enron_emails_table']}
        WHERE body IS NOT NULL AND LENGTH(body) > 50
        ORDER BY date_sent DESC
    """).collect()

    _enron_chunk_cache["chunks"] = [r["chunk_text"] for r in emails]
    return _enron_chunk_cache["chunks"]


_ENRON_EMBEDDINGS_TABLE = f"{config['catalog']}.{config['enron_schema']}.email_chunk_embeddings"


def _embed_enron_chunks():
    """Embed the full email chunk corpus.

    Cycle 8 / REQ-C8-02 / GAP-15: tries loading pre-computed embeddings
    from Delta table first. Falls back to computing via API + saving to Delta.
    """
    if _enron_chunk_cache["embeddings"] is not None:
        return _enron_chunk_cache["embeddings"]

    import numpy as np
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    try:
        emb_df = spark.table(_ENRON_EMBEDDINGS_TABLE)
        rows = emb_df.orderBy("chunk_index").select("embedding").collect()
        if rows:
            emb_matrix = np.array([r["embedding"] for r in rows], dtype=np.float32)
            _enron_chunk_cache["embeddings"] = emb_matrix
            print(f"Loaded {len(rows)} pre-computed embeddings from {_ENRON_EMBEDDINGS_TABLE}")
            return emb_matrix
    except Exception:
        pass

    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    chunks = _build_enron_email_chunks()

    all_embs = []
    batch_size = 16
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        resp = client.predict(endpoint=config['embedding_endpoint'], inputs={"input": batch})
        all_embs.extend([d["embedding"] for d in resp.data])

    emb_matrix = np.array(all_embs, dtype=np.float32)
    _enron_chunk_cache["embeddings"] = emb_matrix

    try:
        from pyspark.sql.types import StructType, StructField, IntegerType, ArrayType, FloatType
        schema = StructType([
            StructField("chunk_index", IntegerType(), False),
            StructField("embedding", ArrayType(FloatType()), False),
        ])
        rows = [(i, emb_matrix[i].tolist()) for i in range(len(emb_matrix))]
        emb_df = spark.createDataFrame(rows, schema)
        emb_df.write.format("delta").mode("overwrite").saveAsTable(_ENRON_EMBEDDINGS_TABLE)
        print(f"Saved {len(rows)} embeddings to {_ENRON_EMBEDDINGS_TABLE}")
    except Exception as e:
        print(f"Warning: could not persist embeddings to Delta: {e}")

    return emb_matrix


def _enron_flat_rag_retrieve(question: str, top_k: int = 5) -> str:
    """Full-corpus embedding retrieval over Enron email chunks.

    Cycle 7 / REQ-C7-01 / GAP-11: upgraded from 2000-email sample
    to full corpus with cached embeddings for fair baseline comparison.
    """
    import mlflow.deployments
    import numpy as np
    client = mlflow.deployments.get_deploy_client("databricks")
    q_resp = client.predict(endpoint=config['embedding_endpoint'], inputs={"input": [question]})
    q_vec = np.array(q_resp.data[0]["embedding"], dtype=np.float32)

    chunks = _build_enron_email_chunks()
    emb_matrix = _embed_enron_chunks()

    norms = np.linalg.norm(emb_matrix, axis=1) * np.linalg.norm(q_vec) + 1e-10
    sims = emb_matrix @ q_vec / norms
    top_idx = np.argsort(sims)[-top_k:][::-1]
    return "\n---\n".join(chunks[i] for i in top_idx)


def predict_enron_flat_rag(question: str) -> dict:
    """Flat RAG + 70B (embedding retrieval baseline)."""
    import mlflow.deployments
    context = _enron_flat_rag_retrieve(question)
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['llm_endpoint'],
        inputs={
            "messages": [
                {"role": "system", "content": (
                    "You are an Enron email analyst. Use ONLY the provided email excerpts "
                    "to answer the question. If the answer is not in the excerpts, say so."
                )},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}


def predict_enron_direct_llm(question: str) -> dict:
    """Direct LLM + 70B (no retrieval — parametric knowledge only)."""
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['llm_endpoint'],
        inputs={
            "messages": [
                {"role": "system", "content": (
                    "You are a corporate communications analyst. Answer questions about "
                    "Enron Corporation based on your training knowledge. Cite sources when "
                    "possible."
                )},
                {"role": "user", "content": question},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}


def predict_enron_direct_external(question: str) -> dict:
    """Direct External (frontier model — no retrieval)."""
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['external_llm_endpoint'],
        inputs={
            "messages": [
                {"role": "system", "content": (
                    "You are a corporate communications analyst. Answer questions about "
                    "Enron Corporation based on your knowledge. Cite sources when possible."
                )},
                {"role": "user", "content": question},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}


# COMMAND ----------

# DBTITLE 1,Run 5-Configuration Comparison
ENRON_CONFIGS = {
    "enron_graphrag_70b": predict_enron_graphrag_70b,
    "enron_graphrag_8b": predict_enron_graphrag_8b,
    "enron_flat_rag_70b": predict_enron_flat_rag,
    "enron_direct_llm_70b": predict_enron_direct_llm,
    "enron_direct_external": predict_enron_direct_external,
}

comparison_df = build_eval_dataframe(ENRON_EVAL_DATASET)
print(f"Full comparison dataset: {len(comparison_df)} questions across all categories (Cycle 7 / REQ-C7-02)")

enron_eval_results = {}
for name, fn in ENRON_CONFIGS.items():
    print(f"\n{'='*60}")
    print(f"  Evaluating: {name}")
    print(f"{'='*60}")
    enron_eval_results[name] = mlflow.genai.evaluate(
        data=comparison_df,
        predict_fn=fn,
        scorers=build_enron_scorers() + [citation_accuracy, provenance_structure_compliance, provenance_content_quality],
    )
    print(f"  Done — run_id: {enron_eval_results[name].run_id}")

# COMMAND ----------

# DBTITLE 1,Enron Governance Scorecard (5-config)
import pandas as pd

gov_metrics = ["evidence_quality", "grounding_integrity", "hallucination_detection", "citation_accuracy"]
gov_rows = []
for cname, result in enron_eval_results.items():
    row = {"Configuration": cname}
    for metric, value in sorted(result.metrics.items()):
        if metric.endswith("/mean"):
            short = metric.replace("/mean", "")
            if short in gov_metrics:
                row[short] = round(value, 3)
    gov_rows.append(row)

enron_gov_df = pd.DataFrame(gov_rows).set_index("Configuration")
print("\n=== Enron Governance Scorecard (5-config) ===")
display(enron_gov_df)

# COMMAND ----------

# DBTITLE 1,Enron Quality Scorecard (5-config)
quality_metrics = ["factual_accuracy", "organizational_accuracy", "answer_completeness", "participant_verification"]
qual_rows = []
for cname, result in enron_eval_results.items():
    row = {"Configuration": cname}
    for metric, value in sorted(result.metrics.items()):
        if metric.endswith("/mean"):
            short = metric.replace("/mean", "")
            if short in quality_metrics:
                row[short] = round(value, 3)
    qual_rows.append(row)

enron_qual_df = pd.DataFrame(qual_rows).set_index("Configuration")
print("\n=== Enron Quality Scorecard (5-config) ===")
display(enron_qual_df)

# COMMAND ----------

# DBTITLE 1,Enron Config Comparison Bar Chart
import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, 2, figsize=(18, 6))

enron_gov_df.T.plot(kind="bar", ax=axes[0], rot=15)
axes[0].set_ylabel("Score (0-1)")
axes[0].set_title("Enron Governance — 5-Config Comparison")
axes[0].set_ylim(0, 1.1)
axes[0].legend(fontsize=7, loc="lower right")

enron_qual_df.T.plot(kind="bar", ax=axes[1], rot=15)
axes[1].set_ylabel("Score (0-1)")
axes[1].set_title("Enron Quality — 5-Config Comparison")
axes[1].set_ylim(0, 1.1)
axes[1].legend(fontsize=7, loc="lower right")

plt.tight_layout()
plt.show()

# COMMAND ----------

# DBTITLE 1,Per-Category Breakdown (Cycle 7 / REQ-C7-02)
category_breakdown = {}
for cname, result in enron_eval_results.items():
    eval_table = result.tables["eval_results"]
    for _, row in eval_table.iterrows():
        cat = (row.get("expectations") or {}).get("category", "unknown") if isinstance(row.get("expectations"), dict) else "unknown"
        if cat not in category_breakdown:
            category_breakdown[cat] = {}
        if cname not in category_breakdown[cat]:
            category_breakdown[cat][cname] = []
        fa = row.get("factual_accuracy/value")
        if fa is not None:
            category_breakdown[cat][cname].append(float(fa))

print("\n=== Per-Category Factual Accuracy by Config ===")
for cat, configs in sorted(category_breakdown.items()):
    print(f"\n  {cat}:")
    for cname, scores in sorted(configs.items()):
        avg = sum(scores) / len(scores) if scores else 0
        print(f"    {cname:30s}: {avg:.3f} (n={len(scores)})")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Evaluation complete. Review the results in MLflow to identify areas for improvement.
# MAGIC
# MAGIC **Scorers used (Cycle 8):**
# MAGIC - `evidence_quality` — LLM judge: are claims backed by dates, emails, tool results?
# MAGIC - `participant_verification` — string match: are expected entities mentioned?
# MAGIC - `organizational_accuracy` — LLM judge: is the hierarchy correct?
# MAGIC - `grounding_integrity` — LLM judge: does agent distinguish graph vs external knowledge?
# MAGIC - `factual_accuracy` — LLM judge: does the response match what the graph contains?
# MAGIC - `hallucination_detection` — LLM judge: does the agent fabricate evidence?
# MAGIC - `answer_completeness` — LLM judge: does the response address the full question?
# MAGIC - `citation_accuracy` — LLM judge: do cited sources actually substantiate the claims?
# MAGIC - `latency_sla_compliance` — instrumentation: are tool latencies within SLA thresholds?
# MAGIC - `session_isolation_score` — LLM judge: detects indirect privilege extraction (Cycle 6)
# MAGIC - `provenance_structure_compliance` — regex: validates Answer/Provenance/Path/Sources/Grounding sections (Cycle 7)
# MAGIC - `provenance_content_quality` — LLM judge: validates provenance content (path connections, source specificity, grounding honesty) (Cycle 8)
