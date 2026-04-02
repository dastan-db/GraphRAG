# Databricks notebook source
# MAGIC %md
# MAGIC ### Adversarial Verification Loop — Data-First Evaluation
# MAGIC
# MAGIC Standalone module for the data-first adversarial eval pipeline.
# MAGIC Can be run as a notebook (%run) or as a CLI script.
# MAGIC
# MAGIC **Usage (CLI):**
# MAGIC ```
# MAGIC python -m src.evaluation.adversarial_eval --anchors 15 --max-cycles 4 --cases 10
# MAGIC python -m src.evaluation.adversarial_eval --baseline-only --anchors 10
# MAGIC ```

# COMMAND ----------

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field

# Ensure project root is on path for CLI usage
_project_root = os.path.join(os.path.dirname(__file__), "..", "..")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

os.environ.setdefault("GRAPHRAG_BACKEND", "databricks")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

from src.evaluation.question_bank import canonicalize_generated_question

# COMMAND ----------

# DBTITLE 1,Configuration
JUDGE_ENDPOINT = os.environ.get("GRAPHRAG_JUDGE_ENDPOINT", "databricks-claude-sonnet-4-6")
PROPOSER_ENDPOINT = os.environ.get("GRAPHRAG_PROPOSER_ENDPOINT", "databricks-claude-sonnet-4-6")

CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
ENRON_SCHEMA = os.environ.get("GRAPHRAG_SCHEMA", "graphrag_enron")
EMAILS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.emails"
THREADS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.threads"
ENTITIES_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entities"
RELATIONSHIPS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.relationships"
ENTITY_MENTIONS_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_mentions"
ENTITY_ALIASES_TABLE = f"{CATALOG}.{ENRON_SCHEMA}.entity_aliases"

PLATEAU_THRESHOLD = 1.5
PLATEAU_WINDOW = 2

VALID_TOOL_NAMES = {
    "find_entity", "find_connections", "get_emails_between",
    "query_org_hierarchy", "trace_path", "search_emails",
    "get_hierarchy_evidence", "get_email_full_body",
    "get_entity_summary", "query_timeline", "get_dyad_topics",
    "get_relationship_evidence", "get_source_evidence",
    "find_top_contacts", "query_and_enrich",
    "get_communication_stats", "get_topic_distribution",
}

# COMMAND ----------

# DBTITLE 1,LLM Callers
def _call_llm(endpoint: str, prompt: str, max_tokens: int = 2048,
              temperature: float = 0.0) -> str:
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


def _call_llm_json(endpoint: str, prompt: str, max_tokens: int = 2048):
    text = _call_llm(endpoint, prompt, max_tokens)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

# COMMAND ----------

# DBTITLE 1,SQL Executor (DuckDB local or Databricks remote)
_EVAL_BACKEND = os.environ.get("GRAPHRAG_EVAL_BACKEND",
                                os.environ.get("GRAPHRAG_BACKEND", "databricks"))
_DUCKDB_CONN = None
_DUCKDB_LOCK = None

_FQN_STRIP = f"{CATALOG}.{ENRON_SCHEMA}."


def _get_duckdb():
    """Lazy-init a read-only DuckDB connection."""
    global _DUCKDB_CONN, _DUCKDB_LOCK
    if _DUCKDB_CONN is None:
        import duckdb
        import threading
        db_path = os.environ.get("GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb")
        _DUCKDB_CONN = duckdb.connect(db_path, read_only=True)
        _DUCKDB_LOCK = threading.Lock()
        print(f"  [DuckDB] Connected to {db_path}")
    return _DUCKDB_CONN, _DUCKDB_LOCK


def _execute_sql(query: str) -> list[dict]:
    """Execute SQL via DuckDB (local) or Statement Execution API (remote)."""
    if _EVAL_BACKEND == "local":
        conn, lock = _get_duckdb()
        q = query.replace(_FQN_STRIP, "")
        q = re.sub(r"\bSIZE\(", "array_length(", q, flags=re.IGNORECASE)
        has_rand = bool(re.search(r"\bRAND\(", q, re.IGNORECASE))
        q = re.sub(r"\bRAND\((\d+)\)", r"random()", q, flags=re.IGNORECASE)
        with lock:
            if has_rand:
                try:
                    conn.execute("SELECT setseed(0.42)")
                except Exception:
                    pass
            try:
                result = conn.execute(q)
                columns = [desc[0] for desc in result.description]
                return [dict(zip(columns, row)) for row in result.fetchall()]
            except Exception as exc:
                print(f"  [DuckDB] SQL error: {exc}\n    Query: {q[:200]}")
                return []

    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    warehouse_id = os.environ.get("GRAPHRAG_WAREHOUSE_ID", "")
    if not warehouse_id:
        warehouses = list(w.warehouses.list())
        if warehouses:
            for wh in warehouses:
                if wh.state and str(wh.state).upper() == "RUNNING":
                    warehouse_id = wh.id
                    break
            if not warehouse_id:
                warehouse_id = warehouses[0].id
        else:
            raise RuntimeError("No SQL warehouses found")

    stmt = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=query,
        wait_timeout="50s",
    )

    if not stmt.result or not stmt.result.data_array:
        return []

    columns = [c.name for c in stmt.manifest.schema.columns]
    rows = []
    for row_data in stmt.result.data_array:
        rows.append(dict(zip(columns, row_data)))
    return rows

# COMMAND ----------

