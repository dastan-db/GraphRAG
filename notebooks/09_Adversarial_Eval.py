# Databricks notebook source
# MAGIC %md
# MAGIC # 09 — Adversarial Verification Loop (Data-First Evaluation)
# MAGIC
# MAGIC **Methodology**: Instead of writing eval questions top-down from domain knowledge,
# MAGIC this notebook works **backward from the actual source data**:
# MAGIC
# MAGIC 1. **Sample raw emails** from the corpus (anchor documents)
# MAGIC 2. **Gold extraction**: LLM judge reads raw text and produces the entities/relationships that _should_ have been extracted
# MAGIC 3. **Extraction audit**: Compare gold extraction against what notebook 07 actually put into the graph
# MAGIC 4. **Tool expectations**: Given gold entities/relationships, derive which agent tools should find them
# MAGIC 5. **Backward question generation**: Generate natural-language questions whose answers _require_ the data in these specific emails
# MAGIC 6. **Adversarial eval loop**: Run agent → score → analyze failures → generate harder questions → repeat
# MAGIC
# MAGIC This closes the gap in the current eval: we now test the **entire pipeline** from raw data to final answer,
# MAGIC not just the last mile (agent Q&A).

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 databricks-langchain langgraph>=0.3.4 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Load Tools
# MAGIC %run ../src/agent/tools

# COMMAND ----------

# DBTITLE 1,Load Agent
# MAGIC %run ../src/agent/agent

# COMMAND ----------

# DBTITLE 1,Load Evaluation Infrastructure
# MAGIC %run ../src/evaluation/evaluation

# COMMAND ----------

# DBTITLE 1,Load Enron Evaluation Scorers
# MAGIC %run ../src/evaluation/enron_evaluation

# COMMAND ----------

# DBTITLE 1,Imports and Configuration
import json
import os
import random
import re
import time
from dataclasses import dataclass, field

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

JUDGE_ENDPOINT = config.get("judge_endpoint", "databricks-claude-sonnet-4-6")
PROPOSER_ENDPOINT = config.get("external_llm_endpoint", "databricks-gpt-5-2")
LLM_ENDPOINT = config["llm_endpoint"]

CATALOG = config["catalog"]
ENRON_SCHEMA = config["enron_schema"]
EMAILS_TABLE = config["enron_emails_table"]
THREADS_TABLE = config["enron_threads_table"]
ENTITIES_TABLE = config["enron_entities_table"]
RELATIONSHIPS_TABLE = config["enron_relationships_table"]
ENTITY_MENTIONS_TABLE = config["enron_entity_mentions_table"]

PLATEAU_THRESHOLD = 1.5  # pp marginal gain below which we declare plateau
PLATEAU_WINDOW = 2

# COMMAND ----------

# DBTITLE 1,LLM Callers
def _call_llm(endpoint: str, prompt: str, max_tokens: int = 2048, temperature: float = 0.0) -> str:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    return resp["choices"][0]["message"]["content"].strip()


def _call_llm_json(endpoint: str, prompt: str, max_tokens: int = 2048) -> dict | list:
    text = _call_llm(endpoint, prompt, max_tokens)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Sample Anchor Emails
# MAGIC
# MAGIC Stratified sample across custodians, date ranges, and thread lengths to ensure
# MAGIC coverage of different communication types.

# COMMAND ----------

# DBTITLE 1,Sample Anchor Emails from Corpus
def sample_anchor_emails(n: int = 30, seed: int = 42) -> list[dict]:
    """Sample n emails from the corpus with stratification.

    Ensures diversity across: custodians, date ranges, thread lengths,
    and email types (direct vs group vs mass).
    """
    df = spark.sql(f"""
        WITH ranked AS (
            SELECT
                e.thread_id,
                e.message_id,
                e.subject,
                e.sender,
                e.date,
                SUBSTRING(e.body, 1, 4000) as body_text,
                COALESCE(SIZE(e.to_recipients), 0) as to_count,
                COALESCE(SIZE(e.cc_recipients), 0) as cc_count,
                LENGTH(e.body) as body_length,
                SPLIT(e.sender, '/')[0] as custodian_folder,
                ROW_NUMBER() OVER (
                    PARTITION BY SPLIT(e.sender, '/')[0]
                    ORDER BY RAND({seed})
                ) as rn
            FROM {EMAILS_TABLE} e
            WHERE e.body IS NOT NULL
              AND LENGTH(e.body) BETWEEN 100 AND 5000
              AND e.sender IS NOT NULL
              AND e.subject IS NOT NULL
        )
        SELECT thread_id, message_id, subject, sender, date,
               body_text, to_count, cc_count, body_length, custodian_folder
        FROM ranked
        WHERE rn <= {max(3, n // 10)}
        ORDER BY RAND({seed})
        LIMIT {n}
    """).collect()

    anchors = []
    for row in df:
        to_count = row["to_count"] or 0
        email_type = "direct" if to_count <= 2 else "group" if to_count <= 10 else "mass"
        anchors.append({
            "thread_id": row["thread_id"],
            "message_id": row["message_id"],
            "subject": row["subject"],
            "sender": row["sender"],
            "date": str(row["date"])[:10] if row["date"] else "unknown",
            "body_text": row["body_text"],
            "to_count": to_count,
            "cc_count": row["cc_count"] or 0,
            "body_length": row["body_length"],
            "email_type": email_type,
            "custodian": row["custodian_folder"],
        })

    print(f"Sampled {len(anchors)} anchor emails")
    print(f"  Custodians: {len(set(a['custodian'] for a in anchors))}")
    print(f"  Types: {dict(pd.Series([a['email_type'] for a in anchors]).value_counts())}")
    return anchors


