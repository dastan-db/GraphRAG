"""Local evaluation harness for the Enron GraphRAG agent.

Runs the same evaluation as notebook 08_Enron_Evaluation but against an
in-process GraphRAGAgent instead of a deployed Model Serving endpoint.
Uses Databricks APIs directly (Statement Execution, Foundation Model, Genie).

Usage:
    python scripts/eval_local.py                           # full 30-question eval
    python scripts/eval_local.py --cases 5                 # quick 5-question subset
    python scripts/eval_local.py --category org_hierarchy  # one category only
    python scripts/eval_local.py --judge gpt-4o            # different judge model
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

os.environ.setdefault("GRAPHRAG_BACKEND", "databricks")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer
from mlflow.types.responses import ResponsesAgentRequest

from src.agent.agent_serving import GraphRAGAgent
from src.evaluation.enron_evaluation import DATA_CONTEXT
from src.evaluation.question_bank import ENRON_CORE_EVAL_DATA

# ---------------------------------------------------------------------------
# Agent predict function (in-process, no endpoint needed)
# ---------------------------------------------------------------------------
_AGENT = None


def _get_agent() -> GraphRAGAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = GraphRAGAgent()
    return _AGENT


def predict_fn(question: str) -> str:
    """Query the in-process Enron GraphRAGAgent and return the response text."""
    agent = _get_agent()
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": question}]
    )
    try:
        response = agent.predict(request)
        texts = []
        for item in response.output:
            item_d = item.model_dump() if hasattr(item, "model_dump") else item
            if item_d.get("type") == "message":
                for part in item_d.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            elif isinstance(item_d, dict) and "text" in item_d:
                texts.append(item_d["text"])
        return "\n".join(texts) if texts else str(response)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Judge (same as notebook 08 — calls Foundation Model API via WorkspaceClient)
# ---------------------------------------------------------------------------
JUDGE_ENDPOINT = os.environ.get(
    "GRAPHRAG_JUDGE_ENDPOINT", "databricks-claude-sonnet-4-6"
)


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
    result_text = resp["choices"][0]["message"]["content"].strip()
    if result_text.startswith("```"):
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
        result_text = re.sub(r"\s*```$", "", result_text)
    return json.loads(result_text)


# ---------------------------------------------------------------------------
# Eval dataset — canonical bank view
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
    return Feedback(
        value=score,
        rationale=f"Found {len(found)}/{len(expected_entities)}: {found}. Missing: {missing}",
    )


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


@scorer
def provenance_completeness(inputs, outputs, expectations=None):
    """Check that responses include verifiable email citations."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required for this question")
    citation_pattern = re.compile(
        r'\[\d{4}-\d{2}-\d{2},\s*From:.*?,\s*Subject:.*?\]'
        r'|'
        r'\d{4}-\d{2}-\d{2}\s*\|?\s*\S+@\S+\s*\|?\s*.{5,}'
    )
    table_pattern = re.compile(r'\|\s*\d+\s*\|\s*\d{4}-\d{2}-\d{2}')
    citations = citation_pattern.findall(text)
    table_rows = table_pattern.findall(text)
    total = len(citations) + len(table_rows)
    if total >= 3:
        score = 1.0
    elif total == 2:
        score = 0.8
    elif total == 1:
        score = 0.5
    else:
        score = 0.1
    return Feedback(value=score, rationale=f"Found {len(citations)} inline citations, {len(table_rows)} table rows")


@scorer
def citation_accuracy(inputs, outputs, expectations=None):
    """Verify that cited emails have valid date/sender/subject format."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")
    prompt = f"""{DATA_CONTEXT}

Evaluate the ACCURACY of email citations in this response. Check:
1. Do cited dates fall within the Enron corpus period (1999-2002)?
2. Do cited senders appear to be real Enron employees (not fabricated)?
3. Are cited subject lines specific and plausible?
4. Does each citation support the claim it's attached to?
5. If no citations are present, that itself is a failure.