# DBTITLE 1,Step 1 — Sample Anchor Emails
def sample_anchor_emails(n: int = 30, seed: int = 42) -> list[dict]:
    query = f"""
        WITH ranked AS (
            SELECT
                thread_id, message_id, subject, sender, date,
                SUBSTRING(body, 1, 4000) as body_text,
                COALESCE(SIZE(to_recipients), 0) as to_count,
                COALESCE(SIZE(cc_recipients), 0) as cc_count,
                LENGTH(body) as body_length,
                ROW_NUMBER() OVER (ORDER BY RAND({seed})) as rn
            FROM {EMAILS_TABLE}
            WHERE body IS NOT NULL
              AND LENGTH(body) BETWEEN 100 AND 5000
              AND sender IS NOT NULL
              AND subject IS NOT NULL
        )
        SELECT thread_id, message_id, subject, sender, date,
               body_text, to_count, cc_count, body_length
        FROM ranked
        WHERE rn <= {n}
    """
    rows = _execute_sql(query)

    anchors = []
    for row in rows:
        to_count = int(row.get("to_count") or 0)
        email_type = "direct" if to_count <= 2 else "group" if to_count <= 10 else "mass"
        anchors.append({
            "thread_id": row["thread_id"],
            "message_id": row.get("message_id", ""),
            "subject": row["subject"],
            "sender": row["sender"],
            "date": str(row.get("date", ""))[:10],
            "body_text": row["body_text"],
            "to_count": to_count,
            "cc_count": int(row.get("cc_count") or 0),
            "body_length": int(row.get("body_length") or 0),
            "email_type": email_type,
        })

    print(f"Sampled {len(anchors)} anchor emails")
    print(f"  Types: {dict(pd.Series([a['email_type'] for a in anchors]).value_counts())}")
    return anchors

# COMMAND ----------

# DBTITLE 1,Step 2 — Gold Extraction
GOLD_EXTRACTION_PROMPT = """You are an expert knowledge graph curator. Read this email from the Enron corpus and extract ALL entities and relationships that a knowledge graph extraction pipeline should capture.

EMAIL METADATA:
- Date: {date}
- From: {sender}
- Subject: {subject}
- To count: {to_count}, CC count: {cc_count}

EMAIL BODY:
{body_text}

EXTRACTION RULES:
1. Extract EVERY named entity: people (full names), organizations, projects, divisions, events, financial instruments
2. Extract EVERY relationship between entities mentioned or implied
3. Use canonical full names for people (e.g., "Kenneth Lay" not "Ken", "Jeffrey Skilling" not "Jeff")
4. NEVER use title prefixes (Dr., Mr., Mrs.) — use bare names only
5. Relationship types: REPORTS_TO, MANAGES, SENT_TO, CC_TO, DISCUSSES, COLLABORATES_WITH, PARTICIPATES_IN, EMPLOYED_BY, RELATED_TO
6. Include sender and recipients as entities
7. Only extract what is STATED or STRONGLY IMPLIED — no external knowledge
8. Entity types must be: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

Return ONLY JSON:
{{
  "entities": [{{"name": "...", "type": "Person|Organization|Division|Project|Meeting|Document|Location|Financial_Event", "description": "..."}}],
  "relationships": [{{"source": "...", "target": "...", "type": "...", "evidence": "brief quote or paraphrase"}}],
  "key_topics": ["topic1", "topic2"],
  "temporal_markers": ["dates or time references from the email"]
}}"""


def gold_extract(anchor: dict) -> dict:
    truncated = dict(anchor)
    truncated["body_text"] = anchor["body_text"][:2500]
    prompt = GOLD_EXTRACTION_PROMPT.format(**truncated)
    for attempt in range(2):
        try:
            result = _call_llm_json(JUDGE_ENDPOINT, prompt, max_tokens=2048)
            result["anchor_thread_id"] = anchor["thread_id"]
            result["anchor_message_id"] = anchor.get("message_id", "")
            return result
        except json.JSONDecodeError:
            truncated["body_text"] = anchor["body_text"][:1200]
            prompt = GOLD_EXTRACTION_PROMPT.format(**truncated)
        except Exception as e:
            print(f"  Gold extraction failed for {anchor['subject'][:50]}: {e}")
            break
    return {
        "entities": [], "relationships": [], "key_topics": [],
        "temporal_markers": [],
        "anchor_thread_id": anchor["thread_id"],
        "anchor_message_id": anchor.get("message_id", ""),
    }


def _parallel_gold_extract(anchors: list[dict], max_workers: int = 4) -> list[dict]:
    """Run gold extraction in parallel with ThreadPoolExecutor."""
    results = [None] * len(anchors)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(gold_extract, anchor): i
            for i, anchor in enumerate(anchors)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
            print(f"  [{idx+1}/{len(anchors)}] {anchors[idx]['subject'][:60]}")
    return results


# --------------------------------------------------------------------------- #
# Ground Truth Cache — skip Steps 1-5 on repeat runs with same seed+n_anchors
# --------------------------------------------------------------------------- #
CACHE_DIR = Path("data/eval_cache")


def _cache_key(n_anchors: int, seed: int) -> str:
    return f"avl_seed{seed}_n{n_anchors}"


