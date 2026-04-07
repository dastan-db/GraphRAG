"""Multi-Agent Iterative Improvement Loop for GraphRAG Evidence Retrieval.

Three-agent self-improvement architecture:
- Proposer:  Generates increasingly challenging evidence-focused questions,
             escalating requirements each cycle based on failure analysis.
- Executor:  Applies targeted code fixes to the agent (tools, prompt, config)
             driven by the failure modes the Proposer surfaced.
- Judge:     Evaluates agent quality via LLM-as-judge scorers on train/test/holdout
             splits, tracks marginal gains, and declares plateau to end the loop.

Usage:
    python scripts/improvement_loop.py                       # full loop (default 10 cycles max)
    python scripts/improvement_loop.py --max-cycles 5        # cap at 5 cycles
    python scripts/improvement_loop.py --baseline-only        # baseline eval only
    python scripts/improvement_loop.py --holdout-only         # final holdout eval
    python scripts/improvement_loop.py --cases 5              # quick: 5 questions per split
"""
import argparse
import copy
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

from src.agent.agent_serving import GraphRAGAgent
from src.evaluation.question_bank import IMPROVEMENT_SEED_QUESTIONS

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
JUDGE_ENDPOINT = os.environ.get("GRAPHRAG_JUDGE_ENDPOINT", "databricks-claude-sonnet-4-6")
PROPOSER_ENDPOINT = os.environ.get("GRAPHRAG_PROPOSER_ENDPOINT", "databricks-claude-sonnet-4-6")

PLATEAU_WINDOW = 2
PLATEAU_THRESHOLD = 1.5  # minimum marginal gain (pp) to not be considered plateau
TRAIN_RATIO = 0.6
TEST_RATIO = 0.2
HOLDOUT_RATIO = 0.2

# ---------------------------------------------------------------------------
# Agent predict function (in-process)
# ---------------------------------------------------------------------------
_AGENT: Optional[GraphRAGAgent] = None


def _get_agent() -> GraphRAGAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = GraphRAGAgent()
    return _AGENT


def _reload_agent():
    """Force-reload the agent (after code changes)."""
    global _AGENT
    _AGENT = None
    import importlib
    import src.agent.agent_serving as mod
    importlib.reload(mod)
    from src.agent.agent_serving import GraphRAGAgent as Cls
    _AGENT = Cls()
    return _AGENT


def predict_fn(question: str) -> str:
    from mlflow.types.responses import ResponsesAgentRequest
    agent = _get_agent()
    request = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
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
# LLM Callers (Judge + Proposer)
# ---------------------------------------------------------------------------
def _call_llm(endpoint: str, prompt: str, max_tokens: int = 1024) -> str:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        },
    )
    return resp["choices"][0]["message"]["content"].strip()


def _call_judge_json(prompt: str) -> dict:
    text = _call_llm(JUDGE_ENDPOINT, prompt, max_tokens=512)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ---------------------------------------------------------------------------
# Seed Dataset — canonical bank view
# ---------------------------------------------------------------------------
SEED_QUESTIONS = IMPROVEMENT_SEED_QUESTIONS

# ---------------------------------------------------------------------------
# Data Context (shared by all scorers)
# ---------------------------------------------------------------------------
DATA_CONTEXT = """CRITICAL CONTEXT: The agent is a QA system built on a knowledge graph derived from ~20,000 Enron emails (2000-2002). It can ONLY access:
1. Email content and metadata from the corpus
2. Entities and relationships extracted from those emails
3. A curated org hierarchy table (24 entries from public record)
4. A curated investigation timeline (28 events from public record)
5. Pre-aggregated communication statistics (dyads, person activity)

Do NOT penalize the agent for:
- Missing facts that require external sources
- Saying "not found in graph" when data genuinely isn't there

DO penalize the agent for:
- Fabricating email citations not supported by data
- Claiming "no email evidence" without trying evidence tools
- Truncating evidence so severely it becomes uninformative"""

