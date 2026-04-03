"""SQL-First Evaluation Pipeline — Redesigned Adversarial Verification Loop.

Generates ground-truth Q/A pairs **backward from raw emails** with:
  - Graph facts via SQL (not LLM re-extraction)
  - Tool expectations from the pattern registry (not LLM guesses)
  - Expected answers via SQL (deterministic)
  - Hybrid scoring: deterministic tool/entity/latency + 1 LLM judge for synthesis

Usage (CLI):
    python -m src.evaluation.sql_first_eval --anchors 15 --seed 42
    python -m src.evaluation.sql_first_eval --anchors 10 --baseline-only --local
    python -m src.evaluation.sql_first_eval --max-cycles 3 --cases 20
"""

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_project_root = os.path.join(os.path.dirname(__file__), "..", "..")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")

from concurrent.futures import ThreadPoolExecutor, as_completed

import mlflow
import pandas as pd
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

from src.evaluation.question_bank import canonicalize_generated_question

# ---------------------------------------------------------------------------
# Table configuration (mirrors src/config.py without notebook dependency)
# ---------------------------------------------------------------------------
CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
ENRON_SCHEMA = os.environ.get("GRAPHRAG_SCHEMA", "graphrag_enron")

_T = f"{CATALOG}.{ENRON_SCHEMA}"
EMAILS_TABLE = f"{_T}.emails"
THREADS_TABLE = f"{_T}.threads"
ENTITIES_TABLE = f"{_T}.entities"
RELATIONSHIPS_TABLE = f"{_T}.relationships"
ENTITY_MENTIONS_TABLE = f"{_T}.entity_mentions"
ENTITY_ANALYTICS_TABLE = f"{_T}.entity_analytics"
ENTITY_ALIASES_TABLE = f"{_T}.entity_aliases"
COMMUNICATION_DYADS_TABLE = f"{_T}.communication_dyads"
ORG_HIERARCHY_TABLE = f"{_T}.org_hierarchy"
INVESTIGATION_TIMELINE_TABLE = f"{_T}.investigation_timeline"
TOPIC_TAXONOMY_TABLE = f"{_T}.topic_taxonomy"
PERSON_ACTIVITY_TABLE = f"{_T}.person_activity"

JUDGE_ENDPOINT = os.environ.get("GRAPHRAG_JUDGE_ENDPOINT", "databricks-claude-sonnet-4-6")
PROPOSER_ENDPOINT = os.environ.get("GRAPHRAG_PROPOSER_ENDPOINT", "databricks-claude-sonnet-4-6")

PLATEAU_THRESHOLD = 1.5
PLATEAU_WINDOW = 2

# ---------------------------------------------------------------------------
# Pattern registry import — THE source of truth for tool expectations
# ---------------------------------------------------------------------------
from src.agent.pattern_registry import PATTERN_REGISTRY, ExecutionStep

PRIMITIVES = list(PATTERN_REGISTRY.keys())

PRIMITIVE_TOOL_MAP: dict[str, list[str]] = {}
for pname, pattern in PATTERN_REGISTRY.items():
    PRIMITIVE_TOOL_MAP[pname] = [step.tool_name for step in pattern.steps]


# =========================================================================
# SQL Executor (DuckDB local or Databricks remote) — reused from AVL
# =========================================================================
_EVAL_BACKEND = os.environ.get(
    "GRAPHRAG_EVAL_BACKEND",
    os.environ.get("GRAPHRAG_BACKEND", "lakebase"),
)
_DUCKDB_CONN = None
_DUCKDB_LOCK = None
_FQN_STRIP = f"{CATALOG}.{ENRON_SCHEMA}."


def _get_duckdb():
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
        for wh in warehouses:
            if wh.state and str(wh.state).upper() == "RUNNING":
                warehouse_id = wh.id
                break
        if not warehouse_id and warehouses:
            warehouse_id = warehouses[0].id
        if not warehouse_id:
            raise RuntimeError("No SQL warehouses found")
    stmt = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=query, wait_timeout="50s",
    )
    if not stmt.result or not stmt.result.data_array:
        return []
    columns = [c.name for c in stmt.manifest.schema.columns]
    return [dict(zip(columns, row_data)) for row_data in stmt.result.data_array]


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


# =========================================================================
# STEP 1 — Sample Anchor Emails
# =========================================================================
def sample_anchor_emails(n: int = 30, seed: int = 42) -> list[dict]:
    """Sample diverse anchor emails from the corpus."""
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
        })
    print(f"  Sampled {len(anchors)} anchor emails")
    return anchors