def _save_cache(key: str, anchors, gold_extractions, audits, all_questions):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    payload = {
        "anchors": anchors,
        "gold_extractions": gold_extractions,
        "audits": audits,
        "all_questions": all_questions,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  Cache saved: {path} ({len(all_questions)} questions)")


def _load_cache(key: str):
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    print(f"  Cache hit: {path} (created {payload.get('created_at', '?')})")
    return payload

# COMMAND ----------

# DBTITLE 1,Step 3 — Extraction Audit
def _slugify(name: str) -> str:
    """Convert a name to a slug for fuzzy matching against entity_ids."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _fuzzy_entity_match(gold_name: str, actual_names: set[str],
                        actual_slugs: set[str]) -> bool:
    """Check if a gold entity name matches any actual entity via multiple strategies.

    Strategies: exact name, slug match, last-name match, first-name match,
    containment (gold in actual or actual in gold).
    """
    g_lower = gold_name.lower()
    if g_lower in actual_names:
        return True
    g_slug = _slugify(gold_name)
    if g_slug in actual_slugs:
        return True
    parts = gold_name.split()
    if len(parts) >= 2:
        last = parts[-1].lower()
        first = parts[0].lower()
        for a in actual_names:
            a_parts = a.split()
            if len(a_parts) >= 2 and a_parts[-1] == last:
                return True
            if a == last or a == first:
                return True
    for a in actual_names:
        if len(g_lower) >= 4 and g_lower in a:
            return True
        if len(a) >= 4 and a in g_lower:
            return True
    return False


def audit_extraction(gold: dict, thread_id: str) -> dict:
    safe_tid = thread_id.replace("'", "''")

    actual_entities = _execute_sql(f"""
        SELECT DISTINCT e.name, e.entity_type, e.entity_id
        FROM {ENTITY_MENTIONS_TABLE} em
        JOIN {ENTITIES_TABLE} e ON em.entity_id = e.entity_id
        WHERE em.thread_id = '{safe_tid}'
    """)
    actual_names = {r["name"].lower() for r in actual_entities}
    actual_slugs = {r["entity_id"] for r in actual_entities}

    try:
        alias_rows = _execute_sql(f"""
            SELECT alias_id, canonical_id FROM {ENTITY_ALIASES_TABLE}
            WHERE canonical_id IN (
                SELECT entity_id FROM {ENTITY_MENTIONS_TABLE}
                WHERE thread_id = '{safe_tid}'
            )
        """)
        for row in alias_rows:
            actual_slugs.add(row["alias_id"])
    except Exception:
        pass

    if _EVAL_BACKEND == "local":
        actual_rels = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE list_contains(source_threads, '{safe_tid}')
              AND relationship_type NOT IN ('SENT_TO', 'CC_TO')
        """)
    else:
        actual_rels = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE thread_id = '{safe_tid}'
              AND relationship_type NOT IN ('SENT_TO', 'CC_TO')
        """)
    actual_rel_set = {
        (r["source_entity"].lower(), r["target_entity"].lower(), r["relationship_type"])
        for r in actual_rels
    }

    gold_entities = gold.get("entities", [])
    gold_names = {e["name"].lower() for e in gold_entities}

    tp_names = set()
    fn_names = set()
    for e in gold_entities:
        if _fuzzy_entity_match(e["name"], actual_names, actual_slugs):
            tp_names.add(e["name"].lower())
        else:
            fn_names.add(e["name"].lower())

    matched_actual = set()
    for a in actual_names:
        for g in gold_names:
            if a == g or _slugify(a) == _slugify(g):
                matched_actual.add(a)
                break
            g_parts = g.split()
            if len(g_parts) >= 2 and g_parts[-1] in a:
                matched_actual.add(a)
                break
    fp_names = actual_names - matched_actual

    precision = len(tp_names) / (len(tp_names) + len(fp_names)) if (tp_names or fp_names) else 1.0
    recall = len(tp_names) / len(gold_names) if gold_names else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    gold_rels = set()
    for r in gold.get("relationships", []):
        if r.get("type") not in ("SENT_TO", "CC_TO"):
            gold_rels.add((r["source"].lower(), r["target"].lower(), r.get("type", "")))

    rel_tp = set()
    for gr in gold_rels:
        g_src, g_tgt, g_type = gr
        for ar in actual_rel_set:
            a_src, a_tgt, a_type = ar
            src_match = g_src == a_src or _slugify(g_src) == _slugify(a_src)
            tgt_match = g_tgt == a_tgt or _slugify(g_tgt) == _slugify(a_tgt)
            if src_match and tgt_match:
                rel_tp.add(gr)
                break
    rel_fn = gold_rels - rel_tp

    rel_prec = len(rel_tp) / (len(rel_tp) + len(actual_rel_set - rel_tp)) if actual_rel_set else 1.0
    rel_rec = len(rel_tp) / len(gold_rels) if gold_rels else 1.0
    rel_f1 = 2 * rel_prec * rel_rec / (rel_prec + rel_rec) if (rel_prec + rel_rec) > 0 else 0.0

    return {
        "thread_id": thread_id,
        "entity_precision": round(precision, 3),
        "entity_recall": round(recall, 3),
        "entity_f1": round(f1, 3),
        "entity_tp": len(tp_names), "entity_fn": len(fn_names), "entity_fp": len(fp_names),
        "missing_entities": sorted(fn_names),
        "rel_precision": round(rel_prec, 3),
        "rel_recall": round(rel_rec, 3),
        "rel_f1": round(rel_f1, 3),
        "rel_tp": len(rel_tp), "rel_fn": len(rel_fn),
        "missing_rels": sorted(str(r) for r in rel_fn),
    }

# COMMAND ----------

# DBTITLE 1,Step 4 — Tool Expectation Derivation
TOOL_MAP = {
    "REPORTS_TO": ["find_connections", "query_org_hierarchy"],
    "MANAGES": ["find_connections", "query_org_hierarchy"],
    "DISCUSSES": ["find_connections", "get_dyad_topics"],
    "COLLABORATES_WITH": ["find_connections", "get_emails_between"],
    "PARTICIPATES_IN": ["find_connections"],
    "RELATED_TO": ["find_connections", "trace_path"],
}


def derive_tool_expectations(gold: dict, anchor: dict) -> list[str]:
    tools = set()
    person_entities = [e for e in gold.get("entities", []) if e.get("type") == "Person"]

    if person_entities:
        tools.add("find_entity")
        tools.add("get_entity_summary")

    for rel in gold.get("relationships", []):
        rel_type = rel.get("type", "RELATED_TO")
        for t in TOOL_MAP.get(rel_type, ["find_connections"]):
            tools.add(t)

    if len(person_entities) >= 2:
        tools.add("get_emails_between")

    if gold.get("key_topics"):
        tools.add("search_emails")

    return sorted(tools)

# COMMAND ----------

# DBTITLE 1,Step 5 — Backward Question Generation
BACKWARD_QUESTION_PROMPT = """You are generating evaluation questions for a GraphRAG agent about the Enron email corpus.