anchors = sample_anchor_emails(n=30)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Gold Extraction
# MAGIC
# MAGIC For each anchor email, use the judge LLM to produce a **gold extraction**:
# MAGIC what entities and relationships _should_ have been extracted from this email.
# MAGIC This becomes the ground truth for the extraction pipeline.

# COMMAND ----------

# DBTITLE 1,Gold Extraction via LLM Judge
GOLD_EXTRACTION_PROMPT = """You are an expert knowledge graph curator. Read this email from the Enron corpus and extract ALL entities and relationships that a knowledge graph extraction pipeline should capture.

EMAIL METADATA:
- Date: {date}
- From: {sender}
- Subject: {subject}
- To count: {to_count}, CC count: {cc_count}

EMAIL BODY:
{body_text}

EXTRACTION RULES:
1. Extract EVERY named entity: people (full names), organizations, projects, divisions, events, locations, financial instruments
2. Extract EVERY relationship between entities mentioned or implied in the email
3. For people, use canonical full names (e.g., "Kenneth Lay" not "Ken")
4. Relationship types should be from: REPORTS_TO, MANAGES, SENT_TO, CC_TO, DISCUSSES, COLLABORATES_WITH, PARTICIPATES_IN, WORKS_AT, LOCATED_IN, RELATED_TO
5. Include the sender and all recipients as entities with SENT_TO relationships
6. Extract entities and relationships that are STATED or STRONGLY IMPLIED in the text — do NOT infer things that require external knowledge

Return ONLY a JSON object:
{{
  "entities": [
    {{"name": "Kenneth Lay", "type": "Person", "description": "Chairman and CEO of Enron, email sender"}},
    ...
  ],
  "relationships": [
    {{"source": "Kenneth Lay", "target": "Jeff Skilling", "type": "MANAGES", "evidence": "Lay directs Skilling to review the proposal"}},
    ...
  ],
  "key_topics": ["topic1", "topic2"],
  "temporal_markers": ["date or time reference from the email"]
}}"""


def gold_extract(anchor: dict) -> dict:
    """Produce a gold extraction for a single anchor email."""
    prompt = GOLD_EXTRACTION_PROMPT.format(**anchor)
    try:
        result = _call_llm_json(JUDGE_ENDPOINT, prompt, max_tokens=2048)
        result["anchor_thread_id"] = anchor["thread_id"]
        result["anchor_message_id"] = anchor["message_id"]
        return result
    except Exception as e:
        print(f"  Gold extraction failed for {anchor['subject'][:50]}: {e}")
        return {
            "entities": [], "relationships": [], "key_topics": [],
            "temporal_markers": [],
            "anchor_thread_id": anchor["thread_id"],
            "anchor_message_id": anchor["message_id"],
        }


print("Running gold extractions...")
gold_extractions = []
for i, anchor in enumerate(anchors):
    print(f"  [{i+1}/{len(anchors)}] {anchor['subject'][:60]}")
    extraction = gold_extract(anchor)
    gold_extractions.append(extraction)

total_entities = sum(len(g["entities"]) for g in gold_extractions)
total_rels = sum(len(g["relationships"]) for g in gold_extractions)
print(f"\nGold extraction complete: {total_entities} entities, {total_rels} relationships across {len(gold_extractions)} emails")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Extraction Audit
# MAGIC
# MAGIC Compare gold extractions against what the pipeline actually put into the graph.
# MAGIC This measures extraction precision and recall — a dimension the current eval completely ignores.

