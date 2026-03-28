# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Evaluation
# MAGIC
# MAGIC Evaluate the Enron GraphRAG agent using MLflow GenAI evaluation with
# MAGIC corporate governance-focused scorers: email citation completeness,
# MAGIC timeline accuracy, participant verification, and organizational consistency.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import json
import mlflow
import pandas as pd
from mlflow.entities import Feedback

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Evaluation Dataset
# MAGIC
# MAGIC Corporate investigation questions with expected answers covering
# MAGIC organizational hierarchy, communication patterns, and temporal queries.

# COMMAND ----------

# DBTITLE 1,Enron Evaluation Dataset
EVAL_DATA = [
    # --- org_hierarchy (7 questions) ---
    {
        "question": "Who reported to Jeff Skilling?",
        "expected_entities": ["David Delainey", "Rob Milnthorp",
                              "Kayla Crenshaw", "Sanjay Bhatnagar"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "The graph contains REPORTS_TO edges from several people to Jeff Skilling: "
            "Delainey, Rob Milnthorp, Keohane, Kayla Crenshaw, Sanjay Bhatnagar, and "
            "Maureen McVicker. Skilling also MANAGES entities like Enron Energy Trading, "
            "Project Raptor, and Enron Broadband Services. The agent should mention both "
            "REPORTS_TO and MANAGES directions for completeness."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What was the organizational structure around Enron Energy Trading?",
        "expected_entities": ["David Delainey", "John Lavorato"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "David Delainey and John Lavorato were key figures in energy trading. "
            "The graph shows Skilling MANAGES Enron Energy Trading. Delainey REPORTS_TO "
            "Skilling and MANAGES many people and divisions. The agent should show "
            "REPORTS_TO and MANAGES relationships connecting these entities."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "Who was Andrew Fastow's boss?",
        "expected_entities": ["Andrew Fastow", "Kenneth Lay"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "The graph shows Andrew Fastow REPORTS_TO Kenneth Lay. Kenneth Lay MANAGES "
            "Fastow. The agent should surface these relationships and note that Fastow "
            "was CFO. The graph does not contain a Fastow→Skilling REPORTS_TO edge; "
            "the direct reporting relationship is to Lay."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What projects did Jeff Skilling manage?",
        "expected_entities": ["Skilling", "Enron Broadband Services"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "The graph shows Skilling MANAGES: Enron Corporation, Enron Energy Trading, "
            "Enron Broadband Services, Project Raptor, Enron Executive Committee Retreat, "
            "and Enron Corporate Policy Committee. The agent should list these MANAGES "
            "relationships from the graph."
        ),
        "evidence_required": False,
        "graph_derivable": True,
    },
    {
        "question": "Who was involved in the California energy trading decisions?",
        "expected_entities": ["Tim Belden"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "Tim Belden is present in the graph and connected to energy trading. "
            "The agent should find Tim Belden's entity and relationships, and look "
            "for connections to California-related entities. David Delainey may appear "
            "through energy trading management links."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "Who did Kenneth Lay report to?",
        "expected_entities": ["Kenneth Lay"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "Kenneth Lay was the Chairman and CEO — the most senior executive. "
            "The graph should show no REPORTS_TO edges FROM Lay to anyone above him. "
            "Lay MANAGES many people (Skilling, Whalley, Fastow, etc.). A good answer "
            "notes that Lay was at the top of the hierarchy based on graph evidence."
        ),
        "evidence_required": False,
        "graph_derivable": True,
    },
    {
        "question": "Who did Greg Whalley manage at Enron?",
        "expected_entities": ["Greg Whalley", "Kenneth Lay"],
        "category": "org_hierarchy",
        "ground_truth_answer": (
            "The graph shows Greg Whalley MANAGES Enron Corporation, Enron Wholesale "
            "Services, and Enron Net Works. Kenneth Lay MANAGES Whalley. Liz Taylor "
            "REPORTS_TO Whalley. The agent should describe Whalley's management scope "
            "and his position under Lay."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },

    # --- communication (5 questions) ---
    {
        "question": "Who communicated most frequently with Kenneth Lay?",
        "expected_entities": ["Kenneth Lay"],
        "category": "communication",
        "ground_truth_answer": (
            "The find_top_contacts tool should return Lay's highest-volume correspondents "
            "from the email headers. These likely include Rosalee Fleming (assistant), "
            "and senior executives. The answer should include ranked names and email counts."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "How did information flow about the Broadband division?",
        "expected_entities": ["Ken Rice", "Enron Broadband Services"],
        "category": "communication",
        "ground_truth_answer": (
            "Ken Rice MANAGES Enron Communications and is connected to Broadband. "
            "The graph shows multiple people managing or referencing Enron Broadband "
            "Services. The agent should surface these connections and any relevant emails."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "Which executives discussed Fastow's partnerships?",
        "expected_entities": ["Andrew Fastow", "Kenneth Lay"],
        "category": "communication",
        "ground_truth_answer": (
            "The agent should search for Fastow's DISCUSSES relationships and look "
            "for partnership entities (LJM, Raptors, SPEs). Kenneth Lay and other "
            "senior executives may appear in email evidence mentioning Fastow and "
            "these partnership entities."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "Who were Vince Kaminski's top email contacts?",
        "expected_entities": ["Vince Kaminski"],
        "category": "communication",
        "ground_truth_answer": (
            "find_top_contacts for Kaminski should return his most frequent email "
            "correspondents — likely his research team and trading colleagues. "
            "The answer should present a ranked list with sent/received counts."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "Did David Delainey and Jeff Skilling communicate directly?",
        "expected_entities": ["David Delainey", "Jeff Skilling"],
        "category": "communication",
        "ground_truth_answer": (
            "get_emails_between should reveal direct emails between Delainey and "
            "Skilling. The graph also shows Delainey REPORTS_TO Skilling, confirming "
            "a working relationship. The answer should cite specific email dates and subjects."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },

    # --- temporal (5 questions) ---
    {
        "question": "What happened at Enron in August 2001?",
        "expected_entities": ["Jeff Skilling", "Kenneth Lay"],
        "category": "temporal",
        "ground_truth_answer": (
            "The timeline and email evidence should show Skilling's resignation "
            "in mid-August 2001. query_timeline and get_context_verses should surface "
            "relevant events. Lay returned as CEO. Email volume changes may be visible."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "When did Enron's problems become public?",
        "expected_entities": ["Kenneth Lay"],
        "category": "temporal",
        "ground_truth_answer": (
            "Email patterns in late 2001 should show increasing crisis communication. "
            "The timeline tool should surface key events. The agent may note that "
            "external events (SEC inquiry, bankruptcy) are outside the email graph "
            "but provide what the emails and timeline reveal."
        ),
        "evidence_required": True,
        "graph_derivable": "partial",
    },
    {
        "question": "How did communication patterns change after Skilling resigned?",
        "expected_entities": ["Jeff Skilling", "Kenneth Lay"],
        "category": "temporal",
        "ground_truth_answer": (
            "The graph should show changes in Lay's communication patterns after "
            "mid-August 2001 — potentially more emails, new contacts. Greg Whalley "
            "took on expanded responsibilities. get_emails_between and find_top_contacts "
            "can surface these patterns."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What was the sequence of executive departures from Enron?",
        "expected_entities": ["Jeff Skilling", "Andrew Fastow", "Kenneth Lay"],
        "category": "temporal",
        "ground_truth_answer": (
            "The timeline tool should show executive departures — Skilling resigned "
            "Aug 2001, Fastow was placed on leave Oct 2001. Email volume changes for "
            "individuals can indicate when they left. Not all departures may be captured."
        ),
        "evidence_required": True,
        "graph_derivable": "partial",
    },
    {
        "question": "What happened between the SEC inquiry and bankruptcy filing?",
        "expected_entities": [],
        "category": "temporal",
        "ground_truth_answer": (
            "The timeline tool may capture some of these events. "
            "Email patterns in Oct-Dec 2001 should show crisis communication. "
            "Many details (SEC actions, Dynegy merger) are external to the email corpus. "
            "A good answer presents what the graph contains and notes its limitations."
        ),
        "evidence_required": True,
        "graph_derivable": "partial",
    },

    # --- topic (5 questions) ---
    {
        "question": "What financial events were discussed in executive emails?",
        "expected_entities": [],
        "category": "topic",
        "ground_truth_answer": (
            "get_context_verses with financial terms should surface emails about "
            "earnings, trading, stock price, and energy markets. "
            "The graph entities of type Financial_Event should appear."
        ),
        "evidence_required": False,
        "graph_derivable": True,
    },
    {
        "question": "What topics did Kenneth Lay discuss in his emails?",
        "expected_entities": ["Kenneth Lay"],
        "category": "topic",
        "ground_truth_answer": (
            "get_entity_summary and find_connections for Lay should reveal his "
            "DISCUSSES relationships. get_emails_between with key contacts should "
            "show topics like strategy, employee communications, and regulatory matters."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What was discussed about special purpose entities?",
        "expected_entities": ["Andrew Fastow"],
        "category": "topic",
        "ground_truth_answer": (
            "The graph may contain entities for LJM, Raptors, or other SPE names "
            "if they appear in emails. Fastow should be connected via DISCUSSES or "
            "PARTICIPATES_IN relationships. get_context_verses('LJM') can find relevant "
            "emails. The agent should report whatever it finds and note gaps."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What were the main subjects of emails mentioning Arthur Andersen?",
        "expected_entities": ["Arthur Andersen"],
        "category": "topic",
        "ground_truth_answer": (
            "find_entity and get_context_verses for Arthur Andersen should surface "
            "emails about audit, accounting, and document-related topics. "
            "Arthur Andersen LLP exists in the graph as an Organization entity."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What internal projects or initiatives were discussed by executives?",
        "expected_entities": [],
        "category": "topic",
        "ground_truth_answer": (
            "The graph contains many Project-type entities extracted from emails: "
            "Project Raptor, Dabhol Power, and others. Enron Broadband Services, "
            "Enron Online, and other initiatives should appear as Division-type entities."
        ),
        "evidence_required": False,
        "graph_derivable": True,
    },

    # --- path (4 questions) ---
    {
        "question": "How are Kenneth Lay and Tim Belden connected?",
        "expected_entities": ["Kenneth Lay", "Tim Belden"],
        "category": "path",
        "ground_truth_answer": (
            "trace_path should show a multi-hop path between Lay and Belden through "
            "the organizational hierarchy. The path may go through Skilling or other "
            "executives. The answer should describe each hop and relationship type."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What's the connection between Andrew Fastow and Jeff Skilling?",
        "expected_entities": ["Andrew Fastow", "Jeff Skilling"],
        "category": "path",
        "ground_truth_answer": (
            "trace_path should find a path between Fastow and Skilling. They may be "
            "connected through Kenneth Lay (Fastow REPORTS_TO Lay, Lay MANAGES Skilling) "
            "or through other shared organizational entities. The answer should walk "
            "through the path hops."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "How is Vince Kaminski connected to Kenneth Lay?",
        "expected_entities": ["Vince Kaminski", "Kenneth Lay"],
        "category": "path",
        "ground_truth_answer": (
            "trace_path should show a multi-hop path through the organizational "
            "or communication graph. The path may involve shared entities like "
            "Enron Corp or intermediate people in the reporting chain."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },
    {
        "question": "What is the relationship between Michael Kopper and Kenneth Lay?",
        "expected_entities": ["Michael Kopper", "Andrew Fastow", "Kenneth Lay"],
        "category": "path",
        "ground_truth_answer": (
            "trace_path should show a multi-hop path. Kopper is connected to Fastow "
            "(partnership entities), and Fastow REPORTS_TO Lay. The answer should "
            "describe the organizational chain and any shared entities."
        ),
        "evidence_required": True,
        "graph_derivable": True,
    },

    # --- general (4 questions) ---
    {
        "question": "What can you tell me about Enron?",
        "expected_entities": ["Enron"],
        "category": "general",
        "ground_truth_answer": (
            "The knowledge graph is built from ~20,000 Enron emails (2000-2002). "
            "The agent should describe the corpus scope, key entities found, "
            "and types of relationships extracted. General historical facts are acceptable "
            "if clearly labeled as external knowledge."
        ),
        "evidence_required": False,
        "graph_derivable": "partial",
    },
    {
        "question": "What do the emails reveal about the Enron scandal?",
        "expected_entities": [],
        "category": "general",
        "ground_truth_answer": (
            "The agent should use the knowledge graph to surface entities and "
            "relationships relevant to the scandal: Fastow's partnerships, executive "
            "departures, Arthur Andersen connections, and communication patterns. "
            "The agent may supplement with labeled external knowledge."
        ),
        "evidence_required": False,
        "graph_derivable": "partial",
    },
    {
        "question": "What role did the board of directors play?",
        "expected_entities": [],
        "category": "general",
        "ground_truth_answer": (
            "The graph may contain board-related entities and communications. "
            "find_entity('board') or get_context_verses('board of directors') "
            "should surface relevant email evidence."
        ),
        "evidence_required": False,
        "graph_derivable": True,
    },
    {
        "question": "Why did Enron fail?",
        "expected_entities": [],
        "category": "general",
        "ground_truth_answer": (
            "This question primarily requires external knowledge. The agent should "
            "share what the email graph reveals (communication patterns, relationships, "
            "discussed topics) and may supplement with clearly-labeled general knowledge."
        ),
        "evidence_required": False,
        "graph_derivable": "partial",
    },
]

# Build MLflow 3 compliant evaluation DataFrame
eval_records = []
for row in EVAL_DATA:
    eval_records.append({
        "inputs": {"question": row["question"]},
        "expectations": {
            "expected_entities": row["expected_entities"],
            "ground_truth_answer": row["ground_truth_answer"],
            "evidence_required": row["evidence_required"],
            "category": row["category"],
            "graph_derivable": row.get("graph_derivable", True),
        },
    })

eval_df = pd.DataFrame(eval_records)
print(f"Evaluation dataset: {len(eval_df)} questions")
display(eval_df)

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
# MAGIC ## Step 3: Corporate Governance Scorers

# COMMAND ----------

# DBTITLE 1,Email Citation Completeness Scorer
from mlflow.genai.scorers import scorer

@scorer
def email_citation_completeness(inputs, outputs, expectations=None):
    """Check if the agent's response cites specific evidence.

    Looks for date patterns, email references, tool result citations,
    relationship type mentions, and provenance sections.
    """
    import re

    text = outputs if isinstance(outputs, str) else str(outputs)

    date_refs = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    subject_refs = re.findall(r"Subject:\s*\S+", text, re.IGNORECASE)
    email_patterns = re.findall(r"\[?\d{4}-\d{2}-\d{2},?\s*From:.*?\]", text, re.IGNORECASE)
    sender_refs = re.findall(r"(?:From|Sender|sent by):\s*\S+", text, re.IGNORECASE)
    tool_citations = re.findall(
        r"(?:find_connections|find_top_contacts|get_emails_between|trace_path|"
        r"get_entity_summary|get_context_verses|query_timeline)\s*\(",
        text,
    )
    rel_type_refs = re.findall(
        r"REPORTS_TO|MANAGES|SENT_TO|COLLABORATES_WITH|DISCUSSES|EMPLOYED_BY",
        text,
    )

    citation_count = (
        len(date_refs) + len(subject_refs) + len(email_patterns)
        + len(sender_refs) + len(tool_citations) + len(rel_type_refs)
    )
    has_provenance = bool(re.search(
        r"###?\s*[Pp]rovenance|^\*\*Sources\*\*|^\*\*Provenance\*\*",
        text, re.MULTILINE,
    ))

    if citation_count >= 5 and has_provenance:
        score = 1.0
    elif citation_count >= 3 and has_provenance:
        score = 0.8
    elif citation_count >= 1 and has_provenance:
        score = 0.6
    elif has_provenance:
        score = 0.4
    elif citation_count >= 3:
        score = 0.3
    elif citation_count >= 1:
        score = 0.2
    else:
        score = 0.0

    return Feedback(
        value=score,
        rationale=(
            f"Found {citation_count} citations (dates={len(date_refs)}, "
            f"subjects={len(subject_refs)}, tool_refs={len(tool_citations)}, "
            f"rel_types={len(rel_type_refs)}), provenance={'yes' if has_provenance else 'no'}"
        ),
    )

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

# DBTITLE 1,Organizational Consistency Scorer
@scorer
def organizational_consistency(inputs, outputs, expectations=None):
    """Check if the response uses structured organizational information.

    Looks for relationship types, hierarchy patterns, role mentions,
    path notation, and provenance sections.
    """
    import re

    text = outputs if isinstance(outputs, str) else str(outputs)

    hierarchy_patterns = re.findall(
        r"(\w[\w\s]+?)\s*(?:reported to|reports to|managed by|overseen by|directed by|"
        r"supervised by|headed by|led by|worked for|was under|oversaw|subordinate to|"
        r"worked under|served under|manages|managed)\s*(\w[\w\s]+?)(?:\.|,|\n)",
        text,
        re.IGNORECASE,
    )

    path_patterns = re.findall(
        r"REPORTS_TO|MANAGES|COLLABORATES_WITH|SENT_TO|DISCUSSES|EMPLOYED_BY|"
        r"PARTICIPATES_IN|REFERENCES|ADVISES",
        text,
    )

    role_patterns = re.findall(
        r"\b(?:CEO|CFO|COO|CRO|CAO|VP|President|Chairman|Director|Manager|"
        r"Managing Director|Vice President|Chief|executive|assistant)\b",
        text,
        re.IGNORECASE,
    )

    has_structure = len(hierarchy_patterns) > 0 or len(path_patterns) > 0 or len(role_patterns) >= 2
    has_provenance = bool(re.search(r"###?\s*[Pp]rovenance|^\*\*Provenance\*\*", text, re.MULTILINE))
    has_path = "→" in text or "->" in text or "--[" in text or "path" in text.lower()

    signals = sum([has_structure, has_provenance, has_path])

    if signals == 3:
        score = 1.0
    elif signals == 2:
        score = 0.7
    elif signals == 1:
        score = 0.4
    else:
        score = 0.1

    return Feedback(
        value=score,
        rationale=(
            f"Hierarchy refs: {len(hierarchy_patterns)}, "
            f"relationship types: {len(path_patterns)}, "
            f"role mentions: {len(role_patterns)}, "
            f"path_notation={'yes' if has_path else 'no'}, "
            f"provenance={'yes' if has_provenance else 'no'}"
        ),
    )

# COMMAND ----------

# DBTITLE 1,Grounding Quality Scorer
@scorer
def grounding_quality(inputs, outputs, expectations=None):
    """Check the self-reported grounding quality of the response."""
    import re

    text = outputs if isinstance(outputs, str) else str(outputs)
    text_lower = text.lower()

    has_provenance_section = bool(re.search(r"###?\s*provenance", text_lower))
    has_grounding_field = bool(re.search(r"\*\*grounding\*\*", text_lower))

    full_grounding_phrases = [
        "all claims grounded", "all findings grounded", "all claims are grounded",
        "grounded in graph data", "based on graph data", "supported by graph",
        "all evidence from graph", "from the knowledge graph",
        "based entirely on", "fully grounded",
    ]
    partial_phrases = [
        "partially grounded", "some claims from", "partially supported",
        "mix of graph", "supplemented with",
    ]
    not_found_phrases = [
        "not found in graph", "not in the knowledge graph", "no data found",
        "not available in the graph",
    ]

    if any(p in text_lower for p in full_grounding_phrases):
        return Feedback(value=1.0, rationale="Agent reports full grounding in graph data")
    elif has_provenance_section and has_grounding_field:
        return Feedback(value=0.8, rationale="Has provenance section with grounding field")
    elif any(p in text_lower for p in partial_phrases):
        return Feedback(value=0.5, rationale="Agent reports partial grounding")
    elif has_provenance_section:
        return Feedback(value=0.5, rationale="Has provenance section (no explicit grounding level)")
    elif any(p in text_lower for p in not_found_phrases):
        return Feedback(value=0.3, rationale="Agent reports information not in graph")
    else:
        return Feedback(value=0.0, rationale="No grounding statement found")

# COMMAND ----------

# DBTITLE 1,Evidence Sufficiency Scorer
@scorer
def evidence_sufficiency(inputs, outputs, expectations=None):
    """Check whether the response provides supporting evidence for its claims.

    For legal audit, every factual claim should cite a source: an email,
    a date, a relationship from the graph, or at least a confidence qualifier.
    """
    import re

    evidence_required = (expectations or {}).get("evidence_required", True)
    if evidence_required is False:
        return Feedback(value=1.0, rationale="Evidence not required for this question")

    text = outputs if isinstance(outputs, str) else str(outputs)

    evidence_signals = []
    evidence_signals.extend(re.findall(r"\d{4}-\d{2}-\d{2}", text))
    evidence_signals.extend(re.findall(r"(?:Subject|From|To|Email|Thread):", text, re.IGNORECASE))
    evidence_signals.extend(re.findall(
        r"REPORTS_TO|MANAGES|SENT_TO|COLLABORATES_WITH|DISCUSSES|"
        r"EMPLOYED_BY|PARTICIPATES_IN|REFERENCES|ADVISES",
        text,
    ))
    evidence_signals.extend(re.findall(
        r"\b(?:according to|based on|evidence|source|cited?|graph data|"
        r"knowledge graph|email corpus|emails? found|relationships? found)\b",
        text, re.IGNORECASE,
    ))
    evidence_signals.extend(re.findall(
        r"(?:find_connections|find_top_contacts|get_emails_between|"
        r"trace_path|get_entity_summary|query_timeline)\b",
        text,
    ))

    confidence_signals = re.findall(
        r"\b(?:confidence|uncertain|approximate|limited data|no data|"
        r"caveat|not found|not available|not in the graph|coverage)\b",
        text, re.IGNORECASE,
    )

    total_signals = len(evidence_signals) + len(confidence_signals)

    if total_signals >= 5:
        score = 1.0
    elif total_signals >= 3:
        score = 0.7
    elif total_signals >= 1:
        score = 0.4
    else:
        score = 0.0

    return Feedback(
        value=score,
        rationale=(
            f"Evidence signals: {len(evidence_signals)}, "
            f"confidence signals: {len(confidence_signals)}, "
            f"total: {total_signals}"
        ),
    )

# COMMAND ----------

# DBTITLE 1,Factual Accuracy Scorer (LLM-as-Judge)
JUDGE_ENDPOINT = config.get("judge_endpoint", "databricks-claude-sonnet-4-6")

@scorer
def factual_accuracy(inputs, outputs, expectations=None):
    """LLM-as-judge scorer comparing agent response against ground truth.

    Uses a strong external LLM to assess factual overlap and correctness.
    """
    ground_truth_answer = (expectations or {}).get("ground_truth_answer")
    if not ground_truth_answer:
        return Feedback(value=1.0, rationale="No ground truth provided")

    text = outputs if isinstance(outputs, str) else str(outputs)

    if text.startswith("ERROR:") or len(text.strip()) < 20:
        return Feedback(value=0.0, rationale=f"Agent returned error or empty response: {text[:100]}")

    judge_prompt = f"""You are evaluating a question-answering system built on a knowledge graph derived from ~20,000 Enron emails (2000-2002). The agent can ONLY access information present in the email corpus and the knowledge graph extracted from it. It does NOT have access to external sources, news articles, court documents, or post-hoc historical analysis.

IMPORTANT CONTEXT: The ground truth answer may contain facts from external sources that the agent cannot access (e.g., SEC filings, court records, post-collapse analysis). You MUST account for this data limitation when scoring.

Scoring rubric (0.0 to 1.0):
- 1.0: Agent's claims are factually correct and well-supported. Covers the ground truth facts that ARE derivable from email evidence. Correctly acknowledges when information is outside its data.
- 0.7: Most claims correct. Minor omissions of email-derivable facts, but no contradictions. May miss some context that requires external knowledge.
- 0.5: Core answer direction is correct. Some email-derivable facts present, some missing. No fabrications.
- 0.3: Partially relevant but thin. Few supporting details from the graph. Or answer is vague.
- 0.0: Contradicts known facts, fabricates information not in the graph, or completely fails to answer.

CRITICAL: Do NOT penalize the agent for:
- Missing facts that require external knowledge (SEC filings, court records, news)
- Saying "not found in graph" when the information genuinely isn't in the email data
- Providing fewer details than the ground truth when the ground truth uses external sources
DO penalize the agent for:
- Contradicting facts that ARE in the email corpus
- Fabricating relationships or events not supported by email evidence
- Failing to find information that IS in the knowledge graph

Ground Truth (may include external facts): {ground_truth_answer}

Agent Response (based on email-derived knowledge graph): {text[:3000]}

Return ONLY a JSON object with keys "score" (float) and "justification" (string). No other text."""

    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{JUDGE_ENDPOINT}/invocations",
            body={
                "messages": [{"role": "user", "content": judge_prompt}],
                "temperature": 0.0,
                "max_tokens": 256,
            },
        )
        result_text = resp["choices"][0]["message"]["content"].strip()

        if result_text.startswith("```"):
            import re
            result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
            result_text = re.sub(r"\s*```$", "", result_text)

        parsed = json.loads(result_text)
        return Feedback(
            value=float(parsed.get("score", 0.0)),
            rationale=parsed.get("justification", "Judge returned no justification"),
        )
    except Exception as e:
        return Feedback(value=0.0, rationale=f"Judge failed: {type(e).__name__}: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Run Evaluation
# MAGIC
# MAGIC Evaluate the Enron agent with all corporate governance scorers.

# COMMAND ----------

# DBTITLE 1,Execute Evaluation
with mlflow.start_run(run_name="enron_graphrag_eval"):
    results = mlflow.genai.evaluate(
        data=eval_df,
        predict_fn=predict_enron_agent,
        scorers=[
            email_citation_completeness,
            participant_verification,
            organizational_consistency,
            grounding_quality,
            evidence_sufficiency,
            factual_accuracy,
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

# Extract category from expectations in the eval DataFrame
categories = eval_df["expectations"].apply(lambda x: x.get("category", "unknown"))
results_df = results_df.copy()
results_df["category"] = categories.values

score_cols = [c for c in results_df.columns if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])]
if score_cols:
    score_agg = {col: "mean" for col in score_cols}
    summary = results_df.groupby("category").agg(score_agg).round(2)
    # Clean up column names for readability
    summary.columns = [c.replace("/value", "") for c in summary.columns]
    display(summary)
else:
    print("No score columns found in results.")

# COMMAND ----------

# DBTITLE 1,Overall Governance Score
score_cols = [c for c in results_df.columns if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])]
if score_cols:
    overall = results_df[score_cols].mean()
    print("=== Enron GraphRAG Governance Scores ===")
    for col in score_cols:
        name = col.replace("/value", "")
        print(f"  {name:35s}: {overall[col]:.2f}")
    print(f"  {'OVERALL':35s}: {overall.mean():.2f}")
else:
    print("No score columns found in results.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Evaluation complete. Review the results in MLflow to identify areas for improvement.