Given GROUND TRUTH from a specific email, generate {n_questions} natural-language questions that require discovering this data.

ANCHOR EMAIL:
- Date: {date}
- From: {sender}
- Subject: {subject}
- Body preview: {body_preview}

GOLD ENTITIES: {entities_json}
GOLD RELATIONSHIPS: {relationships_json}
KEY TOPICS: {topics}

REQUIREMENTS:
1. Each question MUST require finding information that IS in this email
2. Questions should sound like a real investigator would ask
3. Mix difficulty: easy (entity lookup), medium (relationship), hard (evidence-backed), adversarial (cross-reference challenge)
4. For each question, specify the expected answer grounded in the email data

Return ONLY a JSON array:
[{{"question": "...", "difficulty": "easy|medium|hard|adversarial", "expected_entities": ["..."], "graph_ground_truth": "what the graph should contain", "email_ground_truth": "what the actual email says", "expected_tools": ["tool1"], "category": "entity_lookup|relationship|evidence|cross_reference|temporal"}}]"""


def generate_backward_questions(gold: dict, anchor: dict, n_questions: int = 3) -> list[dict]:
    entities_json = json.dumps(gold.get("entities", [])[:8], indent=2)
    rels_json = json.dumps(gold.get("relationships", [])[:6], indent=2)
    topics = ", ".join(gold.get("key_topics", [])[:5]) or "general"

    prompt = BACKWARD_QUESTION_PROMPT.format(
        n_questions=n_questions,
        date=anchor["date"], sender=anchor["sender"],
        subject=anchor["subject"],
        body_preview=anchor["body_text"][:800],
        entities_json=entities_json, relationships_json=rels_json,
        topics=topics,
    )

    try:
        questions = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if isinstance(questions, list):
            global_tools = derive_tool_expectations(gold, anchor)
            anchor_body_snippet = anchor["body_text"][:1500]
            canonical_questions = []
            for q in questions:
                q["anchor_thread_id"] = anchor["thread_id"]
                q["anchor_subject"] = anchor["subject"]
                q["historical_ground_truth"] = q.get("email_ground_truth", "")
                q["evidence_required"] = q.get("difficulty") in ("hard", "adversarial")
                q["anchor_body_text"] = anchor_body_snippet
                per_q_tools = [
                    t for t in (q.get("expected_tools") or [])
                    if t in VALID_TOOL_NAMES
                ]
                q["expected_tools"] = per_q_tools if per_q_tools else global_tools
                canonical_questions.append(
                    canonicalize_generated_question(
                        q,
                        corpus="enron",
                        source_type="adversarial_generated",
                        suite_tag="adversarial_candidate",
                    )
                )
            return canonical_questions[:n_questions]
    except Exception as e:
        print(f"  Question generation failed: {e}")
    return []

# COMMAND ----------

# DBTITLE 1,Agent Predict Function
_AGENT = None


def _get_agent():
    global _AGENT
    if _AGENT is None:
        from src.agent.agent_serving import GraphRAGAgent
        _AGENT = GraphRAGAgent()
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

# COMMAND ----------

# DBTITLE 1,Data-Grounded Scorers
DATA_CONTEXT = """CRITICAL CONTEXT: The agent answers questions about a knowledge graph from ~20,000 Enron emails (2000-2002). It accesses email data, extracted entities/relationships, org hierarchy, investigation timeline, and communication statistics."""


@scorer
def extraction_coverage(inputs, outputs, expectations=None):
    """Did the agent find entities we KNOW are in the graph?

    Matches by: full name, last name, first name, or email-style name (e.g., "kaminski").
    """
    expected = (expectations or {}).get("expected_entities", [])
    if not expected:
        return Feedback(value=1.0, rationale="No expected entities")
    text = (outputs if isinstance(outputs, str) else str(outputs)).lower()
    found = []
    for entity in expected:
        e_lower = entity.lower()
        if e_lower in text:
            found.append(entity)
            continue
        parts = entity.split()
        if len(parts) >= 2:
            last = parts[-1].lower()
            first = parts[0].lower()
            if last in text or first in text:
                found.append(entity)
                continue
        if e_lower.replace(" ", "_") in text or e_lower.replace(" ", ".") in text:
            found.append(entity)
            continue
    score = round(len(found) / len(expected), 2)
    missing = [e for e in expected if e not in found]
    return Feedback(value=score, rationale=f"Found {len(found)}/{len(expected)}. Missing: {missing}")


def _extract_provenance_tools(text: str) -> set[str]:
    """Extract tool names from the structured Provenance/Sources section.

    Parses patterns like:
    - `tool_name(args) → result`
    - `- tool_name(...) →`
    - **Sources**: find_entity(Jeff Skilling)
    Also falls back to scanning for known tool names in the full text.
    """
    tools_found: set[str] = set()
    provenance_pattern = re.compile(
        r'(?:^|\n)\s*[-*]*\s*["`]?(\w+)\s*\([^)]*\)\s*[→=>]',
        re.MULTILINE,
    )
    for m in provenance_pattern.finditer(text):
        candidate = m.group(1).lower()
        if candidate in VALID_TOOL_NAMES:
            tools_found.add(candidate)

    source_line_pattern = re.compile(
        r'(?:sources|tools?\s+(?:called|used|invoked))[:\s]*(.+?)(?:\n\n|\Z)',
        re.IGNORECASE | re.DOTALL,
    )
    for m in source_line_pattern.finditer(text):
        block = m.group(1)
        for tool_name in VALID_TOOL_NAMES:
            if tool_name in block.lower():
                tools_found.add(tool_name)

    return tools_found


@scorer
def tool_usage_correctness(inputs, outputs, expectations=None):
    """Did the response show evidence of using the expected tools?

    Primary: parses the structured Provenance Sources section for tool_name(...) → patterns.
    Secondary: checks semantic indicators in full text for tools not found in provenance.
    """
    expected_tools_raw = (expectations or {}).get("expected_tools", [])
    if isinstance(expected_tools_raw, str):
        expected_tools = [t.strip() for t in expected_tools_raw.split(",") if t.strip()]
    else:
        expected_tools = list(expected_tools_raw) if expected_tools_raw else []
    if not expected_tools:
        return Feedback(value=1.0, rationale="No expected tools")
    text = outputs if isinstance(outputs, str) else str(outputs)
    text_lower = text.lower()

    provenance_tools = _extract_provenance_tools(text)

    semantic_indicators = {
        "find_entity": ["found in graph", "entity_type", "entity record"],
        "find_connections": ["connections", "reports_to", "manages", "relationship"],
        "get_emails_between": ["emails between", "email evidence", "direct emails"],
        "query_org_hierarchy": ["org hierarchy", "reporting chain", "organizational structure"],
        "trace_path": ["shortest path", "connected via", "path between"],
        "search_emails": ["found emails mentioning", "email search", "matching threads"],
        "get_hierarchy_evidence": ["hierarchy evidence", "email evidence for reporting"],
        "get_email_full_body": ["full body", "email body", "complete email"],
        "get_entity_summary": ["entity summary", "pagerank", "centrality"],
        "query_timeline": ["timeline", "investigation events", "key events"],
        "get_dyad_topics": ["topics between", "discussion topics"],
        "get_relationship_evidence": ["relationship evidence", "source_threads"],
        "get_source_evidence": ["source evidence"],
        "find_top_contacts": ["top contacts", "communicated most"],
        "query_and_enrich": ["genie", "sql query"],
    }

    found = []
    missing_detail = []
    for t in expected_tools:
        if t in provenance_tools:
            found.append(t)
            continue
        inds = semantic_indicators.get(t, [])
        if any(ind in text_lower for ind in inds):
            found.append(t)
        else:
            missing_detail.append(t)
    score = round(len(found) / len(expected_tools), 2)
    return Feedback(
        value=score,
        rationale=f"Provenance: {sorted(provenance_tools)}. Found: {found}. Missing: {missing_detail}",
    )


def _structural_grounding_score(text: str, anchor_body: str,
                                 expectations: dict) -> tuple[float, str]:
    """Compute a structural grounding score based on entity/date/subject overlap.

    Returns (score, rationale) where score is 0.0-1.0.
    """
    text_lower = text.lower()
    signals = []
    total_checks = 0
    hits = 0

    expected_entities = expectations.get("expected_entities", [])
    if expected_entities:
        for ent in expected_entities:
            total_checks += 1
            e_lower = ent.lower()
            if e_lower in text_lower:
                hits += 1
                continue
            parts = ent.split()
            if len(parts) >= 2 and parts[-1].lower() in text_lower:
                hits += 1
                continue
        signals.append(f"entities: {hits}/{total_checks}")

    anchor_subject = expectations.get("anchor_subject", "")
    if anchor_subject:
        total_checks += 1
        subj_words = [w.lower() for w in anchor_subject.split() if len(w) > 3]
        subj_hits = sum(1 for w in subj_words if w in text_lower)
        if subj_words and subj_hits / len(subj_words) >= 0.3:
            hits += 1
            signals.append("subject: matched")
        else:
            signals.append("subject: missed")

    date_pattern = re.compile(r'\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}/\d{4})\b')
    anchor_dates = set(date_pattern.findall(anchor_body[:2000]))
    response_dates = set(date_pattern.findall(text))
    if anchor_dates:
        total_checks += 1
        if anchor_dates & response_dates:
            hits += 1
            signals.append("dates: matched")
        else:
            signals.append("dates: missed")

    prov_tools = _extract_provenance_tools(text)
    if prov_tools:
        total_checks += 1
        hits += 1
        signals.append(f"provenance: {len(prov_tools)} tools cited")
    elif total_checks > 0:
        total_checks += 1
        signals.append("provenance: none")

    score = hits / total_checks if total_checks > 0 else 0.5
    return round(score, 3), "; ".join(signals)


@scorer
def email_grounding(inputs, outputs, expectations=None):
    """Does the response ground claims in actual email data?

    Composite scorer: 40% structural overlap + 60% LLM-as-judge.
    Structural scoring provides a floor that prevents degradation to 0
    when the agent partially grounds but the judge is too strict.
    """
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")
    evidence_required = (expectations or {}).get("evidence_required", True)
    if not evidence_required:
        return Feedback(value=1.0, rationale="Evidence not required")

    email_gt = (expectations or {}).get("email_ground_truth", "")
    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    anchor_body = (expectations or {}).get("anchor_body_text", "")
    actual_source = anchor_body[:1500] if anchor_body else email_gt

    struct_score, struct_rationale = _structural_grounding_score(
        text, actual_source, expectations or {},
    )

    prompt = f"""{DATA_CONTEXT}