# COMMAND ----------

# DBTITLE 1,Extraction Audit — Compare Gold vs Actual
def audit_extraction(gold: dict, thread_id: str) -> dict:
    """Compare a gold extraction against what's actually in the graph for this thread."""
    actual_entities_df = spark.sql(f"""
        SELECT DISTINCT em.entity_name, e.entity_type
        FROM {ENTITY_MENTIONS_TABLE} em
        JOIN {ENTITIES_TABLE} e ON em.entity_name = e.name
        WHERE em.thread_id = '{thread_id}'
    """).collect()
    actual_entities = {row["entity_name"].lower(): row["entity_type"] for row in actual_entities_df}

    actual_rels_df = spark.sql(f"""
        SELECT source_entity, target_entity, relationship_type
        FROM {RELATIONSHIPS_TABLE}
        WHERE thread_id = '{thread_id}'
          AND relationship_type NOT IN ('SENT_TO', 'CC_TO')
    """).collect()
    actual_rels = {
        (row["source_entity"].lower(), row["target_entity"].lower(), row["relationship_type"])
        for row in actual_rels_df
    }

    gold_entity_names = {e["name"].lower() for e in gold.get("entities", [])}
    actual_entity_names = set(actual_entities.keys())

    entity_tp = gold_entity_names & actual_entity_names
    entity_fn = gold_entity_names - actual_entity_names  # in gold but not in graph
    entity_fp = actual_entity_names - gold_entity_names  # in graph but not in gold

    entity_precision = len(entity_tp) / len(entity_tp | entity_fp) if (entity_tp | entity_fp) else 1.0
    entity_recall = len(entity_tp) / len(gold_entity_names) if gold_entity_names else 1.0

    gold_rel_triples = set()
    for r in gold.get("relationships", []):
        if r["type"] not in ("SENT_TO", "CC_TO"):
            gold_rel_triples.add((r["source"].lower(), r["target"].lower(), r["type"]))

    rel_tp = gold_rel_triples & actual_rels
    rel_fn = gold_rel_triples - actual_rels
    rel_fp = actual_rels - gold_rel_triples

    rel_precision = len(rel_tp) / len(rel_tp | rel_fp) if (rel_tp | rel_fp) else 1.0
    rel_recall = len(rel_tp) / len(gold_rel_triples) if gold_rel_triples else 1.0

    return {
        "thread_id": thread_id,
        "entity_precision": round(entity_precision, 3),
        "entity_recall": round(entity_recall, 3),
        "entity_f1": round(2 * entity_precision * entity_recall / (entity_precision + entity_recall), 3) if (entity_precision + entity_recall) > 0 else 0.0,
        "entity_tp": len(entity_tp),
        "entity_fn": len(entity_fn),
        "entity_fp": len(entity_fp),
        "missing_entities": sorted(entity_fn),
        "rel_precision": round(rel_precision, 3),
        "rel_recall": round(rel_recall, 3),
        "rel_f1": round(2 * rel_precision * rel_recall / (rel_precision + rel_recall), 3) if (rel_precision + rel_recall) > 0 else 0.0,
        "rel_tp": len(rel_tp),
        "rel_fn": len(rel_fn),
        "rel_fp": len(rel_fp),
        "missing_rels": sorted(str(r) for r in rel_fn),
    }


print("Running extraction audit...")
audits = []
for gold in gold_extractions:
    audit = audit_extraction(gold, gold["anchor_thread_id"])
    audits.append(audit)

audit_df = pd.DataFrame(audits)
print("\n=== Extraction Audit Summary ===")
print(f"  Entity Precision: {audit_df['entity_precision'].mean():.3f}")
print(f"  Entity Recall:    {audit_df['entity_recall'].mean():.3f}")
print(f"  Entity F1:        {audit_df['entity_f1'].mean():.3f}")
print(f"  Rel Precision:    {audit_df['rel_precision'].mean():.3f}")
print(f"  Rel Recall:       {audit_df['rel_recall'].mean():.3f}")
print(f"  Rel F1:           {audit_df['rel_f1'].mean():.3f}")

