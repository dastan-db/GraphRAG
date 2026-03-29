# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Evaluation
# MAGIC
# MAGIC Evaluate the Enron GraphRAG agent using MLflow GenAI evaluation with
# MAGIC LLM-as-judge scorers for evidence quality, organizational accuracy,
# MAGIC grounding integrity, factual accuracy, and governance dimensions
# MAGIC (tool usage correctness, hallucination detection, answer completeness).

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
# MAGIC ## Step 1: Evaluation Dataset (Dual Ground Truth)
# MAGIC
# MAGIC Each question has both `graph_ground_truth` (what the knowledge graph
# MAGIC actually contains) and `historical_ground_truth` (real-world facts from
# MAGIC public record). The judge uses `graph_ground_truth` for scoring but
# MAGIC references `historical_ground_truth` to detect hallucination.

# COMMAND ----------

# DBTITLE 1,Enron Evaluation Dataset
EVAL_DATA = [
    # --- org_hierarchy (7 questions) ---
    {
        "question": "Who reported to Jeff Skilling?",
        "expected_entities": ["Andrew Fastow", "Cliff Baxter", "Greg Whalley",
                              "David Delainey", "Lou Pai", "Kenneth Rice"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "The graph shows REPORTS_TO edges from multiple people to Jeff Skilling: "
            "David Delainey, Kenneth Rice, Cliff Baxter, Rick Buy, and others. "
            "Skilling also MANAGES entities like Enron Energy Trading, Enron Broadband "
            "Services, and Project Raptor. The org_hierarchy table lists Fastow, "
            "Delainey, Rice, Belden, Buy, Lou Pai, Cliff Baxter, Mark Frevert as "
            "reporting to Skilling."
        ),
        "historical_ground_truth": (
            "Key direct reports included Andrew Fastow (CFO), Cliff Baxter (Vice "
            "Chairman), Lou Pai (CEO EES), David Delainey (CEO EES after Pai), "
            "Kenneth Rice (CEO Broadband), and Rick Buy (CRO). After Skilling "
            "became CEO in Feb 2001, Greg Whalley and Mark Frevert reported to him."
        ),
        "evidence_required": True,
    },
    {
        "question": "What was the organizational structure around Enron Energy Trading?",
        "expected_entities": ["David Delainey", "John Lavorato", "Jeffrey Skilling"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "The graph shows David Delainey MANAGES many entities and REPORTS_TO "
            "Skilling. John Lavorato is connected to Delainey. Enron Energy Trading "
            "appears as a Division entity managed by Skilling."
        ),
        "historical_ground_truth": (
            "David Delainey was CEO of Enron Energy Services, with John Lavorato as "
            "COO reporting to Delainey. Both ultimately reported to Jeff Skilling."
        ),
        "evidence_required": True,
    },
    {
        "question": "Who was Andrew Fastow's boss?",
        "expected_entities": ["Andrew Fastow", "Jeff Skilling"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "The graph shows Andrew Fastow REPORTS_TO Jeff Skilling. The org_hierarchy "
            "table confirms Fastow as CFO reporting to Skilling from 1998 to Oct 2001."
        ),
        "historical_ground_truth": (
            "Andrew Fastow, as CFO, reported to Jeff Skilling (COO, then CEO). "
            "After Skilling resigned Aug 2001, Fastow reported to Kenneth Lay."
        ),
        "evidence_required": True,
    },
    {
        "question": "What projects did Jeff Skilling manage?",
        "expected_entities": ["Jeffrey Skilling", "Enron Broadband Services"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "The graph shows Skilling MANAGES: Enron Corporation, Enron Energy "
            "Trading, Enron Broadband Services, Project Raptor, and other entities."
        ),
        "historical_ground_truth": (
            "Skilling oversaw Enron's trading operations, Enron Broadband Services, "
            "Enron Energy Services, and was responsible for the asset-light strategy."
        ),
        "evidence_required": False,
    },
    {
        "question": "Who was involved in the California energy trading decisions?",
        "expected_entities": ["Tim Belden", "David Delainey", "Jeffrey Skilling"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "Tim Belden is present in the graph connected to energy trading entities. "
            "David Delainey and Skilling are connected via MANAGES relationships."
        ),
        "historical_ground_truth": (
            "Tim Belden led the West Power Trading desk. David Delainey and John "
            "Lavorato oversaw EES operations. Skilling was the executive sponsor."
        ),
        "evidence_required": True,
    },
    {
        "question": "Who did Kenneth Lay report to?",
        "expected_entities": ["Kenneth Lay"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "The graph shows no REPORTS_TO edges FROM Lay. He MANAGES many people. "
            "The org_hierarchy table shows Lay as Chairman & CEO with no reports_to_id."
        ),
        "historical_ground_truth": (
            "Kenneth Lay, as Chairman & CEO, reported to the Enron Board of "
            "Directors. He was the most senior executive."
        ),
        "evidence_required": False,
    },
    {
        "question": "What was Sherron Watkins' role and who did she report to?",
        "expected_entities": ["Sherron Watkins", "Andrew Fastow"],
        "category": "org_hierarchy",
        "graph_ground_truth": (
            "Sherron Watkins appears in the graph as a Person. The org_hierarchy "
            "table shows her as VP Corporate Development reporting to Andrew Fastow."
        ),
        "historical_ground_truth": (
            "Sherron Watkins was VP of Corporate Development, reporting to Andrew "
            "Fastow. She became the famous whistleblower in August 2001."
        ),
        "evidence_required": True,
    },

    # --- communication (5 questions) ---
    {
        "question": "Who communicated most frequently with Kenneth Lay?",
        "expected_entities": ["Kenneth Lay"],
        "category": "communication",
        "graph_ground_truth": (
            "The communication_dyads table and find_top_contacts tool should return "
            "Lay's highest-volume correspondents from email headers. These likely "
            "include Rosalee Fleming (assistant) and senior executives."
        ),
        "historical_ground_truth": (
            "Lay's most frequent correspondents included his executive assistant "
            "Rosalee Fleming, Jeff Skilling, and various board members."
        ),
        "evidence_required": True,
    },
    {
        "question": "How did information flow about the Broadband division?",
        "expected_entities": ["Kenneth Rice", "Jeffrey Skilling", "Enron Broadband Services"],
        "category": "communication",
        "graph_ground_truth": (
            "Kenneth Rice MANAGES Enron Communications and is connected to Broadband "
            "entities. Multiple SENT_TO and DISCUSSES edges connect people to EBS."
        ),
        "historical_ground_truth": (
            "Kenneth Rice (CEO Broadband) reported to Skilling and communicated "
            "extensively about EBS projects, fiber assets, and content deals."
        ),
        "evidence_required": True,
    },
    {
        "question": "Which executives discussed Fastow's partnerships?",
        "expected_entities": ["Andrew Fastow", "Jeffrey Skilling", "Kenneth Lay"],
        "category": "communication",
        "graph_ground_truth": (
            "Fastow's DISCUSSES relationships and partnership entities (LJM, Raptors) "
            "should appear in the graph. Email evidence mentioning Fastow and "
            "these entities should connect to Lay and Skilling."
        ),
        "historical_ground_truth": (
            "Fastow's LJM partnerships were discussed by Lay, Skilling, Rick Buy "
            "(CRO), Rick Causey (CAO), and board members."
        ),
        "evidence_required": True,
    },
    {
        "question": "Who were Vince Kaminski's top email contacts?",
        "expected_entities": ["Vince Kaminski"],
        "category": "communication",
        "graph_ground_truth": (
            "find_top_contacts for Kaminski should return his most frequent email "
            "correspondents from the communication_dyads table."
        ),
        "historical_ground_truth": (
            "Kaminski, head of Research, communicated most with his research team "
            "members, Rick Buy (CRO), and various traders."
        ),
        "evidence_required": True,
    },
    {
        "question": "Did Jeff Skilling and Sherron Watkins communicate directly?",
        "expected_entities": ["Jeff Skilling", "Sherron Watkins"],
        "category": "communication",
        "graph_ground_truth": (
            "get_emails_between should reveal whether direct emails exist between "
            "Skilling and Watkins. The graph may show indirect connections through Fastow."
        ),
        "historical_ground_truth": (
            "Their direct email communication was minimal; Watkins primarily "
            "communicated with Fastow and later wrote to Lay."
        ),
        "evidence_required": True,
    },

    # --- temporal (5 questions) ---
    {
        "question": "What happened at Enron in August 2001?",
        "expected_entities": ["Jeff Skilling", "Sherron Watkins", "Kenneth Lay"],
        "category": "temporal",
        "graph_ground_truth": (
            "The investigation_timeline table shows: Skilling resigned Aug 14, "
            "Watkins sent warning letter Aug 15, Watkins met Lay Aug 22. "
            "query_timeline should surface these events."
        ),
        "historical_ground_truth": (
            "Key August 2001 events: Skilling resigned as CEO mid-August. "
            "Watkins sent a warning letter to Lay about accounting concerns. "
            "Lay was reinstated as CEO."
        ),
        "evidence_required": True,
    },
    {
        "question": "When did Enron's problems become public?",
        "expected_entities": ["Kenneth Lay"],
        "category": "temporal",
        "graph_ground_truth": (
            "The investigation_timeline shows: Q3 loss reported Oct 16, SEC inquiry "
            "Oct 22, formal investigation Oct 31. Email patterns should show "
            "increasing crisis communication in late 2001."
        ),
        "historical_ground_truth": (
            "Enron's problems became public in late 2001: a large quarterly loss "
            "was reported, the SEC opened an inquiry, and a formal investigation followed."
        ),
        "evidence_required": True,
    },
    {
        "question": "How did communication patterns change after Skilling resigned?",
        "expected_entities": ["Jeff Skilling", "Kenneth Lay", "Greg Whalley"],
        "category": "temporal",
        "graph_ground_truth": (
            "Email volume changes for Lay and Whalley should be visible via "
            "find_top_contacts with date filtering. The org_hierarchy shows Whalley "
            "became President & COO after Skilling's departure."
        ),
        "historical_ground_truth": (
            "After Skilling resigned mid-August 2001, Lay reassumed the CEO role. "
            "Communication patterns shifted with increased outward-facing emails."
        ),
        "evidence_required": True,
    },
    {
        "question": "What was the sequence of executive departures from Enron?",
        "expected_entities": ["Cliff Baxter", "Lou Pai", "Jeff Skilling",
                              "Andrew Fastow", "Kenneth Lay"],
        "category": "temporal",
        "graph_ground_truth": (
            "The investigation_timeline shows: Baxter resigned May 2001, Pai left "
            "June 2001, Skilling resigned Aug 2001, Fastow removed Oct 2001, "
            "Lay resigned Jan 2002."
        ),
        "historical_ground_truth": (
            "Baxter and Pai left mid-2001, Skilling resigned August 2001, "
            "Fastow was removed late October 2001, Lay resigned January 2002."
        ),
        "evidence_required": True,
    },
    {
        "question": "What happened between the SEC inquiry and bankruptcy filing?",
        "expected_entities": [],
        "category": "temporal",
        "graph_ground_truth": (
            "The investigation_timeline covers: SEC formal investigation Oct 31, "
            "earnings restatement Nov 8, Dynegy merger announced Nov 9, Dynegy "
            "pulls out Nov 28, bankruptcy Dec 2."
        ),
        "historical_ground_truth": (
            "Between late October and early December 2001: SEC investigation "
            "escalated, earnings were restated, a merger with Dynegy was announced "
            "then collapsed, and Enron filed for bankruptcy."
        ),
        "evidence_required": True,
    },

    # --- topic (5 questions) ---
    {
        "question": "What financial events were discussed in executive emails?",
        "expected_entities": [],
        "category": "topic",
        "graph_ground_truth": (
            "Financial_Event entities in the graph include earnings calls, stock "
            "events, and SEC filings. DISCUSSES relationships connect people to these."
        ),
        "historical_ground_truth": (
            "Executive emails discussed quarterly earnings, stock price, trading "
            "revenues, California energy crisis, SPEs, and mark-to-market accounting."
        ),
        "evidence_required": False,
    },
    {
        "question": "What topics did Kenneth Lay discuss in his emails?",
        "expected_entities": ["Kenneth Lay"],
        "category": "topic",
        "graph_ground_truth": (
            "get_entity_summary and find_connections for Lay should reveal his "
            "DISCUSSES relationships. get_emails_between with key contacts should "
            "show topics in email subjects and bodies."
        ),
        "historical_ground_truth": (
            "Lay's emails covered company strategy, employee morale, stock price, "
            "board communications, regulatory matters, and public relations."
        ),
        "evidence_required": True,
    },
    {
        "question": "What was discussed about special purpose entities?",
        "expected_entities": ["Andrew Fastow"],
        "category": "topic",
        "graph_ground_truth": (
            "The graph may contain entities for LJM, Raptors, or other SPE names. "
            "Fastow should be connected via DISCUSSES or PARTICIPATES_IN. "
            "get_context_verses('LJM') can find relevant emails."
        ),
        "historical_ground_truth": (
            "SPE discussions centered on LJM, Raptors, Chewco, and JEDI "
            "partnerships, their capitalization structures, and conflicts of interest."
        ),
        "evidence_required": True,
    },
    {
        "question": "What were the main subjects of emails mentioning Arthur Andersen?",
        "expected_entities": ["Arthur Andersen"],
        "category": "topic",
        "graph_ground_truth": (
            "Arthur Andersen LLP exists in the graph as an Organization. "
            "find_entity and get_context_verses should surface audit-related emails."
        ),
        "historical_ground_truth": (
            "Emails about Andersen covered audit reviews, accounting treatment "
            "guidance, document retention, and eventually document destruction."
        ),
        "evidence_required": True,
    },
    {
        "question": "What internal projects or initiatives were discussed by executives?",
        "expected_entities": [],
        "category": "topic",
        "graph_ground_truth": (
            "The graph contains Project-type entities (Project Raptor, Dabhol Power) "
            "and Division entities (Enron Broadband Services, Enron Online). "
            "These are connected via MANAGES and DISCUSSES edges."
        ),
        "historical_ground_truth": (
            "Key projects included Enron Broadband Services, Enron Online (trading "
            "platform), Project Braveheart (Blockbuster deal), and various "
            "international assets."
        ),
        "evidence_required": False,
    },

    # --- path (4 questions) ---
    {
        "question": "How are Kenneth Lay and Tim Belden connected?",
        "expected_entities": ["Kenneth Lay", "Tim Belden", "Jeffrey Skilling"],
        "category": "path",
        "graph_ground_truth": (
            "trace_path should show a multi-hop path: Lay → Skilling → Belden "
            "through MANAGES/REPORTS_TO edges."
        ),
        "historical_ground_truth": (
            "Lay → Skilling → Belden: Lay oversaw Skilling as CEO, who oversaw "
            "Belden's West Power Trading desk."
        ),
        "evidence_required": True,
    },
    {
        "question": "What's the connection between Sherron Watkins and Jeff Skilling?",
        "expected_entities": ["Sherron Watkins", "Jeff Skilling", "Andrew Fastow"],
        "category": "path",
        "graph_ground_truth": (
            "trace_path should find: Watkins → Fastow → Skilling through "
            "REPORTS_TO edges."
        ),
        "historical_ground_truth": (
            "Watkins reported to Fastow (CFO), who reported to Skilling (CEO/COO)."
        ),
        "evidence_required": True,
    },
    {
        "question": "How is Vince Kaminski connected to Kenneth Lay?",
        "expected_entities": ["Vince Kaminski", "Kenneth Lay", "Rick Buy"],
        "category": "path",
        "graph_ground_truth": (
            "trace_path should show: Kaminski → Buy → Skilling → Lay through "
            "REPORTS_TO edges."
        ),
        "historical_ground_truth": (
            "Kaminski reported to Rick Buy (CRO), who reported to Skilling, "
            "who reported to Lay."
        ),
        "evidence_required": True,
    },
    {
        "question": "What is the relationship between Michael Kopper and Kenneth Lay?",
        "expected_entities": ["Michael Kopper", "Andrew Fastow", "Kenneth Lay"],
        "category": "path",
        "graph_ground_truth": (
            "trace_path should show: Kopper → Fastow → Skilling → Lay. "
            "Kopper connected to Fastow through Global Finance / partnership entities."
        ),
        "historical_ground_truth": (
            "Kopper was under Fastow in Global Finance. Fastow reported to "
            "Skilling, who reported to Lay."
        ),
        "evidence_required": True,
    },

    # --- general (4 questions) ---
    {
        "question": "What can you tell me about Enron?",
        "expected_entities": ["Enron"],
        "category": "general",
        "graph_ground_truth": (
            "The knowledge graph is built from ~20,000 Enron emails (2000-2002). "
            "Enron Corp is the highest-PageRank entity. The graph contains entities "
            "of type Person, Organization, Division, Project, etc."
        ),
        "historical_ground_truth": (
            "Enron was a Houston-based energy company that became one of the "
            "largest corporate fraud cases in US history, filing bankruptcy Dec 2001."
        ),
        "evidence_required": False,
    },
    {
        "question": "Who were the key whistleblowers in the Enron scandal?",
        "expected_entities": ["Sherron Watkins"],
        "category": "general",
        "graph_ground_truth": (
            "Sherron Watkins appears in the graph. The investigation_timeline shows "
            "her warning letter (Aug 15 2001) and congressional testimony (Feb 7 2002)."
        ),
        "historical_ground_truth": (
            "Sherron Watkins (VP Corporate Development) was the most prominent "
            "internal whistleblower, sending a warning letter to Lay in Aug 2001."
        ),
        "evidence_required": True,
    },
    {
        "question": "What role did the board of directors play?",
        "expected_entities": [],
        "category": "general",
        "graph_ground_truth": (
            "The graph may contain board-related entities. find_entity('board') "
            "or get_context_verses('board of directors') should surface relevant evidence."
        ),
        "historical_ground_truth": (
            "The board approved key financial structures including Fastow's "
            "partnerships and waived conflict-of-interest rules."
        ),
        "evidence_required": False,
    },
    {
        "question": "Why did Enron fail?",
        "expected_entities": [],
        "category": "general",
        "graph_ground_truth": (
            "The graph captures communication patterns, relationships, and discussed "
            "topics. Some failure indicators are visible: partnership entities, "
            "executive departures on the timeline, crisis-period email patterns."
        ),
        "historical_ground_truth": (
            "Enron failed due to accounting fraud (mark-to-market abuse, SPE "
            "manipulation), executive conflicts of interest, inadequate board "
            "oversight, and auditor complicity."
        ),
        "evidence_required": False,
    },
]