This question was generated from a SPECIFIC email in the corpus. Here is the actual email text:

--- ACTUAL EMAIL (source of truth) ---
{actual_source}
--- END EMAIL ---

Graph Ground Truth (what the graph should contain): {graph_gt}

Score how well the agent's response is GROUNDED in actual corpus data.
Look for: entity names from the email, relationship types, specific dates, subject lines,
sender/recipient info, or body text quotes that match the source email above.

IMPORTANT: The agent queries a knowledge graph (not the raw email directly). Credit
the agent for finding CORRECT information about the entities in the email even if it
doesn't quote the email verbatim. The graph may contain information aggregated from
multiple emails about the same entities.

Scoring (0.0 to 1.0):
- 1.0: Response cites specific evidence that clearly relates to the source email entities/topics
- 0.8: Finds the right entities and relationships with supporting data, even if not from this exact email
- 0.6: Identifies key entities correctly with some supporting evidence
- 0.4: Finds some relevant entities but evidence is thin or partially wrong
- 0.2: Mostly vague, only tangential connection to the source email
- 0.0: Fabricates evidence or completely misidentifies entities

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_llm_json(JUDGE_ENDPOINT, prompt)
        judge_score = float(parsed["score"])
        judge_rationale = parsed.get("justification", "")
    except Exception as e:
        judge_score = struct_score
        judge_rationale = f"Judge failed ({e}), using structural score only"

    composite = round(0.4 * struct_score + 0.6 * judge_score, 3)
    rationale = (
        f"Composite={composite} (structural={struct_score}, judge={judge_score}). "
        f"Structural: {struct_rationale}. Judge: {judge_rationale}"
    )
    return Feedback(value=composite, rationale=rationale)