# ---------------------------------------------------------------------------
# Scorers (evidence-focused subset + new evidence-depth scorer)
# ---------------------------------------------------------------------------
@scorer
def evidence_quality(inputs, outputs, expectations=None):
    """LLM judge: are claims backed by specific email evidence?"""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")
    prompt = f"""{DATA_CONTEXT}

Evaluate whether this response provides sufficient EVIDENCE for its claims.
Strong evidence: specific email dates, sender/recipient, Subject lines, email body quotes.

Scoring (0.0 to 1.0):
- 1.0: Most claims backed by specific emails (date, sender, subject, body snippet).
- 0.7: Key claims have evidence, some minor claims unsupported.
- 0.5: Some evidence but many claims lack support.
- 0.3: Minimal evidence. Mostly assertions.
- 0.0: No evidence at all, or response is an error.

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def evidence_depth(inputs, outputs, expectations=None):
    """LLM judge: does the evidence include actual email body content, not just metadata?"""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")
    prompt = f"""{DATA_CONTEXT}

Evaluate the DEPTH of email evidence in this response.
Deep evidence means the response includes:
1. Actual email body text/quotes (not just "an email was found")
2. Specific content from the email that supports the claim
3. Multiple pieces of evidence cross-referenced

Shallow evidence means:
1. Only metadata (date, sender, subject) without body content
2. Generic statements like "emails were found" without specifics
3. Claims of evidence without showing it

Scoring (0.0 to 1.0):
- 1.0: Response quotes email body text, shows specific content that proves claims.
- 0.7: Has some body snippets, but key claims rely on metadata only.
- 0.5: Mix of deep and shallow evidence.
- 0.3: Almost entirely metadata-based — dates and subjects but no body content.
- 0.0: No evidence depth at all.

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def evidence_traceability(inputs, outputs, expectations=None):
    """LLM judge: can each claim be traced to a specific source?"""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required")
    prompt = f"""{DATA_CONTEXT}

Evaluate the TRACEABILITY of claims in this response.
Good traceability means:
1. Each factual claim has a [date, from, subject] citation
2. The provenance section lists specific tool calls and what they returned
3. Confidence is calibrated honestly (Low when evidence is weak)

Poor traceability means:
1. Claims without any citation
2. Vague provenance ("data was retrieved")
3. Overconfident claims with weak evidence

Scoring (0.0 to 1.0):
- 1.0: Every claim traceable to a specific email or tool result.
- 0.7: Most claims traceable, 1-2 uncited assertions.
- 0.5: Half the claims traceable.
- 0.3: Few claims have traceable sources.
- 0.0: No traceability.

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def participant_verification(inputs, outputs, expectations=None):
    """Deterministic: are expected entities mentioned in the response?"""
    expected = (expectations or {}).get("expected_entities", [])
    if not expected:
        return Feedback(value=1.0, rationale="No expected entities")
    text = (outputs if isinstance(outputs, str) else str(outputs)).lower()
    found = []
    for entity in expected:
        if entity.lower() in text:
            found.append(entity)
        elif " " in entity and entity.split()[-1].lower() in text:
            found.append(entity)
    score = round(len(found) / len(expected), 2)
    missing = [e for e in expected if e not in found]
    return Feedback(value=score, rationale=f"Found {len(found)}/{len(expected)}. Missing: {missing}")


@scorer
def evidence_fabrication(inputs, outputs, expectations=None):
    """LLM judge: does the response fabricate evidence?"""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    prompt = f"""{DATA_CONTEXT}

Check if this response FABRICATES evidence. Signs of fabrication:
1. Email citations with suspiciously perfect details not from tool results
2. Claiming specific email dates/subjects that seem invented
3. "Supporting Evidence Table" with rows that don't match any tool output
4. Quoting email body text that seems generated rather than retrieved

Note: The agent sometimes says "No email evidence was retrieved" which is HONEST, not fabrication.

