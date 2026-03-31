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

ENRON_EVAL_DATASET = [
    # ===== SINGLE-HOP FACTUAL (21 questions) =====
    {
        "inputs": {"question": "Who is Kenneth Lay?"},
        "expectations": {
            "expected_facts": ["Kenneth Lay was Chairman and CEO of Enron"],
            "expected_entities": ["Kenneth Lay"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What department did Vince Kaminski lead?"},
        "expectations": {
            "expected_facts": ["Vince Kaminski led the Research group"],
            "expected_entities": ["Vince Kaminski"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What was Andrew Fastow's title at Enron?"},
        "expectations": {
            "expected_facts": ["Andrew Fastow was CFO of Enron"],
            "expected_entities": ["Andrew Fastow"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who is Jeff Skilling?"},
        "expectations": {
            "expected_facts": ["Jeff Skilling was CEO/COO of Enron"],
            "expected_entities": ["Jeffrey Skilling"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What is Enron Broadband Services?"},
        "expectations": {
            "expected_facts": ["Enron Broadband Services was a division of Enron"],
            "expected_entities": ["Enron Broadband Services"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Sherron Watkins?"},
        "expectations": {
            "expected_facts": ["Sherron Watkins was VP Corporate Development at Enron"],
            "expected_entities": ["Sherron Watkins"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What entity type is Arthur Andersen in the knowledge graph?"},
        "expectations": {
            "expected_facts": ["Arthur Andersen is an Organization entity"],
            "expected_entities": ["Arthur Andersen"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Rick Buy at Enron?"},
        "expectations": {
            "expected_facts": ["Rick Buy was Chief Risk Officer at Enron"],
            "expected_entities": ["Rick Buy"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What is the entity type of Project Raptor?"},
        "expectations": {
            "expected_facts": ["Project Raptor is a Project-type entity"],
            "expected_entities": ["Project Raptor"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who did Kenneth Lay manage?"},
        "expectations": {
            "expected_facts": ["Lay managed multiple executives including Skilling"],
            "expected_entities": ["Kenneth Lay", "Jeffrey Skilling"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Tim Belden?"},
        "expectations": {
            "expected_facts": ["Tim Belden was involved in energy trading at Enron"],
            "expected_entities": ["Tim Belden"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "When did Jeff Skilling resign from Enron?"},
        "expectations": {
            "expected_facts": ["Skilling resigned in August 2001"],
            "expected_entities": ["Jeffrey Skilling"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What was Rosalee Fleming's role at Enron?"},
        "expectations": {
            "expected_facts": ["Rosalee Fleming was Kenneth Lay's executive assistant"],
            "expected_entities": ["Rosalee Fleming"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was David Delainey at Enron?"},
        "expectations": {
            "expected_facts": ["David Delainey was CEO of Enron Energy Services"],
            "expected_entities": ["David Delainey"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What is Enron Online?"},
        "expectations": {
            "expected_facts": ["Enron Online was Enron's electronic trading platform"],
            "expected_entities": ["Enron Online"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Greg Whalley?"},
        "expectations": {
            "expected_facts": ["Greg Whalley became President and COO"],
            "expected_entities": ["Greg Whalley"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What was Cliff Baxter's role?"},
        "expectations": {
            "expected_facts": ["Cliff Baxter was Vice Chairman of Enron"],
            "expected_entities": ["Cliff Baxter"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Michael Kopper?"},
        "expectations": {
            "expected_facts": ["Michael Kopper worked under Andrew Fastow in Global Finance"],
            "expected_entities": ["Michael Kopper"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "What were the LJM partnerships?"},
        "expectations": {
            "expected_facts": ["LJM was a special purpose entity connected to Andrew Fastow"],
            "expected_entities": ["Andrew Fastow"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "Who was Kenneth Rice?"},
        "expectations": {
            "expected_facts": ["Kenneth Rice was CEO of Enron Broadband Services"],
            "expected_entities": ["Kenneth Rice"],
            "category": "single_hop_factual",
        },
    },
    {
        "inputs": {"question": "When did Enron file for bankruptcy?"},
        "expectations": {
            "expected_facts": ["Enron filed for bankruptcy in December 2001"],
            "expected_entities": [],
            "category": "single_hop_factual",
        },
    },

    # ===== MULTI-HOP LINEAGE (21 questions) =====
    {
        "inputs": {"question": "Trace the reporting chain from Tim Belden to Kenneth Lay."},
        "expectations": {
            "expected_facts": [
                "Belden reported through the trading hierarchy",
                "The chain connects to Skilling",
                "Skilling reported to Lay",
            ],
            "expected_entities": ["Tim Belden", "Jeffrey Skilling", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How is Sherron Watkins connected to Kenneth Lay through the org hierarchy?"},
        "expectations": {
            "expected_facts": [
                "Watkins reported to Andrew Fastow",
                "Fastow reported to Jeff Skilling",
                "Skilling reported to Kenneth Lay",
            ],
            "expected_entities": ["Sherron Watkins", "Andrew Fastow", "Jeffrey Skilling", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How did information flow from Fastow's team to Arthur Andersen?"},
        "expectations": {
            "expected_facts": [
                "Fastow managed the finance team",
                "Finance team communicated with auditors",
            ],
            "expected_entities": ["Andrew Fastow", "Arthur Andersen"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Trace the path between Michael Kopper and Kenneth Lay."},
        "expectations": {
            "expected_facts": [
                "Kopper connected to Fastow",
                "Fastow connected to Skilling",
                "Skilling connected to Lay",
            ],
            "expected_entities": ["Michael Kopper", "Andrew Fastow", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How was Vince Kaminski connected to Kenneth Lay?"},
        "expectations": {
            "expected_facts": [
                "Kaminski reported to Rick Buy",
                "Buy reported to Skilling",
                "Skilling reported to Lay",
            ],
            "expected_entities": ["Vince Kaminski", "Rick Buy", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Who were the intermediaries between Kenneth Rice and Kenneth Lay?"},
        "expectations": {
            "expected_facts": ["Rice connected through Skilling to Lay"],
            "expected_entities": ["Kenneth Rice", "Jeffrey Skilling", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How are Project Raptor and Kenneth Lay connected?"},
        "expectations": {
            "expected_facts": [
                "Raptor connected to Fastow",
                "Fastow reported to Skilling",
                "Skilling reported to Lay",
            ],
            "expected_entities": ["Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Trace the chain of command from David Delainey to the CEO."},
        "expectations": {
            "expected_facts": [
                "Delainey reported to Jeff Skilling",
                "Skilling was CEO",
            ],
            "expected_entities": ["David Delainey", "Jeffrey Skilling"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How did Enron Broadband Services connect to the executive suite?"},
        "expectations": {
            "expected_facts": [
                "Rice managed EBS",
                "Rice connected to Skilling",
            ],
            "expected_entities": ["Kenneth Rice", "Jeffrey Skilling", "Enron Broadband Services"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "What is the shortest path between Cliff Baxter and Andrew Fastow?"},
        "expectations": {
            "expected_facts": [
                "Both reported to Skilling",
                "Connected through executive hierarchy",
            ],
            "expected_entities": ["Cliff Baxter", "Andrew Fastow"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How were the California energy operations connected to the C-suite?"},
        "expectations": {
            "expected_facts": [
                "Tim Belden in energy trading",
                "Connected to Delainey or Skilling",
            ],
            "expected_entities": ["Tim Belden"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Trace the path between Lou Pai and Kenneth Lay."},
        "expectations": {
            "expected_facts": [
                "Lou Pai reported to Skilling",
                "Skilling reported to Lay",
            ],
            "expected_entities": ["Lou Pai", "Jeffrey Skilling", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How did the whistleblower Watkins' concerns reach the board?"},
        "expectations": {
            "expected_facts": [
                "Watkins wrote to Kenneth Lay",
                "Watkins met with Lay in August 2001",
            ],
            "expected_entities": ["Sherron Watkins", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "What was the chain of events from Skilling's resignation to bankruptcy?"},
        "expectations": {
            "expected_facts": [
                "Skilling resigned August 2001",
                "Fastow removed October 2001",
                "Bankruptcy filed December 2001",
            ],
            "expected_entities": ["Jeffrey Skilling", "Andrew Fastow"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How was the Dabhol Power project connected to the executive team?"},
        "expectations": {
            "expected_facts": ["Dabhol connected to executive entities in the graph"],
            "expected_entities": [],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How did Greg Whalley connect to the existing executive structure?"},
        "expectations": {
            "expected_facts": [
                "Whalley became President and COO",
                "Connected to Lay after Skilling departure",
            ],
            "expected_entities": ["Greg Whalley", "Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "What executive chain approved the Raptor structures?"},
        "expectations": {
            "expected_facts": [
                "Fastow was primary architect",
                "Fastow reported to Skilling",
            ],
            "expected_entities": ["Andrew Fastow", "Jeffrey Skilling"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How was Rick Causey connected to the auditing process?"},
        "expectations": {
            "expected_facts": ["Causey connected to accounting and auditing entities"],
            "expected_entities": [],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Map the organizational path from Enron Energy Trading to Kenneth Lay."},
        "expectations": {
            "expected_facts": [
                "Energy Trading managed by Delainey or Skilling",
                "Connected to Lay through executive hierarchy",
            ],
            "expected_entities": ["Kenneth Lay"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "How did the SEC investigation connect to specific Enron executives?"},
        "expectations": {
            "expected_facts": [
                "SEC inquiry started October 2001",
                "Connected to Lay and Fastow",
            ],
            "expected_entities": ["Kenneth Lay", "Andrew Fastow"],
            "category": "multi_hop_lineage",
        },
    },
    {
        "inputs": {"question": "Trace how concerns about mark-to-market accounting reached the C-suite."},
        "expectations": {
            "expected_facts": ["Accounting concerns connected to senior executives through communications"],
            "expected_entities": [],
            "category": "multi_hop_lineage",
        },
    },

    # ===== CROSS-DOCUMENT SYNTHESIS (21 questions) =====
    {
        "inputs": {"question": "Compare communication patterns between the legal team and executive team in 2001."},
        "expectations": {
            "expected_facts": [
                "Communication patterns differed between teams",
                "Executive emails had different recipients and volumes",
            ],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What topics did both Jeff Skilling and Andrew Fastow discuss with external parties?"},
        "expectations": {
            "expected_facts": [
                "Both connected to partnership entities",
                "Both connected to financial topics",
            ],
            "expected_entities": ["Jeffrey Skilling", "Andrew Fastow"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How do the communication networks of Kenneth Lay and Jeff Skilling differ?"},
        "expectations": {
            "expected_facts": [
                "Lay had different top contacts than Skilling",
                "Different communication volumes and patterns",
            ],
            "expected_entities": ["Kenneth Lay", "Jeffrey Skilling"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What were the overlapping discussion topics between Lay's and Fastow's email networks?"},
        "expectations": {
            "expected_facts": ["Both discussed financial structures or company strategy"],
            "expected_entities": ["Kenneth Lay", "Andrew Fastow"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Compare the organizational influence of Andrew Fastow and Jeff Skilling based on their graph connections."},
        "expectations": {
            "expected_facts": [
                "Skilling had more MANAGES relationships",
                "Fastow was connected to SPE entities",
            ],
            "expected_entities": ["Andrew Fastow", "Jeffrey Skilling"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What entities are connected to both Arthur Andersen and Andrew Fastow?"},
        "expectations": {
            "expected_facts": ["Both connected to audit or finance entities"],
            "expected_entities": ["Arthur Andersen", "Andrew Fastow"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How did the crisis affect communication patterns across all executive tiers?"},
        "expectations": {
            "expected_facts": [
                "Communication patterns shifted in late 2001",
                "Crisis period showed increased email volume",
            ],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Which people appear as connectors between multiple organizational divisions?"},
        "expectations": {
            "expected_facts": ["Some individuals bridge multiple division entities"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What was the relationship between California energy trading and the financial engineering that led to Enron's collapse?"},
        "expectations": {
            "expected_facts": [
                "Trading operations and SPEs were connected through executive hierarchy",
                "Both involved significant financial risk",
            ],
            "expected_entities": ["Tim Belden", "Andrew Fastow"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How do the PageRank scores correlate with actual organizational influence?"},
        "expectations": {
            "expected_facts": [
                "High PageRank entities tend to be senior executives",
                "PageRank reflects communication centrality",
            ],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What was the timeline overlap between SPE activity and executive departures?"},
        "expectations": {
            "expected_facts": [
                "Executives departed in 2001",
                "SPE activity was concurrent with departures",
            ],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Compare Enron's internal communications before and after the SEC inquiry in October 2001."},
        "expectations": {
            "expected_facts": ["Communication patterns shifted around the SEC inquiry"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Who had the most diverse communication network spanning different departments?"},
        "expectations": {
            "expected_facts": ["Senior executives communicated across more departments"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How did the whistleblower's concerns relate to the ongoing SPE management?"},
        "expectations": {
            "expected_facts": [
                "Watkins raised concerns about accounting",
                "SPE structures were central to the concerns",
            ],
            "expected_entities": ["Sherron Watkins", "Andrew Fastow"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What patterns of information asymmetry are visible between executive tiers in the graph?"},
        "expectations": {
            "expected_facts": ["Different tiers had different access to information based on communication patterns"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How did Enron's external relationships (Andersen, Dynegy) correlate with internal crisis events?"},
        "expectations": {
            "expected_facts": [
                "Andersen connected to audit events",
                "Dynegy connected to the failed merger",
            ],
            "expected_entities": ["Arthur Andersen"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What entities bridge the financial operations and energy trading sides of Enron?"},
        "expectations": {
            "expected_facts": ["Some executives connected both domains"],
            "expected_entities": ["Jeffrey Skilling"],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "How do the communication frequencies between executives compare to their organizational distance?"},
        "expectations": {
            "expected_facts": ["Closer organizational relationships tend to have higher communication frequency"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "What was the relationship between the investigation timeline events and changes in the org hierarchy?"},
        "expectations": {
            "expected_facts": [
                "Executive departures correlated with investigation milestones",
                "Org hierarchy changed as executives left",
            ],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Which topics show the highest concentration of cross-department communication?"},
        "expectations": {
            "expected_facts": ["Financial and strategic topics crossed department boundaries"],
            "expected_entities": [],
            "category": "cross_document_synthesis",
        },
    },
    {
        "inputs": {"question": "Synthesize the evidence about Enron's governance failures from the knowledge graph."},
        "expectations": {
            "expected_facts": [
                "Multiple governance indicators visible in the graph",
                "Executive conflicts of interest",
                "Auditor relationships",
            ],
            "expected_entities": ["Andrew Fastow", "Arthur Andersen"],
            "category": "cross_document_synthesis",
        },
    },
]

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