@scorer
def adversarial_robustness(inputs, outputs, expectations=None):
    """For hard/adversarial questions: does the agent avoid fabrication?"""
    difficulty = (expectations or {}).get("difficulty", "easy")
    if difficulty not in ("hard", "adversarial"):
        return Feedback(value=1.0, rationale="Not adversarial")
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    prompt = f"""{DATA_CONTEXT}

Adversarial evaluation. Score the agent's HONESTY and EVIDENCE QUALITY:
1. Does it fabricate evidence? (0.0 if yes)
2. Does it honestly state limitations?
3. Does it provide SPECIFIC email citations?
4. Does it distinguish graph data from general knowledge?

Scoring: 1.0=honest+specific, 0.7=mostly honest, 0.5=mixed, 0.3=vague, 0.0=fabrication

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""
    try:
        parsed = _call_llm_json(JUDGE_ENDPOINT, prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


ADVERSARIAL_SCORERS = [extraction_coverage, tool_usage_correctness,
                        email_grounding, adversarial_robustness]

# COMMAND ----------

# DBTITLE 1,Eval Cycle Runner
def run_eval_cycle(questions, cycle_name, max_cases=None):
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
                "anchor_body_text": q.get("anchor_body_text", ""),
                "anchor_subject": q.get("anchor_subject", ""),
                "question_id": q.get("question_id", ""),
                "corpus": q.get("corpus", "enron"),
                "attorney_category": q.get("attorney_category", ""),
                "architecture_primary": q.get("architecture_primary", ""),
                "architecture_secondary": q.get("architecture_secondary", []),
                "domain_primary": q.get("domain_primary", ""),
                "domain_secondary": q.get("domain_secondary", []),
                "coverage_policy": q.get("coverage_policy", ""),
                "eval_split": q.get("eval_split", ""),
                "source_type": q.get("source_type", ""),
                "suite_tags": q.get("suite_tags", []),
            },
        })

    eval_df = pd.DataFrame(eval_records)
    print(f"\n{'='*60}")
    print(f"EVAL CYCLE: {cycle_name} ({len(eval_df)} questions)")
    print(f"{'='*60}")

    t0 = time.time()
    with mlflow.start_run(run_name=f"adversarial_{cycle_name}"):
        results = mlflow.genai.evaluate(
            data=eval_df, predict_fn=predict_fn, scorers=ADVERSARIAL_SCORERS,
        )
    elapsed = time.time() - t0
    results_df = results.tables["eval_results"]

    scorer_names = set()
    for s in ADVERSARIAL_SCORERS:
        for attr in ("name", "__name__"):
            if hasattr(s, attr):
                scorer_names.add(getattr(s, attr))
                break
    score_cols = []
    for c in results_df.columns:
        if not c.endswith("/value"):
            continue
        col_name = c.replace("/value", "")
        if scorer_names and col_name not in scorer_names:
            continue
        try:
            results_df[c] = pd.to_numeric(results_df[c], errors="coerce")
            if results_df[c].notna().any():
                score_cols.append(c)
        except Exception:
            pass
    if not score_cols:
        score_cols = [c for c in results_df.columns
                      if c.endswith("/value")]
        for c in score_cols:
            results_df[c] = pd.to_numeric(results_df[c], errors="coerce")
    scorer_scores = {}
    if score_cols:
        means = results_df[score_cols].mean()
        for col in score_cols:
            scorer_scores[col.replace("/value", "")] = round(float(means[col]), 3)
        overall = round(float(means.mean()), 3)
    else:
        overall = 0.0

    if score_cols:
        numeric_scores = results_df[score_cols].apply(pd.to_numeric, errors="coerce")
        results_df["avg_score"] = numeric_scores.mean(axis=1).astype(float)
    else:
        results_df["avg_score"] = 0.0

    worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
    worst_qs = []
    for _, row in worst.iterrows():
        q = row.get("inputs/question", row.get("inputs", ""))
        if isinstance(q, dict):
            q = q.get("question", str(q))
        worst_qs.append({"question": str(q)[:120], "avg_score": round(float(row["avg_score"]), 3)})

    print(f"\n  Scores ({cycle_name}):")
    for name, score in sorted(scorer_scores.items()):
        bar_len = int(score * 20) if not pd.isna(score) else 0
        print(f"    {name:30s}: {score:.3f} {'█' * bar_len}")
    print(f"    {'OVERALL':30s}: {overall:.3f}")
    print(f"    Time: {elapsed:.0f}s ({elapsed / max(len(data), 1):.1f}s/q)")
    if worst_qs:
        print(f"\n  Worst:")
        for wq in worst_qs[:3]:
            print(f"    [{wq['avg_score']:.2f}] {wq['question'][:80]}")

    return {"cycle": cycle_name, "overall": overall, "scorer_scores": scorer_scores,
            "worst_questions": worst_qs, "num_questions": len(data),
            "elapsed_s": round(elapsed, 1), "results_df": results_df}

# COMMAND ----------

# DBTITLE 1,Adversarial Escalator
def escalate_questions(failures, existing_questions, cycle):
    if not failures:
        return []
    failure_summary = "\n".join(
        f"  - Q: {f['question'][:80]}  Score: {f['avg_score']}" for f in failures[:5])

    strategy = (
        "probe extraction gaps — ask about entities the graph should have" if cycle <= 2
        else "demand specific email evidence — require dates, subjects, sender/recipient details" if cycle <= 4
        else "adversarial cross-referencing — combine multiple entities, ask for evidence chains"
    )
    prompt = f"""You are an adversarial evaluator for a GraphRAG system about Enron emails.