Scoring (0.0 to 1.0):
- 1.0: No fabrication detected. All evidence appears genuine or honestly absent.
- 0.7: Minor concern — one citation seems questionable.
- 0.3: Multiple citations appear fabricated.
- 0.0: Blatant evidence fabrication.

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_judge_json(prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


ALL_SCORERS = [
    evidence_quality,
    evidence_depth,
    evidence_traceability,
    participant_verification,
    evidence_fabrication,
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class CycleResult:
    cycle: int
    overall_score: float
    scorer_scores: dict
    worst_questions: list
    num_questions: int
    elapsed_s: float
    split: str = "train"

@dataclass
class LoopState:
    cycle: int = 0
    history: list = field(default_factory=list)
    train_set: list = field(default_factory=list)
    test_set: list = field(default_factory=list)
    holdout_set: list = field(default_factory=list)
    proposer_questions: list = field(default_factory=list)
    plateau_declared: bool = False


# ---------------------------------------------------------------------------
# Judge Agent
# ---------------------------------------------------------------------------
class JudgeAgent:
    """Evaluates agent quality and detects plateau."""

    def __init__(self, scorers=None):
        self.scorers = scorers or ALL_SCORERS

    def create_splits(self, questions: list, seed: int = 42) -> tuple[list, list, list]:
        """Split questions into train/test/holdout using canonical eval_split when available."""
        if questions and all(isinstance(q, dict) and q.get("eval_split") in {"train", "test", "holdout"} for q in questions):
            train = [q for q in questions if q.get("eval_split") == "train"]
            test = [q for q in questions if q.get("eval_split") == "test"]
            holdout = [q for q in questions if q.get("eval_split") == "holdout"]
            if not holdout:
                holdout = test[-1:]
            print(f"  Splits: train={len(train)}, test={len(test)}, holdout={len(holdout)}")
            return train, test, holdout

        rng = random.Random(seed)
        shuffled = list(questions)
        rng.shuffle(shuffled)
        n = len(shuffled)
        n_train = max(1, int(n * TRAIN_RATIO))
        n_test = max(1, int(n * TEST_RATIO))
        train = shuffled[:n_train]
        test = shuffled[n_train:n_train + n_test]
        holdout = shuffled[n_train + n_test:]
        if not holdout:
            holdout = test[-1:]
        print(f"  Splits: train={len(train)}, test={len(test)}, holdout={len(holdout)}")
        return train, test, holdout

    def evaluate(self, questions: list, split_name: str = "train",
                 max_cases: int | None = None) -> CycleResult:
        """Run MLflow evaluation on a question set."""
        data = questions[:max_cases] if max_cases else questions
        eval_records = []
        for row in data:
            eval_records.append({
                "inputs": {"question": row["question"]},
                "expectations": {
                    "expected_entities": row.get("expected_entities", []),
                    "graph_ground_truth": row.get("graph_ground_truth", ""),
                    "historical_ground_truth": row.get("historical_ground_truth", ""),
                    "evidence_required": row.get("evidence_required", True),
                    "category": row.get("category", ""),
                },
            })

        eval_df = pd.DataFrame(eval_records)
        print(f"\n  Judge: evaluating {len(eval_df)} questions ({split_name}) ...")
        t0 = time.time()

        with mlflow.start_run(run_name=f"improvement_{split_name}"):
            results = mlflow.genai.evaluate(
                data=eval_df,
                predict_fn=predict_fn,
                scorers=self.scorers,
            )

        elapsed = time.time() - t0
        results_df = results.tables["eval_results"]

        scorer_names = {s.name if hasattr(s, "name") else s.__name__ for s in self.scorers}
        score_cols = []
        for c in results_df.columns:
            if not c.endswith("/value"):
                continue
            col_name = c.replace("/value", "")
            if col_name not in scorer_names:
                continue
            try:
                results_df[c] = pd.to_numeric(results_df[c], errors="coerce")
                if results_df[c].notna().any():
                    score_cols.append(c)
            except Exception:
                pass

        scorer_scores = {}
        if score_cols:
            means = results_df[score_cols].mean()
            for col in score_cols:
                name = col.replace("/value", "")
                scorer_scores[name] = round(float(means[col]), 3)
            overall = round(float(means.mean()), 3)
        else:
            overall = 0.0

        if score_cols:
            numeric_df = results_df[score_cols].apply(pd.to_numeric, errors="coerce")
            results_df["avg_score"] = numeric_df.mean(axis=1).astype(float)
        else:
            results_df["avg_score"] = 0.0

        worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
        worst_qs = []
        for _, row in worst.iterrows():
            q = row.get("inputs/question", row.get("inputs", ""))
            if isinstance(q, dict):
                q = q.get("question", str(q))
            worst_qs.append({
                "question": str(q)[:120],
                "avg_score": round(float(row["avg_score"]), 3),
            })

        result = CycleResult(
            cycle=0,
            overall_score=overall,
            scorer_scores=scorer_scores,
            worst_questions=worst_qs,
            num_questions=len(data),
            elapsed_s=round(elapsed, 1),
            split=split_name,
        )
        return result

    def check_plateau(self, history: list[CycleResult]) -> tuple[bool, float]:
        """Check if marginal gain has plateaued."""
        if len(history) < PLATEAU_WINDOW + 1:
            return False, float("inf")

        recent_gains = []
        for i in range(-PLATEAU_WINDOW, 0):
            gain = (history[i].overall_score - history[i - 1].overall_score) * 100
            recent_gains.append(gain)

        avg_gain = sum(recent_gains) / len(recent_gains)
        is_plateau = all(g < PLATEAU_THRESHOLD for g in recent_gains)
        return is_plateau, avg_gain


# ---------------------------------------------------------------------------
# Proposer Agent
# ---------------------------------------------------------------------------
class ProposerAgent:
    """Generates increasingly challenging evidence-focused questions."""

    ESCALATION_TEMPLATES = [
        # Level 1: Basic evidence demands
        "Show me the actual email text that proves {person_a} reported to {person_b}.",
        "What specific emails did {person_a} send to {person_b}? Quote the relevant body text.",
        # Level 2: Cross-referencing
        "The graph says {person_a} has a {rel_type} relationship with {person_b}. Show me the original emails that support this — I need dates, senders, and body content.",
        "find_connections says {person_a} is connected to {person_b} via {rel_type} with {count} evidence threads. Can you show me those actual threads?",
        # Level 3: Provenance drilling
        "You claimed {person_a} reported to {person_b} based on graph data. But what specific email thread was this extracted from? Show me the thread ID and full email body.",
        "I see the relationship has source_threads. Can you retrieve the full email body from those threads, not just a 300-character preview?",
        # Level 4: Adversarial
        "Prove to me that {person_a} and {person_b} actually communicated — don't just cite metadata, show me what they actually said to each other.",
        "The provenance says 'evidence_count: {count}' but I've never seen the actual evidence. Show me every email that contributes to this count.",
    ]

    def generate_questions(self, cycle: int, failures: list[dict],
                           existing_questions: list[dict]) -> list[dict]:
        """Generate new harder questions based on failure analysis."""
        if not failures:
            return []

        failure_summary = "\n".join(
            f"  - Q: {f['question'][:80]}  Score: {f['avg_score']}"
            for f in failures[:5]
        )

        existing_qs = "\n".join(
            f"  - {q['question'][:80]}" for q in existing_questions[:20]
        )

        prompt = f"""You are a Proposer agent in a self-improvement loop for a GraphRAG system that answers questions about Enron emails.

The system's MAIN WEAKNESS is surfacing original email evidence. It can find relationships in the knowledge graph but struggles to:
1. Show actual email body text (it truncates to 300-500 chars)
2. Chain evidence retrieval (finds connections but doesn't follow up with email evidence)
3. Provide traceable, specific citations

Current cycle: {cycle}
Recent failures (lowest-scoring questions):
{failure_summary}

Existing questions (avoid duplicates):
{existing_qs}

Generate exactly 5 NEW questions that are HARDER than the failures above.
Escalation level: {"basic evidence demands" if cycle <= 2 else "cross-referencing and provenance drilling" if cycle <= 4 else "adversarial evidence challenges"}

Each question must:
1. Require the agent to show ACTUAL EMAIL CONTENT, not just metadata
2. Target a specific weakness revealed by the failures
3. Include expected entities that should appear in the answer

Return ONLY a JSON array of objects with keys:
- "question": the question text
- "expected_entities": list of entity names that should appear
- "category": one of "org_hierarchy_evidence", "entity_pair_evidence", "relationship_evidence", "keyword_evidence", "corroboration"
- "graph_ground_truth": what the graph should have
- "historical_ground_truth": what we know historically
- "evidence_required": true

Return ONLY the JSON array, no other text."""

        try:
            raw = _call_llm(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            new_questions = json.loads(raw)
            if isinstance(new_questions, list):
                print(f"  Proposer: generated {len(new_questions)} new questions (cycle {cycle})")
                return new_questions[:5]
        except Exception as e:
            print(f"  Proposer: generation failed ({e}), using template questions")

        return self._template_questions(cycle, failures)

    def _template_questions(self, cycle: int, failures: list[dict]) -> list[dict]:
        """Fallback: generate from templates when LLM fails."""
        pairs = [
            ("Kenneth Lay", "Jeff Skilling", "REPORTS_TO", "5"),
            ("Andrew Fastow", "Jeff Skilling", "REPORTS_TO", "3"),
            ("Rosalee Fleming", "Kenneth Lay", "REPORTS_TO", "2"),
            ("David Delainey", "Jeff Skilling", "MANAGES", "4"),
            ("Sherron Watkins", "Andrew Fastow", "REPORTS_TO", "2"),
        ]

        level = min(cycle, len(self.ESCALATION_TEMPLATES) - 1)
        template = self.ESCALATION_TEMPLATES[level]
        questions = []
        for pa, pb, rel, count in pairs[:3]:
            q = template.format(person_a=pa, person_b=pb, rel_type=rel, count=count)
            questions.append({
                "question": q,
                "expected_entities": [pa, pb],
                "category": "org_hierarchy_evidence",
                "graph_ground_truth": f"Graph shows {pa} {rel} {pb}.",
                "historical_ground_truth": f"{pa} and {pb} had a {rel} relationship at Enron.",
                "evidence_required": True,
            })
        return questions


# ---------------------------------------------------------------------------
# Executor Agent (code-change registry)
# ---------------------------------------------------------------------------
@dataclass
class CodeFix:
    id: str
    description: str
    target_file: str
    applied: bool = False
    priority: int = 0  # lower = higher priority


class ExecutorAgent:
    """Tracks and reports on code improvements.

    Actual code changes are applied externally (by the orchestrating Cursor agent).
    This class tracks which fixes have been applied and recommends the next fix.
    """

    FIX_REGISTRY: list[dict] = [
        {"id": "add_get_email_full_body", "description": "Add get_email_full_body tool that returns untruncated email bodies for specific message_ids", "target_file": "src/agent/_agent_core.py", "priority": 1},
        {"id": "fix_search_emails_thread_id", "description": "Fix search_emails to include thread_id and to_list in returned JSON", "target_file": "src/agent/_agent_core.py", "priority": 2},
        {"id": "increase_snippet_lengths", "description": "Increase snippet_length from 500->1000 and body_preview from 300->800 in evidence tools", "target_file": "src/agent/_agent_core.py", "priority": 3},
        {"id": "evidence_chaining_prompt", "description": "Add evidence-chaining guidance to ENRON_SYSTEM_PROMPT", "target_file": "src/agent/_agent_core.py", "priority": 4},
        {"id": "register_full_body_tool", "description": "Register get_email_full_body in LOCAL_TOOLS and system prompt tool list", "target_file": "src/agent/_agent_core.py", "priority": 5},
    ]

    def __init__(self):
        self.fixes = [CodeFix(**f) for f in self.FIX_REGISTRY]

    def recommend_next_fix(self, failures: list[dict]) -> CodeFix | None:
        """Recommend the next highest-priority unapplied fix."""
        pending = [f for f in self.fixes if not f.applied]
        if not pending:
            return None
        pending.sort(key=lambda f: f.priority)
        return pending[0]

    def mark_applied(self, fix_id: str):
        for f in self.fixes:
            if f.id == fix_id:
                f.applied = True
                break

    def all_applied(self) -> bool:
        return all(f.applied for f in self.fixes)


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
def run_improvement_loop(max_cycles: int = 10, max_cases: int | None = None,
                         baseline_only: bool = False, holdout_only: bool = False):
    """Run the Proposer-Executor-Judge improvement loop."""

    judge = JudgeAgent()
    proposer = ProposerAgent()
    executor = ExecutorAgent()
    state = LoopState()

    # --- Split seed questions ---
    state.train_set, state.test_set, state.holdout_set = judge.create_splits(SEED_QUESTIONS)

    if holdout_only:
        print("\n=== HOLDOUT EVALUATION ===")
        result = judge.evaluate(state.holdout_set, "holdout", max_cases=max_cases)
        _print_result(result)
        return state

    # --- Cycle 0: Baseline ---
    print("\n" + "=" * 60)
    print("CYCLE 0: BASELINE EVALUATION")
    print("=" * 60)
    baseline = judge.evaluate(state.train_set, "train", max_cases=max_cases)
    baseline.cycle = 0
    state.history.append(baseline)
    _print_result(baseline)

    if baseline_only:
        return state

    # --- Improvement Cycles ---
    for cycle in range(1, max_cycles + 1):
        print(f"\n{'=' * 60}")
        print(f"CYCLE {cycle}")
        print("=" * 60)

        state.cycle = cycle

        # 1. Proposer: generate harder questions from failures
        print(f"\n--- Proposer (cycle {cycle}) ---")
        new_qs = proposer.generate_questions(
            cycle,
            state.history[-1].worst_questions,
            state.train_set + state.proposer_questions,
        )
        state.proposer_questions.extend(new_qs)

        # 2. Executor: recommend next fix
        print(f"\n--- Executor (cycle {cycle}) ---")
        next_fix = executor.recommend_next_fix(state.history[-1].worst_questions)
        if next_fix:
            print(f"  Recommended fix: [{next_fix.id}] {next_fix.description}")
            print(f"  Target: {next_fix.target_file}")
            print(f"  >>> Apply this fix externally, then re-run the loop <<<")
            executor.mark_applied(next_fix.id)
        else:
            print("  All fixes applied. No more code changes to recommend.")

        # 3. Judge: evaluate expanded question set (prioritize new questions)
        print(f"\n--- Judge (cycle {cycle}) ---")
        eval_set = new_qs + state.train_set  # new questions first
        result = judge.evaluate(eval_set, "train", max_cases=max_cases)
        result.cycle = cycle
        state.history.append(result)
        _print_result(result)

        # 4. Plateau check
        is_plateau, avg_gain = judge.check_plateau(state.history)
        if is_plateau:
            print(f"\n  PLATEAU DETECTED: avg marginal gain = {avg_gain:.2f}pp")
            print(f"  (threshold = {PLATEAU_THRESHOLD}pp over {PLATEAU_WINDOW} cycles)")
            state.plateau_declared = True

            # Run test set for validation
            print("\n--- Test Set Validation ---")
            test_result = judge.evaluate(state.test_set, "test", max_cases=max_cases)
            test_result.cycle = cycle
            _print_result(test_result)

            # Run holdout
            print("\n--- Holdout Evaluation ---")
            holdout_result = judge.evaluate(state.holdout_set, "holdout", max_cases=max_cases)
            holdout_result.cycle = cycle
            _print_result(holdout_result)
            break
        else:
            gain = (state.history[-1].overall_score - state.history[-2].overall_score) * 100
            print(f"\n  Marginal gain: {gain:+.1f}pp (threshold={PLATEAU_THRESHOLD}pp)")

        if executor.all_applied():
            print("\n  All executor fixes applied — running final evaluation")
            print("\n--- Test Set Validation ---")
            test_result = judge.evaluate(state.test_set, "test", max_cases=max_cases)
            _print_result(test_result)
            break

    # --- Summary ---
    print("\n" + "=" * 60)
    print("IMPROVEMENT LOOP SUMMARY")
    print("=" * 60)
    for r in state.history:
        gain = ""
        if r.cycle > 0:
            prev = state.history[r.cycle - 1].overall_score
            delta = (r.overall_score - prev) * 100
            gain = f"  ({delta:+.1f}pp)"
        print(f"  Cycle {r.cycle}: {r.overall_score:.3f} ({r.num_questions}q, {r.elapsed_s}s){gain}")

    if state.plateau_declared:
        print(f"\n  Plateau declared at cycle {state.cycle}")
    print(f"  Total proposer questions generated: {len(state.proposer_questions)}")

    return state


def _print_result(result: CycleResult):
    """Pretty-print a cycle result."""
    print(f"\n  === {result.split.upper()} Scores (cycle {result.cycle}) ===")
    print(f"  Questions: {result.num_questions}  |  Time: {result.elapsed_s}s")
    for name, score in sorted(result.scorer_scores.items()):
        bar = "█" * int(score * 20)
        print(f"    {name:30s}: {score:.3f} {bar}")
    print(f"    {'OVERALL':30s}: {result.overall_score:.3f}")
    if result.worst_questions:
        print(f"\n  Worst questions:")
        for wq in result.worst_questions[:3]:
            print(f"    [{wq['avg_score']:.2f}] {wq['question'][:80]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Agent Improvement Loop")
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--cases", type=int, default=None, help="Max questions per eval")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--holdout-only", action="store_true")
    args = parser.parse_args()

    run_improvement_loop(
        max_cycles=args.max_cycles,
        max_cases=args.cases,
        baseline_only=args.baseline_only,
        holdout_only=args.holdout_only,
    )