# =========================================================================
# STEP 2 — Graph-Fact Extraction (SQL, not LLM)
# =========================================================================
@dataclass
class GraphFacts:
    """What the REAL extraction pipeline put into the graph for a thread."""
    thread_id: str
    entities: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    org_hierarchy_hits: list[dict] = field(default_factory=list)
    dyad_hits: list[dict] = field(default_factory=list)
    timeline_hits: list[dict] = field(default_factory=list)
    person_names: list[str] = field(default_factory=list)
    org_names: list[str] = field(default_factory=list)
    topic_keywords: list[str] = field(default_factory=list)
    testable_primitives: list[str] = field(default_factory=list)


def extract_graph_facts(anchor: dict) -> GraphFacts:
    """Query the graph for what the extraction pipeline stored for this thread."""
    tid = anchor["thread_id"].replace("'", "''")
    facts = GraphFacts(thread_id=anchor["thread_id"])

    # Entities extracted from this thread
    facts.entities = _execute_sql(f"""
        SELECT DISTINCT e.name, e.entity_type, e.entity_id
        FROM {ENTITY_MENTIONS_TABLE} em
        JOIN {ENTITIES_TABLE} e ON em.entity_id = e.entity_id
        WHERE em.thread_id = '{tid}'
    """)
    for e in facts.entities:
        if e.get("entity_type") == "Person":
            facts.person_names.append(e["name"])
        elif e.get("entity_type") in ("Organization", "Division"):
            facts.org_names.append(e["name"])

    # Relationships extracted from this thread
    if _EVAL_BACKEND == "local":
        facts.relationships = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE list_contains(source_threads, '{tid}')
        """)
    else:
        facts.relationships = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE thread_id = '{tid}'
        """)

    # Org hierarchy entries for persons in this thread
    if facts.person_names:
        safe_names = [n.replace("'", "''").lower() for n in facts.person_names[:10]]
        name_list = ", ".join(f"'{n}'" for n in safe_names)
        facts.org_hierarchy_hits = _execute_sql(f"""
            SELECT person_name, title, reports_to_name, reports_to_title,
                   effective_from, effective_to
            FROM {ORG_HIERARCHY_TABLE}
            WHERE LOWER(person_name) IN ({name_list})
        """)

    # Communication dyads involving persons in this thread
    if len(facts.person_names) >= 2:
        p0 = facts.person_names[0].replace("'", "''").lower()
        p1 = facts.person_names[1].replace("'", "''").lower()
        facts.dyad_hits = _execute_sql(f"""
            SELECT person_a, person_b, total_emails
            FROM {COMMUNICATION_DYADS_TABLE}
            WHERE (LOWER(person_a) LIKE '%{p0}%' AND LOWER(person_b) LIKE '%{p1}%')
               OR (LOWER(person_a) LIKE '%{p1}%' AND LOWER(person_b) LIKE '%{p0}%')
            LIMIT 5
        """)

    # Topic keywords from the thread
    thread_topics = _execute_sql(f"""
        SELECT key_topics FROM {THREADS_TABLE}
        WHERE thread_id = '{tid}'
    """)
    if thread_topics and thread_topics[0].get("key_topics"):
        kt = thread_topics[0]["key_topics"]
        if isinstance(kt, str):
            try:
                facts.topic_keywords = json.loads(kt)
            except (json.JSONDecodeError, TypeError):
                facts.topic_keywords = [t.strip() for t in kt.split(",") if t.strip()]
        elif isinstance(kt, list):
            facts.topic_keywords = kt

    # Determine which primitives this email can test
    facts.testable_primitives = _derive_testable_primitives(facts, anchor)

    return facts


def _derive_testable_primitives(facts: GraphFacts, anchor: dict) -> list[str]:
    """Determine which MECE primitives this email's graph facts can exercise."""
    primitives = []

    if facts.org_hierarchy_hits:
        primitives.append("entity_structure")

    if facts.person_names:
        primitives.append("entity_explore")

    if len(facts.person_names) >= 2 or facts.dyad_hits:
        primitives.append("entity_pair")

    if anchor.get("date"):
        primitives.append("timeline")

    if facts.topic_keywords or facts.org_names:
        primitives.append("keyword_search")

    if facts.dyad_hits:
        primitives.append("genie_analytics")

    if not primitives:
        primitives.append("general")

    return primitives