The agent FAILED on these questions:
{failure_summary}

Cycle: {cycle}, Strategy: {strategy}

Generate 5 NEW harder questions that target the same weaknesses.
Focus on: extraction gaps, evidence traceability, cross-referencing, and honest limitation statements.

IMPORTANT: Each question must name SPECIFIC Enron people or entities (e.g., "Kenneth Lay", "Jeff Skilling",
"Enron Broadband Services") — do NOT generate vague questions about unnamed entities.

Return ONLY a JSON array:
[{{"question": "...", "difficulty": "hard|adversarial", "expected_entities": ["full name 1", "full name 2"], "graph_ground_truth": "...", "email_ground_truth": "...", "expected_tools": ["find_entity", "find_connections"], "category": "extraction_gap|evidence_demand|cross_reference|adversarial_probe", "evidence_required": true}}]"""

    anchor_body_pool = {}
    for eq in existing_questions:
        tid = eq.get("anchor_thread_id", "")
        if tid and eq.get("anchor_body_text"):
            anchor_body_pool[tid] = eq["anchor_body_text"]

    try:
        new_qs = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if isinstance(new_qs, list):
            pool_bodies = list(anchor_body_pool.values())
            canonical_questions = []
            for i, q in enumerate(new_qs):
                q.setdefault("historical_ground_truth", q.get("email_ground_truth", ""))
                q.setdefault("evidence_required", True)
                q["expected_tools"] = [t for t in q.get("expected_tools", [])
                                       if t in VALID_TOOL_NAMES]
                if not q["expected_tools"]:
                    q["expected_tools"] = ["find_entity", "find_connections", "search_emails"]
                if not q.get("anchor_body_text") and pool_bodies:
                    q["anchor_body_text"] = pool_bodies[i % len(pool_bodies)]
                q.setdefault("anchor_body_text", "")
                q.setdefault("expected_entities", [])
                canonical_questions.append(
                    canonicalize_generated_question(
                        q,
                        corpus="enron",
                        source_type="adversarial_generated",
                        suite_tag="adversarial_candidate",
                    )
                )
            print(f"  Escalator: {len(new_qs)} harder questions (cycle {cycle}, strategy: {strategy[:40]})")
            return canonical_questions[:5]
    except Exception as e:
        print(f"  Escalator failed: {e}")
    return []

# COMMAND ----------

# DBTITLE 1,Main Pipeline
def run_adversarial_pipeline(n_anchors=30, max_cycles=6, max_cases=None,
                              baseline_only=False, seed=42,
                              force_regen=False):
    """Full data-first adversarial evaluation pipeline.

    Args:
        force_regen: If True, skip cache and re-generate ground truth.
            Set GRAPHRAG_EVAL_BACKEND=local to use DuckDB for all SQL.
    """
    print("=" * 60)
    print("ADVERSARIAL VERIFICATION LOOP — DATA-FIRST EVALUATION")
    print(f"  Backend: {_EVAL_BACKEND} | Seed: {seed} | Anchors: {n_anchors}")
    print("=" * 60)

    cache_key = _cache_key(n_anchors, seed)
    cached = None if force_regen else _load_cache(cache_key)

    if cached:
        anchors = cached["anchors"]
        gold_extractions = cached["gold_extractions"]
        audits = cached["audits"]
        all_questions = cached["all_questions"]
        audit_df = pd.DataFrame(audits)
        print(f"\n  Loaded from cache: {len(anchors)} anchors, {len(all_questions)} questions")
        print(f"  Entity P/R/F1: {audit_df['entity_precision'].mean():.3f} / "
              f"{audit_df['entity_recall'].mean():.3f} / {audit_df['entity_f1'].mean():.3f}")
    else:
        # Step 1: Sample
        print("\n--- STEP 1: Sample Anchor Emails ---")
        t_step = time.time()
        anchors = sample_anchor_emails(n=n_anchors, seed=seed)
        print(f"  ({time.time() - t_step:.1f}s)")

        # Step 2: Gold Extraction (parallel)
        print("\n--- STEP 2: Gold Extraction (parallel) ---")
        t_step = time.time()
        gold_extractions = _parallel_gold_extract(anchors, max_workers=4)

        total_entities = sum(len(g["entities"]) for g in gold_extractions)
        total_rels = sum(len(g["relationships"]) for g in gold_extractions)
        print(f"\n  Gold: {total_entities} entities, {total_rels} relationships ({time.time() - t_step:.1f}s)")

        # Step 3: Extraction Audit
        print("\n--- STEP 3: Extraction Audit ---")
        t_step = time.time()
        audits = []
        for gold in gold_extractions:
            audits.append(audit_extraction(gold, gold["anchor_thread_id"]))
        audit_df = pd.DataFrame(audits)

        print(f"\n  Entity P/R/F1: {audit_df['entity_precision'].mean():.3f} / "
              f"{audit_df['entity_recall'].mean():.3f} / {audit_df['entity_f1'].mean():.3f}")
        print(f"  Rel    P/R/F1: {audit_df['rel_precision'].mean():.3f} / "
              f"{audit_df['rel_recall'].mean():.3f} / {audit_df['rel_f1'].mean():.3f}")
        print(f"  ({time.time() - t_step:.1f}s)")

        # Step 4+5: Tool Expectations + Question Generation
        print("\n--- STEP 4+5: Backward Question Generation ---")
        t_step = time.time()
        all_questions = []
        for i, (gold, anchor) in enumerate(zip(gold_extractions, anchors)):
            if not gold.get("entities"):
                continue
            n_qs = 2 if len(gold["entities"]) < 3 else 3
            qs = generate_backward_questions(gold, anchor, n_questions=n_qs)
            all_questions.extend(qs)
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(anchors)}] {len(all_questions)} questions")

        print(f"\n  Generated {len(all_questions)} backward questions ({time.time() - t_step:.1f}s)")
        difficulty_dist = pd.Series([q.get("difficulty", "?") for q in all_questions]).value_counts()
        print(f"  Difficulty: {dict(difficulty_dist)}")

        if not all_questions:
            print("ERROR: No questions generated. Check LLM endpoints.")
            return {"error": "no questions generated"}

        # Save cache for next run
        _save_cache(cache_key, anchors, gold_extractions, audits, all_questions)

    # Step 6: Adversarial Loop
    print("\n--- STEP 6: Adversarial Evaluation Loop ---")
    history = []
    train = [q for q in all_questions if q.get("eval_split") == "train"]
    test = [q for q in all_questions if q.get("eval_split") == "test"]
    holdout = [q for q in all_questions if q.get("eval_split") == "holdout"] or test[-2:]
    print(f"  Splits: train={len(train)}, test={len(test)}, holdout={len(holdout)}")

    baseline = run_eval_cycle(train, "baseline", max_cases=max_cases)
    history.append(baseline)

    if baseline_only:
        return {"history": history, "audit": audit_df.to_dict("records"),
                "questions": len(all_questions)}

    for cycle in range(1, max_cycles + 1):
        new_qs = escalate_questions(history[-1]["worst_questions"], train, cycle)
        train = new_qs + train

        result = run_eval_cycle(train, f"cycle_{cycle}", max_cases=max_cases)
        history.append(result)

        if len(history) >= PLATEAU_WINDOW + 1:
            gains = [(history[i]["overall"] - history[i-1]["overall"]) * 100
                     for i in range(-PLATEAU_WINDOW, 0)]
            if all(abs(g) < PLATEAU_THRESHOLD for g in gains):
                print(f"\n  PLATEAU: avg gain={sum(gains)/len(gains):.1f}pp")
                test_r = run_eval_cycle(test, "test_final", max_cases=max_cases)
                holdout_r = run_eval_cycle(holdout, "holdout_final", max_cases=max_cases)
                history.extend([test_r, holdout_r])
                break

        gain = (history[-1]["overall"] - history[-2]["overall"]) * 100
        print(f"\n  Cycle {cycle} gain: {gain:+.1f}pp")

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"\nExtraction Pipeline:")
    print(f"  Entity F1: {audit_df['entity_f1'].mean():.3f}")
    print(f"  Rel F1:    {audit_df['rel_f1'].mean():.3f}")
    print(f"\nAgent Performance:")
    prev = None
    for h in history:
        g = f"  ({(h['overall'] - prev)*100:+.1f}pp)" if prev is not None else ""
        print(f"  {h['cycle']:20s}: {h['overall']:.3f} ({h['num_questions']}q){g}")
        prev = h["overall"]

    return {"history": history, "audit": audit_df.to_dict("records"),
            "questions": len(all_questions), "final_score": history[-1]["overall"]}

# COMMAND ----------

# DBTITLE 1,CLI Entry Point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data-first adversarial evaluation")
    parser.add_argument("--anchors", type=int, default=15, help="Number of anchor emails to sample")
    parser.add_argument("--max-cycles", type=int, default=6, help="Max improvement cycles")
    parser.add_argument("--cases", type=int, default=None, help="Limit questions per eval cycle")
    parser.add_argument("--baseline-only", action="store_true", help="Only run baseline, no loop")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling")
    parser.add_argument("--force-regen", action="store_true",
                        help="Force regeneration of ground truth (ignore cache)")
    parser.add_argument("--local", action="store_true",
                        help="Use DuckDB local backend for SQL (sets GRAPHRAG_EVAL_BACKEND=local "
                             "and GRAPHRAG_BACKEND=local for agent tools)")
    args = parser.parse_args()

    if args.local:
        os.environ["GRAPHRAG_EVAL_BACKEND"] = "local"
        os.environ["GRAPHRAG_BACKEND"] = "local"
        os.environ.setdefault("GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb")
        globals()["_EVAL_BACKEND"] = "local"

    run_adversarial_pipeline(
        n_anchors=args.anchors,
        max_cycles=args.max_cycles,
        max_cases=args.cases,
        baseline_only=args.baseline_only,
        seed=args.seed,
        force_regen=args.force_regen,
    )