worst_recall = audit_df.nsmallest(5, "entity_recall")
if not worst_recall.empty:
    print("\n  Worst entity recall threads:")
    for _, row in worst_recall.iterrows():
        print(f"    {row['thread_id'][:20]}: recall={row['entity_recall']:.2f}, missing={row['missing_entities'][:3]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Derive Tool Expectations
# MAGIC
# MAGIC Given the gold entities and relationships, determine which agent tools should
# MAGIC retrieve them and what the expected tool outputs are.

# COMMAND ----------

# DBTITLE 1,Tool Expectation Derivation
TOOL_MAP = {
    "REPORTS_TO": ["find_connections", "query_org_hierarchy", "get_hierarchy_evidence"],
    "MANAGES": ["find_connections", "query_org_hierarchy", "get_hierarchy_evidence"],
    "DISCUSSES": ["find_connections", "get_dyad_topics"],
    "COLLABORATES_WITH": ["find_connections", "get_emails_between"],
    "PARTICIPATES_IN": ["find_connections"],
    "SENT_TO": ["get_emails_between", "find_emails"],
    "CC_TO": ["get_emails_between", "find_emails"],
    "RELATED_TO": ["find_connections", "trace_path"],
}


def derive_tool_expectations(gold: dict, anchor: dict) -> list[dict]:
    """From a gold extraction + anchor email, derive expected tool calls and outputs."""
    expectations = []

    entities = gold.get("entities", [])
    person_entities = [e for e in entities if e.get("type") == "Person"]

    for entity in person_entities[:3]:
        expectations.append({
            "tool": "find_entity",
            "args": {"name": entity["name"]},
            "should_find": True,
            "rationale": f"Entity '{entity['name']}' appears in email '{anchor['subject'][:50]}'",
        })

    for rel in gold.get("relationships", [])[:5]:
        rel_type = rel.get("type", "RELATED_TO")
        expected_tools = TOOL_MAP.get(rel_type, ["find_connections"])
        for tool_name in expected_tools[:2]:
            expectations.append({
                "tool": tool_name,
                "args": {"entity_name": rel["source"], "relationship_type": rel_type}
                if tool_name == "find_connections" else {"entity_name": rel["source"]},
                "should_contain": {
                    "source": rel["source"],
                    "target": rel["target"],
                    "type": rel_type,
                },
                "rationale": f"Relationship {rel['source']} -{rel_type}-> {rel['target']} from email",
            })

    if len(person_entities) >= 2:
        a, b = person_entities[0]["name"], person_entities[1]["name"]
        expectations.append({
            "tool": "get_emails_between",
            "args": {"entity_a": a, "entity_b": b},
            "should_find": True,
            "rationale": f"Both {a} and {b} appear in the same email thread",
        })

    if gold.get("key_topics"):
        expectations.append({
            "tool": "search_emails",
            "args": {"keywords": ", ".join(gold["key_topics"][:3])},
            "should_find": True,
            "rationale": f"Topics {gold['key_topics'][:3]} are discussed in the anchor email",
        })

    return expectations


print("Deriving tool expectations...")
all_tool_expectations = []
for gold, anchor in zip(gold_extractions, anchors):
    exps = derive_tool_expectations(gold, anchor)
    all_tool_expectations.append(exps)
    
total_exps = sum(len(e) for e in all_tool_expectations)
print(f"Generated {total_exps} tool expectations across {len(anchors)} emails")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Backward Question Generation
# MAGIC
# MAGIC Generate natural-language questions whose answers require discovering the
# MAGIC specific entities, relationships, and evidence found in the anchor emails.

# COMMAND ----------

# DBTITLE 1,Backward Question Generator
BACKWARD_QUESTION_PROMPT = """You are generating evaluation questions for a GraphRAG agent that answers questions about the Enron email corpus.

Given the following GROUND TRUTH about a specific email and the entities/relationships extracted from it, generate {n_questions} natural-language questions that a user might ask.

ANCHOR EMAIL:
- Date: {date}
- From: {sender}
- Subject: {subject}
- Body preview: {body_preview}

GOLD ENTITIES found in this email:
{entities_json}

GOLD RELATIONSHIPS found in this email:
{relationships_json}

KEY TOPICS: {topics}

REQUIREMENTS:
1. Each question MUST require the agent to find information that IS in this specific email or its extracted graph data
2. Questions should sound natural — like a real investigator would ask
3. Include a mix of difficulty levels:
   - Easy: single entity lookup ("Who is X?")
   - Medium: relationship traversal ("How is X connected to Y?")
   - Hard: evidence-backed ("Show me the emails proving X reported to Y")
   - Adversarial: cross-referencing ("The graph says X discussed Y — show me the original email")
4. For each question, specify the expected answer grounded in the email data

Return ONLY a JSON array:
[
  {{
    "question": "natural language question",
    "difficulty": "easy|medium|hard|adversarial",
    "expected_entities": ["entity1", "entity2"],
    "graph_ground_truth": "What the graph should contain (from the gold extraction)",
    "email_ground_truth": "What the actual email says (from the anchor email body)",
    "expected_tools": ["tool1", "tool2"],
    "category": "entity_lookup|relationship|evidence|cross_reference|temporal"
  }},
  ...
]"""


def generate_backward_questions(
    gold: dict, anchor: dict, n_questions: int = 3
) -> list[dict]:
    """Generate questions backward from gold extraction + anchor email."""
    entities_json = json.dumps(gold.get("entities", [])[:8], indent=2)
    rels_json = json.dumps(gold.get("relationships", [])[:6], indent=2)
    topics = ", ".join(gold.get("key_topics", [])[:5]) or "general corporate communication"

    prompt = BACKWARD_QUESTION_PROMPT.format(
        n_questions=n_questions,
        date=anchor["date"],
        sender=anchor["sender"],
        subject=anchor["subject"],
        body_preview=anchor["body_text"][:800],
        entities_json=entities_json,
        relationships_json=rels_json,
        topics=topics,
    )

    try:
        questions = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if isinstance(questions, list):
            for q in questions:
                q["anchor_thread_id"] = anchor["thread_id"]
                q["anchor_subject"] = anchor["subject"]
                q["historical_ground_truth"] = q.get("email_ground_truth", "")
                q["evidence_required"] = q.get("difficulty") in ("hard", "adversarial")
            return questions[:n_questions]
    except Exception as e:
        print(f"  Question generation failed: {e}")

    return []


print("Generating backward questions...")
all_questions = []
for i, (gold, anchor) in enumerate(zip(gold_extractions, anchors)):
    if not gold.get("entities"):
        continue
    n_qs = 2 if len(gold["entities"]) < 3 else 3
    questions = generate_backward_questions(gold, anchor, n_questions=n_qs)
    all_questions.extend(questions)
    if (i + 1) % 10 == 0:
        print(f"  [{i+1}/{len(anchors)}] Generated {len(all_questions)} questions so far")

print(f"\nTotal backward-generated questions: {len(all_questions)}")
difficulty_dist = pd.Series([q.get("difficulty", "unknown") for q in all_questions]).value_counts()
print(f"Difficulty distribution:\n{difficulty_dist.to_string()}")
category_dist = pd.Series([q.get("category", "unknown") for q in all_questions]).value_counts()
print(f"Category distribution:\n{category_dist.to_string()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Adversarial Evaluation Loop
# MAGIC
# MAGIC Run the agent against the backward-generated questions, score with LLM judges,
# MAGIC analyze failures, generate harder questions from failures, and repeat until
# MAGIC plateau or target is reached.

# COMMAND ----------

# DBTITLE 1,Agent Predict Function
from src.agent.agent_serving import GraphRAGAgent
from mlflow.types.responses import ResponsesAgentRequest

_AGENT = None

def _get_agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = GraphRAGAgent()
    return _AGENT

def predict_fn(question: str) -> str:
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

# COMMAND ----------

# DBTITLE 1,Data-Grounded Scorers (new)
DATA_CONTEXT = """CRITICAL CONTEXT: The agent is a QA system built on a knowledge graph derived from ~20,000 Enron emails (2000-2002). It can ONLY access:
1. Email content and metadata from the corpus
2. Entities and relationships extracted from those emails
3. A curated org hierarchy table (24 entries from public record)
4. A curated investigation timeline (28 events from public record)
5. Pre-aggregated communication statistics (dyads, person activity)"""


@scorer
def extraction_coverage(inputs, outputs, expectations=None):
    """Did the agent find entities that we KNOW are in the graph (from gold extraction)?"""
    expected = (expectations or {}).get("expected_entities", [])
    if not expected:
        return Feedback(value=1.0, rationale="No expected entities")
    text = (outputs if isinstance(outputs, str) else str(outputs)).lower()
    found = [e for e in expected if e.lower() in text or (
        " " in e and e.split()[-1].lower() in text
    )]
    score = round(len(found) / len(expected), 2)
    missing = [e for e in expected if e not in found]
    return Feedback(value=score, rationale=f"Found {len(found)}/{len(expected)}. Missing: {missing}")


@scorer
def tool_usage_correctness(inputs, outputs, expectations=None):
    """Did the response demonstrate use of the expected tools?"""
    expected_tools = (expectations or {}).get("expected_tools", [])
    if not expected_tools:
        return Feedback(value=1.0, rationale="No expected tools")
    text = (outputs if isinstance(outputs, str) else str(outputs)).lower()

    tool_indicators = {
        "find_entity": ["entity", "found in graph", "entity_type"],
        "find_connections": ["connections", "reports_to", "manages", "relationship"],
        "get_emails_between": ["emails between", "email evidence", "direct emails"],
        "query_org_hierarchy": ["org hierarchy", "reporting chain", "organizational"],
        "trace_path": ["path", "shortest path", "connected via"],
        "search_emails": ["search", "keyword", "found emails mentioning"],
        "get_hierarchy_evidence": ["hierarchy evidence", "email evidence for reporting"],
        "get_email_full_body": ["full body", "email body", "complete email"],
        "get_entity_summary": ["entity summary", "profile", "pagerank", "centrality"],
        "query_timeline": ["timeline", "investigation", "key events"],
    }

    found_tools = []
    for tool in expected_tools:
        indicators = tool_indicators.get(tool, [tool.replace("_", " ")])
        if any(ind in text for ind in indicators):
            found_tools.append(tool)

    score = round(len(found_tools) / len(expected_tools), 2)
    missing = [t for t in expected_tools if t not in found_tools]
    return Feedback(value=score, rationale=f"Tool evidence: {found_tools}. Missing: {missing}")


@scorer
def email_grounding(inputs, outputs, expectations=None):
    """Does the response ground claims in actual email data from the anchor?"""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    evidence_required = (expectations or {}).get("evidence_required", True)
    if not evidence_required:
        return Feedback(value=1.0, rationale="Evidence not required")

    email_gt = (expectations or {}).get("email_ground_truth", "")
    graph_gt = (expectations or {}).get("graph_ground_truth", "")

    prompt = f"""{DATA_CONTEXT}

This question was generated from a SPECIFIC email in the corpus. We know exactly what the email contains.

Email Ground Truth (what the actual email says): {email_gt}
Graph Ground Truth (what the graph should contain): {graph_gt}

Evaluate whether the agent's response is GROUNDED in actual data from the corpus.

Scoring (0.0 to 1.0):
- 1.0: Response cites specific email evidence (dates, senders, subjects, body text) that matches the ground truth
- 0.7: Response finds the right entities/relationships but evidence is metadata-only (no email body)
- 0.5: Response partially matches — some data found but key ground truth elements missing
- 0.3: Response is vague or only uses general knowledge
- 0.0: Response fabricates evidence or contradicts the ground truth

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""

    try:
        parsed = _call_llm_json(JUDGE_ENDPOINT, prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


@scorer
def adversarial_robustness(inputs, outputs, expectations=None):
    """For adversarial questions: does the agent handle challenges without fabricating?"""
    difficulty = (expectations or {}).get("difficulty", "easy")
    if difficulty not in ("hard", "adversarial"):
        return Feedback(value=1.0, rationale="Not an adversarial question")

    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    prompt = f"""{DATA_CONTEXT}

This is an ADVERSARIAL evaluation question designed to probe the agent's honesty and evidence quality.

Evaluate:
1. Does the agent fabricate evidence when challenged? (instant 0.0 if yes)
2. Does the agent honestly state limitations when data is incomplete?
3. Does the agent provide SPECIFIC email citations rather than vague claims?
4. Does the agent distinguish between graph-derived evidence and general knowledge?

Scoring (0.0 to 1.0):
- 1.0: Honest, specific, well-grounded, no fabrication
- 0.7: Mostly honest, specific evidence but some vague claims
- 0.5: Mixed — some specific evidence, some ungrounded claims
- 0.3: Mostly vague, limited evidence, possible fabrication
- 0.0: Fabricates evidence or makes ungrounded claims when challenged

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""

    try:
        parsed = _call_llm_json(JUDGE_ENDPOINT, prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


ADVERSARIAL_SCORERS = [
    extraction_coverage,
    tool_usage_correctness,
    email_grounding,
    adversarial_robustness,
]

# COMMAND ----------

# DBTITLE 1,Evaluation Runner
def run_eval_cycle(questions: list[dict], cycle_name: str = "cycle_0",
                   max_cases: int | None = None) -> dict:
    """Run one evaluation cycle and return results."""
    data = questions[:max_cases] if max_cases else questions

    eval_records = []
    for q in data:
        eval_records.append({
            "inputs": {"question": q["question"]},
            "expectations": {
                "expected_entities": q.get("expected_entities", []),
                "expected_tools": q.get("expected_tools", []),
                "graph_ground_truth": q.get("graph_ground_truth", ""),
                "email_ground_truth": q.get("email_ground_truth", q.get("historical_ground_truth", "")),
                "historical_ground_truth": q.get("historical_ground_truth", ""),
                "evidence_required": q.get("evidence_required", True),
                "difficulty": q.get("difficulty", "medium"),
                "category": q.get("category", "unknown"),
            },
        })

    eval_df = pd.DataFrame(eval_records)
    print(f"\n{'='*60}")
    print(f"EVAL CYCLE: {cycle_name} ({len(eval_df)} questions)")
    print(f"{'='*60}")

    t0 = time.time()
    with mlflow.start_run(run_name=f"adversarial_{cycle_name}"):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=predict_fn,
            scorers=ADVERSARIAL_SCORERS,
        )

    elapsed = time.time() - t0
    results_df = results.tables["eval_results"]

    score_cols = [
        c for c in results_df.columns
        if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])
    ]

    scorer_scores = {}
    if score_cols:
        means = results_df[score_cols].mean()
        for col in score_cols:
            name = col.replace("/value", "")
            scorer_scores[name] = round(float(means[col]), 3)
        overall = round(float(means.mean()), 3)
    else:
        overall = 0.0

    results_df["avg_score"] = results_df[score_cols].apply(
        pd.to_numeric, errors="coerce"
    ).mean(axis=1) if score_cols else 0.0

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

    print(f"\n  Scores ({cycle_name}):")
    for name, score in sorted(scorer_scores.items()):
        bar = "█" * int(score * 20)
        print(f"    {name:30s}: {score:.3f} {bar}")
    print(f"    {'OVERALL':30s}: {overall:.3f}")
    print(f"    Time: {elapsed:.0f}s ({elapsed / max(len(data), 1):.1f}s/question)")

    if worst_qs:
        print(f"\n  Worst questions:")
        for wq in worst_qs[:3]:
            print(f"    [{wq['avg_score']:.2f}] {wq['question'][:80]}")

    return {
        "cycle": cycle_name,
        "overall": overall,
        "scorer_scores": scorer_scores,
        "worst_questions": worst_qs,
        "num_questions": len(data),
        "elapsed_s": round(elapsed, 1),
        "results_df": results_df,
    }