# =========================================================================
# STEP 3 — Primitive-Aware Proposer
# =========================================================================
PROPOSER_PROMPT = """You are generating evaluation questions for a GraphRAG agent about the Enron email corpus.

You have a SPECIFIC email and the ACTUAL graph data extracted from it. Generate {n_questions} natural-language questions that an investigator would ask and that require the agent to find this data.

ANCHOR EMAIL:
- Date: {date}
- From: {sender}
- Subject: {subject}
- Body preview: {body_preview}

GRAPH FACTS (what the extraction pipeline ACTUALLY stored):
- Entities: {entities_json}
- Relationships: {relationships_json}
- Org hierarchy: {org_json}
- Topics: {topics}

TARGET PRIMITIVES (generate questions that exercise these):
{primitives_desc}

REQUIREMENTS:
1. Each question MUST be answerable using the graph facts above
2. Each question MUST map to exactly ONE of the target primitives listed
3. Questions should sound like a real corporate investigator would ask
4. For each question, specify:
   - The target primitive (from the list above)
   - The primary entity name(s) — use EXACT names from the graph facts
   - What the correct answer is, based on the graph facts
5. Mix difficulty: easy (single entity lookup), medium (relationship traversal), hard (evidence-backed multi-hop)
6. Do NOT invent entity names. Use ONLY names from the graph facts above.

Return ONLY a JSON array:
[{{"question": "...", "primitive": "entity_structure|entity_explore|entity_pair|timeline|keyword_search|genie_analytics|general", "primary_entity": "exact name from graph", "secondary_entity": "exact name or empty", "expected_answer_summary": "brief answer based on graph facts", "difficulty": "easy|medium|hard", "keywords": "search terms if keyword_search"}}]"""

PRIMITIVES_DESCRIPTION = {
    "entity_structure": "entity_structure — 'who reports to X?', 'what is X's role?', 'X's org chart'. Requires: one person name from graph.",
    "entity_explore": "entity_explore — 'who did X email most?', 'what did X discuss?', 'X's activities'. Requires: one person name.",
    "entity_pair": "entity_pair — 'how are A and B connected?', 'did A and B email each other?'. Requires: two person names.",
    "timeline": "timeline — 'what happened in [date]?', 'events around [time period]'. Requires: a date or date range.",
    "keyword_search": "keyword_search — 'what emails mention [topic]?', 'what was discussed about [project]?'. Requires: topic keywords.",
    "genie_analytics": "genie_analytics — 'how many emails between A and B?', 'who sent the most emails?'. Requires: a countable/rankable question.",
    "general": "general — broad investigative questions. Use only when no other primitive fits.",
}


def generate_questions(
    facts: GraphFacts,
    anchor: dict,
    n_questions: int = 3,
) -> list[dict]:
    """Generate questions using the LLM proposer, with graph-fact grounding."""
    if not facts.entities:
        return []

    primitives_desc = "\n".join(
        f"  - {PRIMITIVES_DESCRIPTION[p]}"
        for p in facts.testable_primitives
        if p in PRIMITIVES_DESCRIPTION
    )

    entities_json = json.dumps(facts.entities[:10], indent=2, default=str)
    rels_json = json.dumps(facts.relationships[:8], indent=2, default=str)
    org_json = json.dumps(facts.org_hierarchy_hits[:5], indent=2, default=str) if facts.org_hierarchy_hits else "None found"
    topics = ", ".join(facts.topic_keywords[:5]) or "general"

    prompt = PROPOSER_PROMPT.format(
        n_questions=n_questions,
        date=anchor["date"],
        sender=anchor["sender"],
        subject=anchor["subject"],
        body_preview=anchor["body_text"][:800],
        entities_json=entities_json,
        relationships_json=rels_json,
        org_json=org_json,
        topics=topics,
        primitives_desc=primitives_desc,
    )

    try:
        questions = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if not isinstance(questions, list):
            return []
    except (json.JSONDecodeError, Exception) as e:
        print(f"  Proposer failed for {anchor['subject'][:50]}: {e}")
        return []

    enriched = []
    for q in questions[:n_questions]:
        primitive = q.get("primitive", "general")
        if primitive not in PRIMITIVE_TOOL_MAP:
            primitive = "general"

        candidate = {
            "question": q["question"],
            "primitive": primitive,
            "expected_tools": PRIMITIVE_TOOL_MAP[primitive],
            "primary_entity": q.get("primary_entity", ""),
            "secondary_entity": q.get("secondary_entity", ""),
            "expected_answer_summary": q.get("expected_answer_summary", ""),
            "expected_entities": _collect_expected_entities(q, facts),
            "difficulty": q.get("difficulty", "medium"),
            "keywords": q.get("keywords", ""),
            "anchor_thread_id": anchor["thread_id"],
            "anchor_subject": anchor["subject"],
            "anchor_body_text": anchor["body_text"][:1500],
            "anchor_date": anchor["date"],
            "anchor_sender": anchor["sender"],
            "graph_entities": [e["name"] for e in facts.entities],
            "graph_relationships": [
                f"{r['source_entity']} -{r['relationship_type']}-> {r['target_entity']}"
                for r in facts.relationships[:10]
            ],
        }
        enriched.append(
            canonicalize_generated_question(
                candidate,
                corpus="enron",
                source_type="sql_first_generated",
                suite_tag="sql_first_candidate",
            )
        )
    return enriched