Scoring rubric (0.0 to 1.0):
- 1.0: All citations have valid dates (1999-2002), real senders, specific subjects.
- 0.7: Most citations valid; one or two imprecise.
- 0.5: Some valid, others suspicious.
- 0.3: Few valid citations.
- 0.0: No citations, or all fabricated.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def retrieval_relevance(inputs, outputs, expectations=None):
    """Judge whether evidence is relevant to the specific question."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    prompt = f"""{DATA_CONTEXT}

Evaluate whether the evidence cited in this response is RELEVANT to the question.
Irrelevant evidence includes: mass newsletters, unrelated topics, emails between unrelated people.

User Question: {question}

Scoring rubric (0.0 to 1.0):
- 1.0: All cited evidence directly supports answering the question.
- 0.7: Most evidence relevant; one or two tangential.
- 0.5: Mixed relevant and irrelevant evidence.
- 0.3: Most evidence tangential.
- 0.0: No evidence or all irrelevant.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


# ---------------------------------------------------------------------------
# Corroboration scorers — cross-tool consistency and fabrication detection
# ---------------------------------------------------------------------------

@scorer
def cross_tool_consistency(inputs, outputs, expectations=None):
    """Check that the response doesn't contradict itself across different data sources.

    Detects: claiming N emails exist then saying no emails found, reporting
    a contact as #1 then failing to find emails with that contact, etc.
    """
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response is INTERNALLY CONSISTENT — that is, whether
different parts of the answer agree with each other.

Look for these contradictions:
1. Claiming a specific number of emails (e.g. "6 emails") but then saying "no emails found"
2. Naming someone as a top contact but having no evidence of their communication
3. Stating a relationship exists (e.g. "SENT_TO") but then showing no supporting emails
4. The provenance section contradicting claims in the main body
5. The evidence table containing emails that weren't mentioned by any tool

Score 1.0 if the response is fully self-consistent.
Score 0.5 if there are minor inconsistencies that don't affect the main answer.
Score 0.0 if there are major contradictions (e.g. "6 emails" then "no emails found").