# COMMAND ----------

# DBTITLE 1,Adversarial Question Escalator
def escalate_questions(failures: list[dict], existing_questions: list[dict],
                       cycle: int) -> list[dict]:
    """Generate harder questions from failure analysis."""
    if not failures:
        return []

    failure_summary = "\n".join(
        f"  - Q: {f['question'][:80]}  Score: {f['avg_score']}"
        for f in failures[:5]
    )

    prompt = f"""You are an adversarial evaluator for a GraphRAG system about Enron emails.

The agent FAILED on these questions (lowest scores):
{failure_summary}

Current cycle: {cycle}
Escalation strategy: {"probe extraction gaps — ask about entities the agent missed" if cycle <= 2 else "demand specific email evidence — ask for exact email body text, dates, senders" if cycle <= 4 else "adversarial cross-referencing — challenge claims, ask for proof of specific relationships"}

Generate 5 NEW harder questions that:
1. Target the same weaknesses revealed by the failures
2. Require the agent to show ACTUAL DATA, not just assert facts
3. Include questions that test the EXTRACTION PIPELINE (e.g., "What entities were extracted from emails about topic X?")
4. Include questions that test EVIDENCE TRACEABILITY (e.g., "Show me the specific email that proves X works with Y")

Return ONLY a JSON array of objects:
[{{"question": "...", "difficulty": "hard|adversarial", "expected_entities": [...], "graph_ground_truth": "...", "email_ground_truth": "...", "expected_tools": [...], "category": "extraction_gap|evidence_demand|cross_reference|adversarial_probe", "evidence_required": true}}]"""

    try:
        new_qs = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if isinstance(new_qs, list):
            for q in new_qs:
                q.setdefault("historical_ground_truth", q.get("email_ground_truth", ""))
                q.setdefault("evidence_required", True)
            print(f"  Escalator: generated {len(new_qs)} harder questions")
            return new_qs[:5]
    except Exception as e:
        print(f"  Escalator failed: {e}")
    return []