def _collect_expected_entities(q: dict, facts: GraphFacts) -> list[str]:
    """Build the expected_entities list from the question and graph facts."""
    entities = set()
    primary = q.get("primary_entity", "")
    secondary = q.get("secondary_entity", "")
    if primary:
        entities.add(primary)
    if secondary:
        entities.add(secondary)
    if not entities:
        for e in facts.entities[:3]:
            if e.get("entity_type") == "Person":
                entities.add(e["name"])
    return sorted(entities)


# =========================================================================
# STEP 4 — SQL-Based Expected Answer Computation
# =========================================================================
def compute_sql_ground_truth(question: dict) -> dict:
    """Run the same SQL the agent tools would execute to produce ground truth."""
    primitive = question["primitive"]
    primary = question.get("primary_entity", "").replace("'", "''")
    secondary = question.get("secondary_entity", "").replace("'", "''")
    keywords = question.get("keywords", "").replace("'", "''")

    gt = {"sql_results": {}, "graph_ground_truth": ""}
    parts = []

    if primitive == "entity_structure" and primary:
        rows = _execute_sql(f"""
            SELECT person_name, title, reports_to_name, reports_to_title,
                   effective_from, effective_to
            FROM {ORG_HIERARCHY_TABLE}
            WHERE LOWER(person_name) LIKE '%{primary.lower()}%'
               OR LOWER(reports_to_name) LIKE '%{primary.lower()}%'
        """)
        gt["sql_results"]["org_hierarchy"] = rows
        if rows:
            parts.append(f"Org hierarchy: {json.dumps(rows[:5], default=str)}")

        rels = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE (LOWER(source_entity) LIKE '%{primary.lower()}%'
               OR LOWER(target_entity) LIKE '%{primary.lower()}%')
              AND relationship_type IN ('REPORTS_TO', 'MANAGES')
            LIMIT 20
        """)
        gt["sql_results"]["reports_manages"] = rels
        if rels:
            parts.append(f"Relationships: {json.dumps(rels[:10], default=str)}")

    elif primitive == "entity_explore" and primary:
        dyads = _execute_sql(f"""
            SELECT person_a, person_b, total_emails
            FROM {COMMUNICATION_DYADS_TABLE}
            WHERE LOWER(person_a) LIKE '%{primary.lower()}%'
               OR LOWER(person_b) LIKE '%{primary.lower()}%'
            ORDER BY total_emails DESC
            LIMIT 5
        """)
        gt["sql_results"]["top_contacts"] = dyads
        if dyads:
            parts.append(f"Top contacts: {json.dumps(dyads, default=str)}")

    elif primitive == "entity_pair" and primary and secondary:
        dyads = _execute_sql(f"""
            SELECT person_a, person_b, total_emails, sent_a_to_b, sent_b_to_a
            FROM {COMMUNICATION_DYADS_TABLE}
            WHERE (LOWER(person_a) LIKE '%{primary.lower()}%'
               AND LOWER(person_b) LIKE '%{secondary.lower()}%')
              OR (LOWER(person_a) LIKE '%{secondary.lower()}%'
               AND LOWER(person_b) LIKE '%{primary.lower()}%')
            LIMIT 5
        """)
        gt["sql_results"]["dyad"] = dyads
        if dyads:
            parts.append(f"Communication: {json.dumps(dyads, default=str)}")

        rels = _execute_sql(f"""
            SELECT source_entity, target_entity, relationship_type
            FROM {RELATIONSHIPS_TABLE}
            WHERE (LOWER(source_entity) LIKE '%{primary.lower()}%'
               AND LOWER(target_entity) LIKE '%{secondary.lower()}%')
              OR (LOWER(source_entity) LIKE '%{secondary.lower()}%'
               AND LOWER(target_entity) LIKE '%{primary.lower()}%')
            LIMIT 10
        """)
        gt["sql_results"]["pair_relationships"] = rels
        if rels:
            parts.append(f"Relationships: {json.dumps(rels[:5], default=str)}")

    elif primitive == "timeline":
        date_str = question.get("anchor_date", "")
        if date_str:
            rows = _execute_sql(f"""
                SELECT event_date, event_description, category, people_involved
                FROM {INVESTIGATION_TIMELINE_TABLE}
                ORDER BY event_date
                LIMIT 10
            """)
            gt["sql_results"]["timeline"] = rows
            if rows:
                parts.append(f"Timeline events: {len(rows)} events")

    elif primitive == "keyword_search" and (keywords or primary):
        search_term = keywords or primary
        rows = _execute_sql(f"""
            SELECT e.name, e.entity_type, e.description
            FROM {ENTITIES_TABLE} e
            WHERE LOWER(e.name) LIKE '%{search_term.lower()}%'
            LIMIT 10
        """)
        gt["sql_results"]["matching_entities"] = rows
        if rows:
            parts.append(f"Matching entities: {json.dumps(rows[:5], default=str)}")

    elif primitive == "genie_analytics" and primary:
        activity = _execute_sql(f"""
            SELECT entity_name, total_sent, total_received
            FROM {PERSON_ACTIVITY_TABLE}
            WHERE LOWER(entity_name) LIKE '%{primary.lower()}%'
            LIMIT 5
        """)
        gt["sql_results"]["activity"] = activity
        if activity:
            parts.append(f"Activity: {json.dumps(activity, default=str)}")

    gt["graph_ground_truth"] = "; ".join(parts) if parts else question.get("expected_answer_summary", "")
    return gt


# =========================================================================
# STEP 5 — Agent Execution
# =========================================================================
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


# =========================================================================
# STEP 6 — Hybrid Scorers
# =========================================================================

# --- Deterministic scorer: entity recall ---
@scorer
def entity_recall(inputs, outputs, expectations=None):
    """Did the response mention the expected entities?"""
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
        if len(parts) >= 2 and parts[-1].lower() in text:
            found.append(entity)
            continue
        if e_lower.replace(" ", "_") in text or e_lower.replace(" ", ".") in text:
            found.append(entity)
    score = round(len(found) / len(expected), 2)
    missing = [e for e in expected if e not in found]
    return Feedback(value=score, rationale=f"Found {len(found)}/{len(expected)}. Missing: {missing}")


# --- Deterministic scorer: tool plan recall ---
@scorer
def tool_plan_recall(inputs, outputs, expectations=None):
    """Were the expected tools invoked? Checks provenance section of the response."""
    expected_tools = (expectations or {}).get("expected_tools", [])
    if not expected_tools:
        return Feedback(value=1.0, rationale="No expected tools")
    text = outputs if isinstance(outputs, str) else str(outputs)
    text_lower = text.lower()

    tool_provenance_re = re.compile(
        r'(?:^|\n)\s*[-*]*\s*["`]?(\w+)\s*\([^)]*\)\s*[→=>]',
        re.MULTILINE,
    )
    found_in_provenance = set()
    for m in tool_provenance_re.finditer(text):
        found_in_provenance.add(m.group(1).lower())

    source_block_re = re.compile(
        r'(?:sources|tools?\s+(?:called|used|invoked))[:\s]*(.+?)(?:\n\n|\Z)',
        re.IGNORECASE | re.DOTALL,
    )
    for m in source_block_re.finditer(text):
        block = m.group(1).lower()
        for tool_name in set(expected_tools):
            if tool_name in block:
                found_in_provenance.add(tool_name)

    expected_unique = set(expected_tools)
    found = expected_unique & found_in_provenance
    recall = round(len(found) / len(expected_unique), 2) if expected_unique else 1.0
    missing = sorted(expected_unique - found)
    extra = sorted(found_in_provenance - expected_unique)
    return Feedback(
        value=recall,
        rationale=f"Tool recall: {len(found)}/{len(expected_unique)}. Missing: {missing}. Extra: {extra}",
    )


# --- Deterministic scorer: graph grounding overlap ---
@scorer
def graph_fact_overlap(inputs, outputs, expectations=None):
    """Do the graph relationships from the anchor appear in the response?"""
    graph_rels = (expectations or {}).get("graph_relationships", [])
    if not graph_rels:
        return Feedback(value=1.0, rationale="No graph relationships to check")
    text = (outputs if isinstance(outputs, str) else str(outputs)).lower()
    hits = 0
    for rel_str in graph_rels:
        parts = rel_str.lower().replace("->", " ").replace("-", " ").split()
        key_tokens = [p for p in parts if len(p) > 3]
        if sum(1 for tok in key_tokens if tok in text) >= max(1, len(key_tokens) // 2):
            hits += 1
    score = round(hits / len(graph_rels), 2) if graph_rels else 1.0
    return Feedback(value=score, rationale=f"Graph fact overlap: {hits}/{len(graph_rels)}")


# --- LLM judge: synthesis quality (the only LLM scorer) ---

DATA_CONTEXT = """CRITICAL CONTEXT: The agent answers questions about a knowledge graph from ~20,000 Enron emails (2000-2002). It accesses email data, extracted entities/relationships, org hierarchy, investigation timeline, and communication statistics."""


@scorer
def synthesis_quality(inputs, outputs, expectations=None):
    """LLM judge for overall answer quality: grounding, completeness, honesty."""
    text = outputs if isinstance(outputs, str) else str(outputs)
    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent error: {text[:100]}")

    graph_gt = (expectations or {}).get("graph_ground_truth", "")
    anchor_body = (expectations or {}).get("anchor_body_text", "")
    question = inputs.get("question", "") if isinstance(inputs, dict) else str(inputs)

    prompt = f"""{DATA_CONTEXT}