Agent Response:
{text[:4000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def evidence_fabrication(inputs, outputs, expectations=None):
    """Detect fabricated evidence — citations that don't match any tool output."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    citation_pattern = re.compile(
        r'\[?\d{4}-\d{2}-\d{2},?\s*From:.*?(?:Subject|Re):.*?\]?', re.IGNORECASE
    )
    citations = citation_pattern.findall(text)
    if not citations:
        evidence_required = (expectations or {}).get("evidence_required", True)
        if not evidence_required:
            return Feedback(value=1.0, rationale="No citations needed and none present")
        has_no_evidence_statement = any(
            phrase in text.lower()
            for phrase in ["no emails found", "no direct emails", "no email evidence", "not found"]
        )
        if has_no_evidence_statement:
            return Feedback(value=1.0, rationale="Correctly states no evidence found")
        return Feedback(value=0.5, rationale="No citations found but evidence was expected")

    prompt = f"""{DATA_CONTEXT}

The agent cited {len(citations)} pieces of email evidence in its response.
Evaluate whether these citations appear to be REAL (grounded in tool data) or FABRICATED.

Signs of fabrication:
- Dates, subjects, or senders that seem invented (too specific without supporting context)
- Citations that appear in a "Supporting Evidence" table but weren't mentioned in the tool results
- Emails attributed to people who weren't queried by any tool
- Suspiciously clean/perfect evidence that covers every claim exactly

Signs of real evidence:
- Dates consistent with 2000-2002 Enron email corpus
- Senders/recipients that match @enron.com patterns
- Subjects that feel like real email subject lines
- Evidence that has gaps or imperfect coverage (realistic)

Score 1.0 if all citations appear grounded.
Score 0.5 if some citations are questionable.
Score 0.0 if citations appear fabricated.

Agent Response (with citations):
{text[:4000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def spelling_correction_transparency(inputs, outputs, expectations=None):
    """For questions with typos, check if the agent surfaces the correction."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    category = (expectations or {}).get("category", "")
    if category != "corroboration":
        return Feedback(value=1.0, rationale="Not a corroboration question — skipping")

    typo_pairs = {
        "dassovich": "dasovich",
        "jeffery": "jeffrey",
        "fasttow": "fastow",
    }

    q_lower = question.lower()
    has_typo = False
    correct_form = None
    for typo, correct in typo_pairs.items():
        if typo in q_lower:
            has_typo = True
            correct_form = correct
            break

    if not has_typo:
        return Feedback(value=1.0, rationale="No typo in question — skipping")

    text_lower = text.lower()
    mentions_correction = any(
        phrase in text_lower
        for phrase in ["corrected to", "did you mean", "correcting", "note:", "spelling"]
    )
    found_correct = correct_form and correct_form in text_lower

    if mentions_correction and found_correct:
        return Feedback(value=1.0, rationale="Transparently corrected the typo")
    if found_correct:
        return Feedback(value=0.5, rationale="Found correct entity but didn't mention the correction")
    return Feedback(value=0.0, rationale="Failed to resolve the typo or find the correct entity")


# ---------------------------------------------------------------------------
# Genie routing scorers — conciseness, directional precision, routing quality
# ---------------------------------------------------------------------------

@scorer
def response_conciseness(inputs, outputs, expectations=None):
    """Penalize verbose, repetitive responses for simple tabular questions."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    word_count = len(text.split())
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response is CONCISE and avoids redundancy.

User Question: {question}
Response word count: {word_count}

Check for these verbosity problems:
1. The SAME fact restated in multiple sections (e.g., email count repeated in body, table header, and provenance)
2. Boilerplate suggestions the user didn't ask for ("To see all emails, ask...")
3. Sections that add no new information beyond what earlier sections already covered
4. For simple count/ranking questions: does the response exceed 300 words when a table + one sentence would suffice?

Scoring rubric (0.0 to 1.0):
- 1.0: Each fact stated once, no redundant sections, proportional length to question complexity.
- 0.7: Minor repetition (1-2 restated facts) but generally concise.
- 0.5: Noticeable repetition across sections, some unnecessary boilerplate.
- 0.3: Significant redundancy — same facts appear 3+ times, excessive boilerplate.
- 0.0: Extremely verbose, most content is restated or filler.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def directional_precision(inputs, outputs, expectations=None):
    """Check that 'sent' vs 'received' vs 'exchanged' matches actual data direction."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    category = (expectations or {}).get("category", "")
    if category not in ("genie_routing", "communication", "entity_pair_evidence", "corroboration"):
        return Feedback(value=1.0, rationale="Not a directional question — skipping")

    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response uses DIRECTIONALLY PRECISE language about email communication.

User Question: {question}
Graph Ground Truth: {graph_gt}

Rules:
- "exchanged" or "between" implies BIDIRECTIONAL communication (both A->B and B->A)
- "sent" or "sent to" implies ONE-DIRECTIONAL (A->B only)
- "received from" implies ONE-DIRECTIONAL (B->A only)
- If all emails go one direction, the response MUST NOT say "exchanged" — it should say "sent" or "received"
- If the response includes directional counts (e.g., "15 from A to B, 3 from B to A"), that's precise
- If the tool data shows one-directional communication but the response says "exchanged", that's wrong

Scoring rubric (0.0 to 1.0):
- 1.0: Directional language matches the actual data. Counts are attributed to correct direction.
- 0.7: Mostly correct but one imprecise term (e.g., "exchanged" when direction is 90/10 split).
- 0.5: Mixed — some directional claims correct, others vague or wrong.
- 0.3: Uses "exchanged" for clearly one-directional data, or reverses the direction.
- 0.0: Completely wrong direction or fabricated directional claims.

Agent Response:
{text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string)."""
    try:
        parsed = _call_judge(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def routing_appropriateness(inputs, outputs, expectations=None):
    """For genie_routing questions, check if the response looks like clean tabular output."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    category = (expectations or {}).get("category", "")
    if category != "genie_routing":
        return Feedback(value=1.0, rationale="Not a genie_routing question — skipping")

    word_count = len(text.split())
    has_table = bool(re.search(r'\|.*\|.*\|', text))
    has_sql_artifact = any(kw in text.lower() for kw in ["select ", "group by", "order by", "query_and_enrich", "genie"])
    has_provenance = "provenance" in text.lower() or "sources:" in text.lower()
    section_count = text.count("##")

    if has_sql_artifact or (has_table and word_count < 400):
        score = 1.0
        rationale = "Response appears Genie-routed: clean tabular output"
    elif has_table and word_count < 600:
        score = 0.7
        rationale = f"Has table but somewhat verbose ({word_count} words)"
    elif has_table:
        score = 0.5
        rationale = f"Has table but excessively verbose ({word_count} words, {section_count} sections)"
    elif word_count > 500:
        score = 0.2
        rationale = f"No table, verbose narrative ({word_count} words) — likely mis-routed to graph tools"
    else:
        score = 0.3
        rationale = f"No table output ({word_count} words) — may not have routed to Genie"

    return Feedback(value=score, rationale=rationale)


ALL_SCORERS = [
    evidence_quality,
    participant_verification,
    organizational_accuracy,
    grounding_integrity,
    factual_accuracy,
    hallucination_detection,
    answer_completeness,
    provenance_completeness,
    citation_accuracy,
    retrieval_relevance,
    cross_tool_consistency,
    evidence_fabrication,
    spelling_correction_transparency,
    response_conciseness,
    directional_precision,
    routing_appropriateness,
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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


def run_local_evaluation(
    *,
    cases: int | None = None,
    category: str | None = None,
    split: str | None = None,
    judge: str | None = None,
    run_name: str = "local_eval",
    output_json: str | None = None,
) -> dict[str, Any]:
    global JUDGE_ENDPOINT
    if judge:
        JUDGE_ENDPOINT = judge

    data = _filter_eval_data(cases=cases, category=category, split=split)
    if not data:
        raise ValueError("No evaluation questions matched the requested filters.")

    eval_df = pd.DataFrame(_build_eval_records(data))
    print(f"Evaluation: {len(eval_df)} questions | judge={JUDGE_ENDPOINT}")
    print(f"Backend: {os.environ.get('GRAPHRAG_BACKEND', 'databricks')}")
    print(f"Corpus: {os.environ.get('GRAPHRAG_CORPUS', 'enron')}")
    print()

    started = time.time()
    with mlflow.start_run(run_name=run_name):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=predict_fn,
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
        print("=== Enron GraphRAG Governance Scores (v2) ===")
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

        if overall_score >= 0.58:
            print(f"\n  TARGET MET ({overall_score:.2f} >= 0.58) — ready to deploy!")
        else:
            print(f"\n  Below target ({overall_score:.2f} < 0.58) — keep iterating.")
    else:
        print("No score columns found in results.")

    payload = {
        "version": "1.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        "backend": os.environ.get("GRAPHRAG_BACKEND", "databricks"),
        "corpus": os.environ.get("GRAPHRAG_CORPUS", "enron"),
    }
    if output_json:
        Path(output_json).resolve().write_text(json.dumps(payload, indent=2))
    return payload


def main():
    parser = argparse.ArgumentParser(description="Local Enron GraphRAG evaluation")
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--split", type=str, default=None, choices=["train", "test", "holdout"], help="Filter by eval split")
    parser.add_argument("--judge", type=str, default=None, help="Judge endpoint name")
    parser.add_argument("--run-name", type=str, default="local_eval", help="MLflow run name")
    parser.add_argument("--output-json", type=str, default=None, help="Optional JSON summary path")
    args = parser.parse_args()

    payload = run_local_evaluation(
        cases=args.cases,
        category=args.category,
        split=args.split,
        judge=args.judge,
        run_name=args.run_name,
        output_json=args.output_json,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