# COMMAND ----------

# DBTITLE 1,Main Adversarial Loop
def run_adversarial_loop(questions: list[dict], max_cycles: int = 6,
                          max_cases: int | None = None) -> dict:
    """Run the full adversarial verification loop."""
    history = []
    current_questions = list(questions)

    random.seed(42)
    random.shuffle(current_questions)
    n = len(current_questions)
    n_train = max(3, int(n * 0.6))
    n_test = max(2, int(n * 0.2))
    train = current_questions[:n_train]
    test = current_questions[n_train:n_train + n_test]
    holdout = current_questions[n_train + n_test:]
    if not holdout:
        holdout = test[-2:]

    print(f"Splits: train={len(train)}, test={len(test)}, holdout={len(holdout)}")

    # Cycle 0: Baseline
    baseline = run_eval_cycle(train, "baseline", max_cases=max_cases)
    history.append(baseline)

    for cycle in range(1, max_cycles + 1):
        # Escalate from failures
        new_qs = escalate_questions(
            history[-1]["worst_questions"],
            train,
            cycle,
        )
        train = new_qs + train  # new (harder) questions first

        result = run_eval_cycle(train, f"cycle_{cycle}", max_cases=max_cases)
        history.append(result)

        # Plateau check
        if len(history) >= PLATEAU_WINDOW + 1:
            gains = []
            for i in range(-PLATEAU_WINDOW, 0):
                g = (history[i]["overall"] - history[i - 1]["overall"]) * 100
                gains.append(g)

            if all(abs(g) < PLATEAU_THRESHOLD for g in gains):
                print(f"\n  PLATEAU: avg gain = {sum(gains)/len(gains):.1f}pp over {PLATEAU_WINDOW} cycles")

                print("\n--- Test Set Validation ---")
                test_result = run_eval_cycle(test, "test_final", max_cases=max_cases)

                print("\n--- Holdout Evaluation ---")
                holdout_result = run_eval_cycle(holdout, "holdout_final", max_cases=max_cases)

                history.append(test_result)
                history.append(holdout_result)
                break

        gain = (history[-1]["overall"] - history[-2]["overall"]) * 100
        print(f"\n  Cycle {cycle} marginal gain: {gain:+.1f}pp")

    # Summary
    print(f"\n{'='*60}")
    print("ADVERSARIAL LOOP SUMMARY")
    print(f"{'='*60}")
    for h in history:
        cycle_name = h["cycle"]
        overall = h["overall"]
        n_q = h["num_questions"]
        elapsed = h["elapsed_s"]
        print(f"  {cycle_name:20s}: {overall:.3f} ({n_q}q, {elapsed:.0f}s)")

    return {
        "history": history,
        "extraction_audit": audit_df.to_dict("records"),
        "total_questions_generated": len(train),
        "final_score": history[-1]["overall"],
    }