This question was generated from a SPECIFIC email in the corpus.

Question: {question}
Graph Ground Truth (what the graph contains): {graph_gt}
Source email excerpt: {anchor_body[:1000]}

Score the agent's response on these dimensions:
1. Did it find the core facts that ARE in the graph?
2. Did it properly ground claims in tool results (not training data)?
3. Did it avoid fabricating evidence?
4. Is it well-structured with provenance?

Scoring (0.0 to 1.0):
- 1.0: Found graph facts, properly grounded, no fabrication, has provenance
- 0.7: Most facts found, minor grounding gaps, no fabrication
- 0.5: Some facts found, partial grounding
- 0.3: Few facts, vague, thin evidence
- 0.0: Fabrication, contradicts graph, or error

Agent Response:
{text[:3000]}

Return ONLY JSON: {{"score": float, "justification": string}}"""

    try:
        parsed = _call_llm_json(JUDGE_ENDPOINT, prompt)
        return Feedback(value=float(parsed["score"]), rationale=parsed.get("justification", ""))
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {e}")


ALL_SCORERS = [entity_recall, tool_plan_recall, graph_fact_overlap, synthesis_quality]


# =========================================================================
# STEP 7 — Eval Cycle Runner
# =========================================================================
def run_eval_cycle(questions: list[dict], cycle_name: str,
                   max_cases: int | None = None) -> dict:
    """Run one evaluation cycle: agent predict + score."""
    data = questions[:max_cases] if max_cases else questions
    eval_records = []
    for q in data:
        gt = compute_sql_ground_truth(q)
        eval_records.append({
            "inputs": {"question": q["question"]},
            "expectations": {
                "expected_entities": q.get("expected_entities", []),
                "expected_tools": q.get("expected_tools", []),
                "graph_ground_truth": gt["graph_ground_truth"],
                "graph_relationships": q.get("graph_relationships", []),
                "anchor_body_text": q.get("anchor_body_text", ""),
                "anchor_subject": q.get("anchor_subject", ""),
                "primitive": q.get("primitive", ""),
                "difficulty": q.get("difficulty", "medium"),
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
    with mlflow.start_run(run_name=f"sql_first_{cycle_name}"):
        results = mlflow.genai.evaluate(
            data=eval_df, predict_fn=predict_fn, scorers=ALL_SCORERS,
        )
    elapsed = time.time() - t0
    results_df = results.tables["eval_results"]

    scorer_names = {s.__name__ if hasattr(s, "__name__") else getattr(s, "name", "?")
                    for s in ALL_SCORERS}
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
        score_cols = [c for c in results_df.columns if c.endswith("/value")]
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
        worst_qs.append({
            "question": str(q)[:120],
            "avg_score": round(float(row["avg_score"]), 3),
        })

    print(f"\n  Scores ({cycle_name}):")
    for name, sc in sorted(scorer_scores.items()):
        bar_len = int(sc * 20) if not pd.isna(sc) else 0
        print(f"    {name:30s}: {sc:.3f} {'█' * bar_len}")
    print(f"    {'OVERALL':30s}: {overall:.3f}")
    print(f"    Time: {elapsed:.0f}s ({elapsed / max(len(data), 1):.1f}s/q)")
    if worst_qs:
        print(f"\n  Worst:")
        for wq in worst_qs[:3]:
            print(f"    [{wq['avg_score']:.2f}] {wq['question'][:80]}")

    return {
        "cycle": cycle_name, "overall": overall, "scorer_scores": scorer_scores,
        "worst_questions": worst_qs, "num_questions": len(data),
        "elapsed_s": round(elapsed, 1), "results_df": results_df,
    }


# =========================================================================
# STEP 8 — Adaptive Escalation
# =========================================================================
ESCALATION_PROMPT = """You are an adversarial evaluator for a GraphRAG system about Enron emails.