eval_records = []
for row in EVAL_DATA:
    eval_records.append({
        "inputs": {"question": row["question"]},
        "expectations": {
            "expected_entities": row["expected_entities"],
            "graph_ground_truth": row["graph_ground_truth"],
            "historical_ground_truth": row["historical_ground_truth"],
            "evidence_required": row["evidence_required"],
            "category": row["category"],
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

# DBTITLE 1,Execute Evaluation
with mlflow.start_run(run_name="enron_graphrag_eval_v2"):
    results = mlflow.genai.evaluate(
        data=eval_df,
        predict_fn=predict_enron_agent,
        scorers=[
            evidence_quality,
            participant_verification,
            organizational_accuracy,
            grounding_integrity,
            factual_accuracy,
            hallucination_detection,
            answer_completeness,
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
    results_df["avg_score"] = results_df[score_cols].mean(axis=1)
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
# MAGIC Evaluation complete. Review the results in MLflow to identify areas for improvement.
# MAGIC
# MAGIC **Scorers used:**
# MAGIC - `evidence_quality` — LLM judge: are claims backed by dates, emails, tool results?
# MAGIC - `participant_verification` — string match: are expected entities mentioned?
# MAGIC - `organizational_accuracy` — LLM judge: is the hierarchy correct?
# MAGIC - `grounding_integrity` — LLM judge: does agent distinguish graph vs external knowledge?
# MAGIC - `factual_accuracy` — LLM judge: does the response match what the graph contains?
# MAGIC - `hallucination_detection` — LLM judge: does the agent fabricate evidence?
# MAGIC - `answer_completeness` — LLM judge: does the response address the full question?