# COMMAND ----------

# DBTITLE 1,Run the Adversarial Loop
loop_result = run_adversarial_loop(
    all_questions,
    max_cycles=6,
    max_cases=None,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Results Summary

# COMMAND ----------

# DBTITLE 1,Extraction Audit Report
print("=== EXTRACTION PIPELINE AUDIT ===")
print(f"Emails audited: {len(audit_df)}")
print(f"Entity Precision: {audit_df['entity_precision'].mean():.3f}")
print(f"Entity Recall:    {audit_df['entity_recall'].mean():.3f}")
print(f"Entity F1:        {audit_df['entity_f1'].mean():.3f}")
print(f"Rel Precision:    {audit_df['rel_precision'].mean():.3f}")
print(f"Rel Recall:       {audit_df['rel_recall'].mean():.3f}")
print(f"Rel F1:           {audit_df['rel_f1'].mean():.3f}")

if audit_df["entity_fn"].sum() > 0:
    all_missing = []
    for _, row in audit_df.iterrows():
        all_missing.extend(row["missing_entities"])
    missing_counts = pd.Series(all_missing).value_counts().head(10)
    print(f"\nMost commonly missed entities:")
    for entity, count in missing_counts.items():
        print(f"  {entity}: missed in {count} emails")

# COMMAND ----------

# DBTITLE 1,Agent Performance by Question Difficulty
if loop_result.get("history"):
    final_cycle = loop_result["history"][-1]
    results_df = final_cycle.get("results_df")
    if results_df is not None and "avg_score" in results_df.columns:
        print("=== AGENT PERFORMANCE (Final Cycle) ===")
        print(f"Overall: {final_cycle['overall']:.3f}")
        print(f"Questions: {final_cycle['num_questions']}")
        print(f"\nScorer breakdown:")
        for name, score in sorted(final_cycle["scorer_scores"].items()):
            print(f"  {name:30s}: {score:.3f}")

# COMMAND ----------

# DBTITLE 1,Improvement Trajectory
if loop_result.get("history"):
    print("=== IMPROVEMENT TRAJECTORY ===")
    prev = None
    for h in loop_result["history"]:
        gain = f"  ({(h['overall'] - prev)*100:+.1f}pp)" if prev is not None else ""
        print(f"  {h['cycle']:20s}: {h['overall']:.3f}{gain}")
        prev = h["overall"]