The agent FAILED on these questions:
{failure_summary}

Failure analysis:
- Questions where entity_recall was low: the agent couldn't find entities in the graph
- Questions where tool_plan_recall was low: the agent used wrong tools
- Questions where synthesis_quality was low: the answer was poorly grounded

Cycle: {cycle}, Strategy: {strategy}

Using the ACTUAL graph data below, generate {n_questions} NEW harder questions
that target the same weaknesses. Each question MUST name SPECIFIC Enron people
or entities and map to exactly ONE computational primitive.

AVAILABLE GRAPH DATA (use only these entity names):
{entity_pool}

TARGET PRIMITIVES:
{primitives_desc}

Return ONLY a JSON array:
[{{"question": "...", "primitive": "entity_structure|entity_explore|entity_pair|timeline|keyword_search|genie_analytics", "primary_entity": "exact name", "secondary_entity": "exact name or empty", "expected_answer_summary": "...", "difficulty": "hard", "keywords": ""}}]"""


def escalate_questions(
    failures: list[dict],
    all_questions: list[dict],
    cycle: int,
    entity_pool: list[str],
) -> list[dict]:
    """Generate harder questions targeting identified weaknesses."""
    if not failures:
        return []

    failure_summary = "\n".join(
        f"  - Q: {f['question'][:80]}  Score: {f['avg_score']}"
        for f in failures[:5]
    )

    strategy = (
        "probe entity resolution gaps — ask about entities with multiple name forms"
        if cycle <= 2
        else "demand specific email evidence — require dates, subjects, sender/recipient details"
        if cycle <= 4
        else "adversarial cross-referencing — combine multiple primitives, ask for evidence chains"
    )

    pool_str = ", ".join(entity_pool[:30])
    primitives_desc = "\n".join(
        f"  - {PRIMITIVES_DESCRIPTION[p]}" for p in PRIMITIVES if p in PRIMITIVES_DESCRIPTION
    )

    prompt = ESCALATION_PROMPT.format(
        failure_summary=failure_summary,
        cycle=cycle,
        strategy=strategy,
        n_questions=5,
        entity_pool=pool_str,
        primitives_desc=primitives_desc,
    )

    try:
        new_qs = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=2048)
        if not isinstance(new_qs, list):
            return []
    except Exception as e:
        print(f"  Escalator failed: {e}")
        return []

    enriched = []
    for q in new_qs[:5]:
        primitive = q.get("primitive", "general")
        if primitive not in PRIMITIVE_TOOL_MAP:
            primitive = "general"
        candidate = {
            "question": q.get("question", ""),
            "primitive": primitive,
            "expected_tools": PRIMITIVE_TOOL_MAP[primitive],
            "primary_entity": q.get("primary_entity", ""),
            "secondary_entity": q.get("secondary_entity", ""),
            "expected_answer_summary": q.get("expected_answer_summary", ""),
            "expected_entities": sorted(set(filter(None, [
                q.get("primary_entity", ""),
                q.get("secondary_entity", ""),
            ]))),
            "difficulty": "hard",
            "keywords": q.get("keywords", ""),
            "anchor_thread_id": "",
            "anchor_subject": "",
            "anchor_body_text": "",
            "anchor_date": "",
            "anchor_sender": "",
            "graph_entities": entity_pool[:20],
            "graph_relationships": [],
        }
        enriched.append(
            canonicalize_generated_question(
                candidate,
                corpus="enron",
                source_type="sql_first_generated",
                suite_tag="sql_first_candidate",
            )
        )
    print(f"  Escalator: {len(enriched)} harder questions (cycle {cycle})")
    return enriched


# =========================================================================
# STEP 9 — Cache
# =========================================================================
CACHE_DIR = Path("data/eval_cache")


def _cache_key(n_anchors: int, seed: int) -> str:
    return f"sqlfirst_seed{seed}_n{n_anchors}"


def _save_cache(key: str, anchors, graph_facts_dicts, all_questions):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    payload = {
        "anchors": anchors,
        "graph_facts": graph_facts_dicts,
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


# =========================================================================
# Main Pipeline
# =========================================================================
def run_pipeline(
    n_anchors: int = 15,
    max_cycles: int = 4,
    max_cases: int | None = None,
    baseline_only: bool = False,
    seed: int = 42,
    force_regen: bool = False,
) -> dict:
    """Full SQL-first evaluation pipeline."""
    print("=" * 60)
    print("SQL-FIRST EVALUATION PIPELINE")
    print(f"  Backend: {_EVAL_BACKEND} | Seed: {seed} | Anchors: {n_anchors}")
    print("=" * 60)

    cache_key = _cache_key(n_anchors, seed)
    cached = None if force_regen else _load_cache(cache_key)

    if cached:
        anchors = cached["anchors"]
        all_questions = cached["all_questions"]
        entity_pool = list({e for q in all_questions for e in q.get("graph_entities", [])})
        print(f"\n  Loaded from cache: {len(anchors)} anchors, {len(all_questions)} questions")
    else:
        # Step 1: Sample
        print("\n--- STEP 1: Sample Anchor Emails ---")
        t_step = time.time()
        anchors = sample_anchor_emails(n=n_anchors, seed=seed)
        print(f"  ({time.time() - t_step:.1f}s)")

        # Step 2: Graph-fact extraction (SQL)
        print("\n--- STEP 2: Graph-Fact Extraction (SQL) ---")
        t_step = time.time()
        graph_facts_list: list[GraphFacts] = []
        graph_facts_dicts: list[dict] = []
        for anchor in anchors:
            gf = extract_graph_facts(anchor)
            graph_facts_list.append(gf)
            graph_facts_dicts.append({
                "thread_id": gf.thread_id,
                "n_entities": len(gf.entities),
                "n_relationships": len(gf.relationships),
                "n_org_hits": len(gf.org_hierarchy_hits),
                "person_names": gf.person_names,
                "testable_primitives": gf.testable_primitives,
            })

        total_entities = sum(len(gf.entities) for gf in graph_facts_list)
        total_rels = sum(len(gf.relationships) for gf in graph_facts_list)
        print(f"  Graph facts: {total_entities} entities, {total_rels} relationships ({time.time() - t_step:.1f}s)")

        # Step 3: Question generation (LLM proposer, architecture-aware)
        print("\n--- STEP 3: Primitive-Aware Question Generation ---")
        t_step = time.time()
        all_questions: list[dict] = []
        for i, (gf, anchor) in enumerate(zip(graph_facts_list, anchors)):
            if not gf.entities:
                continue
            n_qs = min(3, max(1, len(gf.testable_primitives)))
            qs = generate_questions(gf, anchor, n_questions=n_qs)
            all_questions.extend(qs)
            if (i + 1) % 5 == 0:
                print(f"  [{i+1}/{len(anchors)}] {len(all_questions)} questions generated")

        print(f"\n  Generated {len(all_questions)} questions ({time.time() - t_step:.1f}s)")

        primitive_dist = pd.Series([q["primitive"] for q in all_questions]).value_counts()
        print(f"  Primitives: {dict(primitive_dist)}")

        if not all_questions:
            print("ERROR: No questions generated. Check LLM endpoints.")
            return {"error": "no questions generated"}

        entity_pool = list({e for q in all_questions for e in q.get("graph_entities", [])})

        _save_cache(cache_key, anchors, graph_facts_dicts, all_questions)

    # Step 6: Evaluation loop
    print("\n--- STEP 6: Evaluation Loop ---")
    history = []
    train = [q for q in all_questions if q.get("eval_split") == "train"]
    test = [q for q in all_questions if q.get("eval_split") == "test"]
    holdout = [q for q in all_questions if q.get("eval_split") == "holdout"] or test[-2:]
    print(f"  Splits: train={len(train)}, test={len(test)}, holdout={len(holdout)}")

    baseline = run_eval_cycle(train, "baseline", max_cases=max_cases)
    history.append(baseline)

    if baseline_only:
        return {"history": history, "questions": len(all_questions)}

    entity_pool_list = entity_pool if entity_pool else []

    for cycle in range(1, max_cycles + 1):
        new_qs = escalate_questions(
            history[-1]["worst_questions"], train, cycle, entity_pool_list,
        )
        for eq in new_qs:
            eq["expected_tools"] = PRIMITIVE_TOOL_MAP.get(eq["primitive"], [])
        train = new_qs + train

        result = run_eval_cycle(train, f"cycle_{cycle}", max_cases=max_cases)
        history.append(result)

        if len(history) >= PLATEAU_WINDOW + 1:
            gains = [
                (history[i]["overall"] - history[i - 1]["overall"]) * 100
                for i in range(-PLATEAU_WINDOW, 0)
            ]
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
    print(f"\nAgent Performance:")
    prev = None
    for h in history:
        g = f"  ({(h['overall'] - prev)*100:+.1f}pp)" if prev is not None else ""
        print(f"  {h['cycle']:20s}: {h['overall']:.3f} ({h['num_questions']}q){g}")
        prev = h["overall"]

    return {
        "history": history,
        "questions": len(all_questions),
        "final_score": history[-1]["overall"],
    }


# =========================================================================
# CLI Entry Point
# =========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQL-first evaluation pipeline")
    parser.add_argument("--anchors", type=int, default=15,
                        help="Number of anchor emails to sample")
    parser.add_argument("--max-cycles", type=int, default=4,
                        help="Max adversarial escalation cycles")
    parser.add_argument("--cases", type=int, default=None,
                        help="Limit questions per eval cycle")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Only run baseline, no escalation loop")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for sampling")
    parser.add_argument("--force-regen", action="store_true",
                        help="Force regeneration (ignore cache)")
    parser.add_argument("--local", action="store_true",
                        help="Use DuckDB local backend for SQL")
    args = parser.parse_args()

    if args.local:
        os.environ["GRAPHRAG_EVAL_BACKEND"] = "local"
        os.environ["GRAPHRAG_BACKEND"] = "local"
        os.environ.setdefault("GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb")
        globals()["_EVAL_BACKEND"] = "local"

    run_pipeline(
        n_anchors=args.anchors,
        max_cycles=args.max_cycles,
        max_cases=args.cases,
        baseline_only=args.baseline_only,
        seed=args.seed,
        force_regen=args.force_regen,
    )
