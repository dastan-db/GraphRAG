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
# Eval dataset — identical to notebook 08
# ---------------------------------------------------------------------------
EVAL_DATA = [
    {"question": "Who reported to Jeff Skilling?", "expected_entities": ["Andrew Fastow", "Cliff Baxter", "Greg Whalley", "David Delainey", "Lou Pai", "Kenneth Rice"], "category": "org_hierarchy", "graph_ground_truth": "The graph shows REPORTS_TO edges from multiple people to Jeff Skilling: David Delainey, Kenneth Rice, Cliff Baxter, Rick Buy, and others. Skilling also MANAGES entities like Enron Energy Trading, Enron Broadband Services, and Project Raptor. The org_hierarchy table lists Fastow, Delainey, Rice, Belden, Buy, Lou Pai, Cliff Baxter, Mark Frevert as reporting to Skilling.", "historical_ground_truth": "Key direct reports included Andrew Fastow (CFO), Cliff Baxter (Vice Chairman), Lou Pai (CEO EES), David Delainey (CEO EES after Pai), Kenneth Rice (CEO Broadband), and Rick Buy (CRO). After Skilling became CEO in Feb 2001, Greg Whalley and Mark Frevert reported to him.", "evidence_required": True},
    {"question": "What was the organizational structure around Enron Energy Trading?", "expected_entities": ["David Delainey", "John Lavorato", "Jeffrey Skilling"], "category": "org_hierarchy", "graph_ground_truth": "The graph shows David Delainey MANAGES many entities and REPORTS_TO Skilling. John Lavorato is connected to Delainey. Enron Energy Trading appears as a Division entity managed by Skilling.", "historical_ground_truth": "David Delainey was CEO of Enron Energy Services, with John Lavorato as COO reporting to Delainey. Both ultimately reported to Jeff Skilling.", "evidence_required": True},
    {"question": "Who was Andrew Fastow's boss?", "expected_entities": ["Andrew Fastow", "Jeff Skilling"], "category": "org_hierarchy", "graph_ground_truth": "The graph shows Andrew Fastow REPORTS_TO Jeff Skilling. The org_hierarchy table confirms Fastow as CFO reporting to Skilling from 1998 to Oct 2001.", "historical_ground_truth": "Andrew Fastow, as CFO, reported to Jeff Skilling (COO, then CEO). After Skilling resigned Aug 2001, Fastow reported to Kenneth Lay.", "evidence_required": True},
    {"question": "What projects did Jeff Skilling manage?", "expected_entities": ["Jeffrey Skilling", "Enron Broadband Services"], "category": "org_hierarchy", "graph_ground_truth": "The graph shows Skilling MANAGES: Enron Corporation, Enron Energy Trading, Enron Broadband Services, Project Raptor, and other entities.", "historical_ground_truth": "Skilling oversaw Enron's trading operations, Enron Broadband Services, Enron Energy Services, and was responsible for the asset-light strategy.", "evidence_required": False},
    {"question": "Who was involved in the California energy trading decisions?", "expected_entities": ["Tim Belden", "David Delainey", "Jeffrey Skilling"], "category": "org_hierarchy", "graph_ground_truth": "Tim Belden is present in the graph connected to energy trading entities. David Delainey and Skilling are connected via MANAGES relationships.", "historical_ground_truth": "Tim Belden led the West Power Trading desk. David Delainey and John Lavorato oversaw EES operations. Skilling was the executive sponsor.", "evidence_required": True},
    {"question": "Who did Kenneth Lay report to?", "expected_entities": ["Kenneth Lay"], "category": "org_hierarchy", "graph_ground_truth": "The graph shows no REPORTS_TO edges FROM Lay. He MANAGES many people. The org_hierarchy table shows Lay as Chairman & CEO with no reports_to_id.", "historical_ground_truth": "Kenneth Lay, as Chairman & CEO, reported to the Enron Board of Directors. He was the most senior executive.", "evidence_required": False},
    {"question": "What was Sherron Watkins' role and who did she report to?", "expected_entities": ["Sherron Watkins", "Andrew Fastow"], "category": "org_hierarchy", "graph_ground_truth": "Sherron Watkins appears in the graph as a Person. The org_hierarchy table shows her as VP Corporate Development reporting to Andrew Fastow.", "historical_ground_truth": "Sherron Watkins was VP of Corporate Development, reporting to Andrew Fastow. She became the famous whistleblower in August 2001.", "evidence_required": True},
    {"question": "Who communicated most frequently with Kenneth Lay?", "expected_entities": ["Kenneth Lay"], "category": "communication", "graph_ground_truth": "The communication_dyads table and find_top_contacts tool should return Lay's highest-volume correspondents from email headers. These likely include Rosalee Fleming (assistant) and senior executives.", "historical_ground_truth": "Lay's most frequent correspondents included his executive assistant Rosalee Fleming, Jeff Skilling, and various board members.", "evidence_required": True},
    {"question": "How did information flow about the Broadband division?", "expected_entities": ["Kenneth Rice", "Jeffrey Skilling", "Enron Broadband Services"], "category": "communication", "graph_ground_truth": "Kenneth Rice MANAGES Enron Communications and is connected to Broadband entities. Multiple SENT_TO and DISCUSSES edges connect people to EBS.", "historical_ground_truth": "Kenneth Rice (CEO Broadband) reported to Skilling and communicated extensively about EBS projects, fiber assets, and content deals.", "evidence_required": True},
    {"question": "Which executives discussed Fastow's partnerships?", "expected_entities": ["Andrew Fastow", "Jeffrey Skilling", "Kenneth Lay"], "category": "communication", "graph_ground_truth": "Fastow's DISCUSSES relationships and partnership entities (LJM, Raptors) should appear in the graph. Email evidence mentioning Fastow and these entities should connect to Lay and Skilling.", "historical_ground_truth": "Fastow's LJM partnerships were discussed by Lay, Skilling, Rick Buy (CRO), Rick Causey (CAO), and board members.", "evidence_required": True},
    {"question": "Who were Vince Kaminski's top email contacts?", "expected_entities": ["Vince Kaminski"], "category": "communication", "graph_ground_truth": "find_top_contacts for Kaminski should return his most frequent email correspondents from the communication_dyads table.", "historical_ground_truth": "Kaminski, head of Research, communicated most with his research team members, Rick Buy (CRO), and various traders.", "evidence_required": True},
    {"question": "Did Jeff Skilling and Sherron Watkins communicate directly?", "expected_entities": ["Jeff Skilling", "Sherron Watkins"], "category": "communication", "graph_ground_truth": "get_emails_between should reveal whether direct emails exist between Skilling and Watkins. The graph may show indirect connections through Fastow.", "historical_ground_truth": "Their direct email communication was minimal; Watkins primarily communicated with Fastow and later wrote to Lay.", "evidence_required": True},
    {"question": "What happened at Enron in August 2001?", "expected_entities": ["Jeff Skilling", "Sherron Watkins", "Kenneth Lay"], "category": "temporal", "graph_ground_truth": "The investigation_timeline table shows: Skilling resigned Aug 14, Watkins sent warning letter Aug 15, Watkins met Lay Aug 22. query_timeline should surface these events.", "historical_ground_truth": "Key August 2001 events: Skilling resigned as CEO mid-August. Watkins sent a warning letter to Lay about accounting concerns. Lay was reinstated as CEO.", "evidence_required": True},
    {"question": "When did Enron's problems become public?", "expected_entities": ["Kenneth Lay"], "category": "temporal", "graph_ground_truth": "The investigation_timeline shows: Q3 loss reported Oct 16, SEC inquiry Oct 22, formal investigation Oct 31. Email patterns should show increasing crisis communication in late 2001.", "historical_ground_truth": "Enron's problems became public in late 2001: a large quarterly loss was reported, the SEC opened an inquiry, and a formal investigation followed.", "evidence_required": True},
    {"question": "How did communication patterns change after Skilling resigned?", "expected_entities": ["Jeff Skilling", "Kenneth Lay", "Greg Whalley"], "category": "temporal", "graph_ground_truth": "Email volume changes for Lay and Whalley should be visible via find_top_contacts with date filtering. The org_hierarchy shows Whalley became President & COO after Skilling's departure.", "historical_ground_truth": "After Skilling resigned mid-August 2001, Lay reassumed the CEO role. Communication patterns shifted with increased outward-facing emails.", "evidence_required": True},
    {"question": "What was the sequence of executive departures from Enron?", "expected_entities": ["Cliff Baxter", "Lou Pai", "Jeff Skilling", "Andrew Fastow", "Kenneth Lay"], "category": "temporal", "graph_ground_truth": "The investigation_timeline shows: Baxter resigned May 2001, Pai left June 2001, Skilling resigned Aug 2001, Fastow removed Oct 2001, Lay resigned Jan 2002.", "historical_ground_truth": "Baxter and Pai left mid-2001, Skilling resigned August 2001, Fastow was removed late October 2001, Lay resigned January 2002.", "evidence_required": True},
    {"question": "What happened between the SEC inquiry and bankruptcy filing?", "expected_entities": [], "category": "temporal", "graph_ground_truth": "The investigation_timeline covers: SEC formal investigation Oct 31, earnings restatement Nov 8, Dynegy merger announced Nov 9, Dynegy pulls out Nov 28, bankruptcy Dec 2.", "historical_ground_truth": "Between late October and early December 2001: SEC investigation escalated, earnings were restated, a merger with Dynegy was announced then collapsed, and Enron filed for bankruptcy.", "evidence_required": True},
    {"question": "What financial events were discussed in executive emails?", "expected_entities": [], "category": "topic", "graph_ground_truth": "Financial_Event entities in the graph include earnings calls, stock events, and SEC filings. DISCUSSES relationships connect people to these.", "historical_ground_truth": "Executive emails discussed quarterly earnings, stock price, trading revenues, California energy crisis, SPEs, and mark-to-market accounting.", "evidence_required": False},
    {"question": "What topics did Kenneth Lay discuss in his emails?", "expected_entities": ["Kenneth Lay"], "category": "topic", "graph_ground_truth": "get_entity_summary and find_connections for Lay should reveal his DISCUSSES relationships. get_emails_between with key contacts should show topics in email subjects and bodies.", "historical_ground_truth": "Lay's emails covered company strategy, employee morale, stock price, board communications, regulatory matters, and public relations.", "evidence_required": True},
    {"question": "What was discussed about special purpose entities?", "expected_entities": ["Andrew Fastow"], "category": "topic", "graph_ground_truth": "The graph may contain entities for LJM, Raptors, or other SPE names. Fastow should be connected via DISCUSSES or PARTICIPATES_IN. get_source_evidence('LJM') can find relevant emails.", "historical_ground_truth": "SPE discussions centered on LJM, Raptors, Chewco, and JEDI partnerships, their capitalization structures, and conflicts of interest.", "evidence_required": True},
    {"question": "What were the main subjects of emails mentioning Arthur Andersen?", "expected_entities": ["Arthur Andersen"], "category": "topic", "graph_ground_truth": "Arthur Andersen LLP exists in the graph as an Organization. find_entity and get_source_evidence should surface audit-related emails.", "historical_ground_truth": "Emails about Andersen covered audit reviews, accounting treatment guidance, document retention, and eventually document destruction.", "evidence_required": True},
    {"question": "What internal projects or initiatives were discussed by executives?", "expected_entities": [], "category": "topic", "graph_ground_truth": "The graph contains Project-type entities (Project Raptor, Dabhol Power) and Division entities (Enron Broadband Services, Enron Online). These are connected via MANAGES and DISCUSSES edges.", "historical_ground_truth": "Key projects included Enron Broadband Services, Enron Online (trading platform), Project Braveheart (Blockbuster deal), and various international assets.", "evidence_required": False},
    {"question": "How are Kenneth Lay and Tim Belden connected?", "expected_entities": ["Kenneth Lay", "Tim Belden", "Jeffrey Skilling"], "category": "path", "graph_ground_truth": "trace_path should show a multi-hop path: Lay -> Skilling -> Belden through MANAGES/REPORTS_TO edges.", "historical_ground_truth": "Lay -> Skilling -> Belden: Lay oversaw Skilling as CEO, who oversaw Belden's West Power Trading desk.", "evidence_required": True},
    {"question": "What's the connection between Sherron Watkins and Jeff Skilling?", "expected_entities": ["Sherron Watkins", "Jeff Skilling", "Andrew Fastow"], "category": "path", "graph_ground_truth": "trace_path should find: Watkins -> Fastow -> Skilling through REPORTS_TO edges.", "historical_ground_truth": "Watkins reported to Fastow (CFO), who reported to Skilling (CEO/COO).", "evidence_required": True},
    {"question": "How is Vince Kaminski connected to Kenneth Lay?", "expected_entities": ["Vince Kaminski", "Kenneth Lay", "Rick Buy"], "category": "path", "graph_ground_truth": "trace_path should show: Kaminski -> Buy -> Skilling -> Lay through REPORTS_TO edges.", "historical_ground_truth": "Kaminski reported to Rick Buy (CRO), who reported to Skilling, who reported to Lay.", "evidence_required": True},
    {"question": "What is the relationship between Michael Kopper and Kenneth Lay?", "expected_entities": ["Michael Kopper", "Andrew Fastow", "Kenneth Lay"], "category": "path", "graph_ground_truth": "trace_path should show: Kopper -> Fastow -> Skilling -> Lay. Kopper connected to Fastow through Global Finance / partnership entities.", "historical_ground_truth": "Kopper was under Fastow in Global Finance. Fastow reported to Skilling, who reported to Lay.", "evidence_required": True},
    {"question": "What can you tell me about Enron?", "expected_entities": ["Enron"], "category": "general", "graph_ground_truth": "The knowledge graph is built from ~20,000 Enron emails (2000-2002). Enron Corp is the highest-PageRank entity. The graph contains entities of type Person, Organization, Division, Project, etc.", "historical_ground_truth": "Enron was a Houston-based energy company that became one of the largest corporate fraud cases in US history, filing bankruptcy Dec 2001.", "evidence_required": False},
    {"question": "Who were the key whistleblowers in the Enron scandal?", "expected_entities": ["Sherron Watkins"], "category": "general", "graph_ground_truth": "Sherron Watkins appears in the graph. The investigation_timeline shows her warning letter (Aug 15 2001) and congressional testimony (Feb 7 2002).", "historical_ground_truth": "Sherron Watkins (VP Corporate Development) was the most prominent internal whistleblower, sending a warning letter to Lay in Aug 2001.", "evidence_required": True},
    {"question": "What role did the board of directors play?", "expected_entities": [], "category": "general", "graph_ground_truth": "The graph may contain board-related entities. find_entity('board') or get_source_evidence('board of directors') should surface relevant evidence.", "historical_ground_truth": "The board approved key financial structures including Fastow's partnerships and waived conflict-of-interest rules.", "evidence_required": False},
    {"question": "Why did Enron fail?", "expected_entities": [], "category": "general", "graph_ground_truth": "The graph captures communication patterns, relationships, and discussed topics. Some failure indicators are visible: partnership entities, executive departures on the timeline, crisis-period email patterns.", "historical_ground_truth": "Enron failed due to accounting fraud (mark-to-market abuse, SPE manipulation), executive conflicts of interest, inadequate board oversight, and auditor complicity.", "evidence_required": False},
    {"question": "Who reported to Andrew Fastow? Show me the email evidence for each direct report.", "expected_entities": ["Andrew Fastow", "Michael Kopper", "Jeff McMahon", "Ben Glisan", "Richard Causey", "Sherron Watkins"], "category": "org_hierarchy_evidence", "graph_ground_truth": "org_hierarchy shows: Kopper, McMahon, Causey, Watkins, Glisan. get_hierarchy_evidence should return corroborating emails.", "historical_ground_truth": "Fastow's direct reports included Kopper, McMahon, Causey, Watkins, and Glisan.", "evidence_required": True},
    {"question": "What email evidence supports Michael Kopper reporting to Andrew Fastow?", "expected_entities": ["Michael Kopper", "Andrew Fastow"], "category": "org_hierarchy_evidence", "graph_ground_truth": "org_hierarchy has Kopper->Fastow. get_hierarchy_evidence should find direct emails between them.", "historical_ground_truth": "Kopper was Fastow's right-hand man in Global Finance.", "evidence_required": True},
    {"question": "Show me emails that prove Jeff Skilling managed David Delainey.", "expected_entities": ["Jeff Skilling", "David Delainey"], "category": "org_hierarchy_evidence", "graph_ground_truth": "org_hierarchy shows Delainey->Skilling. get_hierarchy_evidence and get_relationship_evidence should find corroborating emails.", "historical_ground_truth": "Delainey was CEO of EES reporting to Skilling.", "evidence_required": True},
    {"question": "What is the email evidence for Kenneth Lay's position at the top of Enron?", "expected_entities": ["Kenneth Lay"], "category": "org_hierarchy_evidence", "graph_ground_truth": "org_hierarchy shows Lay as Chairman & CEO. get_hierarchy_evidence should find emails demonstrating his authority.", "historical_ground_truth": "Lay was the founder and longest-serving executive.", "evidence_required": True},
    {"question": "Show me the reporting chain from Tim Belden to Kenneth Lay with email evidence.", "expected_entities": ["Tim Belden", "Jeff Skilling", "Kenneth Lay"], "category": "org_hierarchy_evidence", "graph_ground_truth": "org_hierarchy shows: Belden->Skilling->Lay. get_hierarchy_evidence for each pair should find supporting emails.", "historical_ground_truth": "Belden led West Power Trading, reported to Skilling, who reported to Lay.", "evidence_required": True},
    {"question": "What emails did Jeff Skilling and Andrew Fastow exchange?", "expected_entities": ["Jeff Skilling", "Andrew Fastow"], "category": "entity_pair_evidence", "graph_ground_truth": "get_emails_between should find direct emails. get_relationship_evidence should show REPORTS_TO edge.", "historical_ground_truth": "Fastow reported to Skilling; they communicated about financial structures.", "evidence_required": True},
    {"question": "Show me the evidence for communication between Vince Kaminski and Rick Buy.", "expected_entities": ["Vince Kaminski", "Rick Buy"], "category": "entity_pair_evidence", "graph_ground_truth": "org_hierarchy shows Kaminski->Buy. get_emails_between should find direct emails.", "historical_ground_truth": "Kaminski (MD Research) reported to Buy (CRO).", "evidence_required": True},
    {"question": "What emails show the relationship between Sherron Watkins and Kenneth Lay?", "expected_entities": ["Sherron Watkins", "Kenneth Lay"], "category": "entity_pair_evidence", "graph_ground_truth": "get_emails_between should find the warning letter and follow-up emails.", "historical_ground_truth": "Watkins sent Lay a warning letter Aug 15, 2001.", "evidence_required": True},
    {"question": "What is the source evidence for REPORTS_TO relationships involving Jeff Skilling?", "expected_entities": ["Jeff Skilling", "Kenneth Lay", "Andrew Fastow"], "category": "relationship_evidence", "graph_ground_truth": "find_connections with REPORTS_TO should show relationships. get_relationship_evidence should return source_threads.", "historical_ground_truth": "Skilling reported to Lay. Multiple executives reported to Skilling.", "evidence_required": True},
    {"question": "Can you trace the evidence for Enron's management hierarchy from emails?", "expected_entities": ["Kenneth Lay", "Jeff Skilling", "Andrew Fastow"], "category": "relationship_evidence", "graph_ground_truth": "org_hierarchy has the full chain. get_hierarchy_evidence should provide email evidence at each level.", "historical_ground_truth": "The core hierarchy: Lay->Skilling->Fastow.", "evidence_required": True},
    {"question": "What emails discuss the resignation of Jeff Skilling?", "expected_entities": ["Jeff Skilling", "Kenneth Lay"], "category": "keyword_evidence", "graph_ground_truth": "search_emails for 'resign' + 'Skilling' should find relevant emails.", "historical_ground_truth": "Skilling resigned Aug 14, 2001 citing personal reasons.", "evidence_required": True},
    {"question": "Find emails about the Arthur Andersen document destruction.", "expected_entities": ["Arthur Andersen"], "category": "keyword_evidence", "graph_ground_truth": "search_emails for 'shred', 'destroy', 'Andersen' should find relevant emails.", "historical_ground_truth": "Andersen began shredding Enron documents around Oct 12, 2001.", "evidence_required": True},
    {"question": "Show me emails from the period when Fastow was removed as CFO.", "expected_entities": ["Andrew Fastow", "Jeff McMahon"], "category": "keyword_evidence", "graph_ground_truth": "search_emails around Oct 24, 2001 mentioning Fastow/CFO should find relevant emails.", "historical_ground_truth": "Fastow was removed as CFO Oct 24, 2001. McMahon replaced him.", "evidence_required": True},
    # --- Corroboration test cases (cross-tool consistency / typo / null-evidence) ---
    {"question": "Show me all emails between Kenneth Lay and Jeff Dassovich.", "expected_entities": ["Kenneth Lay", "Jeff Dasovich"], "category": "corroboration", "graph_ground_truth": "Jeff Dasovich (one 's') exists in the graph. The typo 'Dassovich' should be fuzzy-corrected. get_emails_between should find emails matching the corrected name.", "historical_ground_truth": "Jeff Dasovich was a Government Affairs executive who communicated with senior leadership.", "evidence_required": True},
    {"question": "Who reported to Jeffery Skilling?", "expected_entities": ["Jeffrey Skilling", "Andrew Fastow", "David Delainey"], "category": "corroboration", "graph_ground_truth": "Jeffrey Skilling (correct spelling) exists in the graph. The typo 'Jeffery' should be fuzzy-corrected. find_connections with REPORTS_TO should return direct reports.", "historical_ground_truth": "Skilling's direct reports included Fastow, Delainey, Rice, Belden, Buy.", "evidence_required": True},
    {"question": "How many emails did Rosalee Fleming send to Kenneth Lay?", "expected_entities": ["Rosalee Fleming", "Kenneth Lay"], "category": "corroboration", "graph_ground_truth": "communication_dyads shows Fleming as one of Lay's top contacts. get_emails_between should return matching emails. Both tools must agree on a non-zero count.", "historical_ground_truth": "Fleming was Lay's executive assistant and communicated with him extensively.", "evidence_required": True},
    {"question": "Show me the emails between Kenneth Lay and Jeff Dasovich.", "expected_entities": ["Kenneth Lay", "Jeff Dasovich"], "category": "corroboration", "graph_ground_truth": "Both entities exist in the graph. find_connections may show SENT_TO edges. get_emails_between should find header-matched or body-mentioned emails. The counts should be consistent.", "historical_ground_truth": "Dasovich communicated with Lay primarily about California energy policy.", "evidence_required": True},
    {"question": "What did Vince Kaminski and vkaminski@aol.com discuss?", "expected_entities": ["Vince Kaminski"], "category": "corroboration", "graph_ground_truth": "vkaminski@aol.com is Kaminski's personal email. detect_self_emails should flag this pair. The agent should explain this is a self-email pattern, not a separate person.", "historical_ground_truth": "Kaminski used his AOL address as a personal backup.", "evidence_required": True},
    {"question": "Show me emails between David Delainey and someone who never emailed him directly.", "expected_entities": ["David Delainey"], "category": "corroboration", "graph_ground_truth": "The agent should identify contacts connected to Delainey via graph relationships but without direct email headers. It should honestly report 'no direct emails' without fabricating evidence.", "historical_ground_truth": "Not all graph relationships correspond to direct email exchanges.", "evidence_required": False},
    {"question": "Who communicated most with Jeff Skilling? Show me the actual emails with his top contact.", "expected_entities": ["Jeff Skilling"], "category": "corroboration", "graph_ground_truth": "find_top_contacts returns ranked contacts from communication_dyads. The follow-up 'show me actual emails' requires get_emails_between to find emails for the top contact. Both tools must resolve to the same person.", "historical_ground_truth": "Skilling's frequent correspondents included his assistant and senior executives.", "evidence_required": True},
    {"question": "What is the relationship between Andrew Fasttow and Kenneth Lay?", "expected_entities": ["Andrew Fastow", "Kenneth Lay"], "category": "corroboration", "graph_ground_truth": "Andrew Fastow (correct spelling, one 't') exists in the graph. The typo 'Fasttow' should be fuzzy-corrected. trace_path and find_connections should work with the corrected name.", "historical_ground_truth": "Fastow (CFO) reported to Skilling who reported to Lay.", "evidence_required": True},
    # --- Genie routing test cases (tabular questions that should route to genie_analytics) ---
    {"question": "Who communicated most frequently with Kenneth Lay?", "expected_entities": ["Kenneth Lay", "Rosalee Fleming"], "category": "genie_routing", "graph_ground_truth": "communication_dyads shows Rosalee Fleming as Lay's top contact by email volume. find_top_contacts returns a ranked list. A Genie SQL query on communication_dyads should produce a clean ranked table.", "historical_ground_truth": "Lay's most frequent correspondents included his executive assistant Rosalee Fleming and senior executives.", "evidence_required": False},
    {"question": "How many emails were exchanged between Kenneth Lay and Leonardo Pacheco?", "expected_entities": ["Kenneth Lay", "Leonardo Pacheco"], "category": "genie_routing", "graph_ground_truth": "communication_dyads shows ~28 emails from Pacheco to Lay. The direction is one-way: Pacheco -> Lay. The response must specify direction, not just say 'exchanged'.", "historical_ground_truth": "Leonardo Pacheco sent EnronOnline executive summaries and management reports to Kenneth Lay.", "evidence_required": False},
    {"question": "Show me all emails between Kenneth Lay and Leonardo Pacheco", "expected_entities": ["Kenneth Lay", "Leonardo Pacheco"], "category": "genie_routing", "graph_ground_truth": "There are ~28 emails from Pacheco to Lay. A 'show all' request should return the full result set, not truncated to 5 or 15. Subjects include EnronOnline Executive Summary and Management Report.", "historical_ground_truth": "Pacheco sent daily EnronOnline summaries to Lay and other executives.", "evidence_required": True},
    {"question": "What are the most common topics of email sent to Leonardo Pacheco?", "expected_entities": ["Leonardo Pacheco"], "category": "genie_routing", "graph_ground_truth": "The threads table with key_topics joined to entity_mentions should produce a ranked topic list. Topics likely include EnronOnline, transaction summaries, and management reports.", "historical_ground_truth": "Pacheco was involved in EnronOnline operations, receiving transaction and executive summaries.", "evidence_required": False},
    {"question": "What percentage of Kenneth Lay's emails were from his assistant?", "expected_entities": ["Kenneth Lay", "Rosalee Fleming"], "category": "genie_routing", "graph_ground_truth": "person_activity and communication_dyads tables can compute this. Fleming's email count divided by Lay's total_received gives the percentage. This is a pure SQL aggregation.", "historical_ground_truth": "Rosalee Fleming was Lay's executive assistant and handled significant correspondence volume.", "evidence_required": False},
    # --- Communication quality test cases (directional precision, honest gaps) ---
    {"question": "Who is Bob Shults?", "expected_entities": ["Bob Shults"], "category": "communication", "graph_ground_truth": "Bob Shults is a Person entity in the graph with email bob.shults@enron.com. get_entity_summary should return department, title if available, and graph centrality. If no title/department exists, say so explicitly.", "historical_ground_truth": "Bob Shults was an Enron employee involved in EnronOnline operations.", "evidence_required": False},
    {"question": "Who is Bob Shults to Leonardo Pacheco?", "expected_entities": ["Bob Shults", "Leonardo Pacheco"], "category": "communication", "graph_ground_truth": "get_emails_between shows ~39 emails primarily from Pacheco to Shults about EnronOnline summaries. The direction is predominantly Pacheco -> Shults. The response must characterize the direction correctly.", "historical_ground_truth": "Pacheco sent EnronOnline reports to Shults as part of the distribution list.", "evidence_required": True},
    {"question": "Who reported to Bob Shults?", "expected_entities": ["Bob Shults"], "category": "communication", "graph_ground_truth": "query_org_hierarchy and find_connections for Bob Shults may return no REPORTS_TO or MANAGES relationships. The agent must honestly state 'no reporting relationships found' without fabricating structure.", "historical_ground_truth": "Bob Shults' reporting relationships are not well documented in the available data.", "evidence_required": False},
]


# ---------------------------------------------------------------------------
# Scorers — identical to notebook 08
# ---------------------------------------------------------------------------
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
def main():
    parser = argparse.ArgumentParser(description="Local Enron GraphRAG evaluation")
    parser.add_argument("--cases", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--judge", type=str, default=None, help="Judge endpoint name")
    parser.add_argument("--run-name", type=str, default="local_eval", help="MLflow run name")
    args = parser.parse_args()

    if args.judge:
        global JUDGE_ENDPOINT
        JUDGE_ENDPOINT = args.judge

    data = EVAL_DATA
    if args.category:
        data = [d for d in data if d["category"] == args.category]
        if not data:
            print(f"No questions found for category '{args.category}'")
            print(f"Available: {sorted(set(d['category'] for d in EVAL_DATA))}")
            return
    if args.cases:
        data = data[: args.cases]

    eval_records = []
    for row in data:
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
    print(f"Evaluation: {len(eval_df)} questions | judge={JUDGE_ENDPOINT}")
    print(f"Backend: {os.environ.get('GRAPHRAG_BACKEND', 'databricks')}")
    print(f"Corpus: {os.environ.get('GRAPHRAG_CORPUS', 'enron')}")
    print()

    t0 = time.time()
    with mlflow.start_run(run_name=args.run_name):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=predict_fn,
            scorers=ALL_SCORERS,
        )

    elapsed = time.time() - t0
    results_df = results.tables["eval_results"]

    categories = eval_df["expectations"].apply(lambda x: x.get("category", "unknown"))
    results_df = results_df.copy()
    results_df["category"] = categories.values

    score_cols = [
        c for c in results_df.columns
        if c.endswith("/value")
        and c != "evidence_required/value"
        and pd.api.types.is_numeric_dtype(results_df[c])
    ]

    if score_cols:
        overall = results_df[score_cols].mean()
        print("=== Enron GraphRAG Governance Scores (v2) ===")
        for col in score_cols:
            name = col.replace("/value", "")
            print(f"  {name:35s}: {overall[col]:.2f}")
        overall_score = overall.mean()
        print(f"  {'OVERALL':35s}: {overall_score:.2f}")
        print(f"\n  Time: {elapsed:.0f}s ({elapsed / len(eval_df):.1f}s/question)")

        print("\n=== Score Matrix (category x scorer) ===")
        score_agg = {col: "mean" for col in score_cols}
        summary = results_df.groupby("category").agg(score_agg).round(2)
        summary.columns = [c.replace("/value", "") for c in summary.columns]
        print(summary.to_string())

        results_df["avg_score"] = results_df[score_cols].apply(pd.to_numeric, errors="coerce").mean(axis=1)
        worst = results_df.nsmallest(min(5, len(results_df)), "avg_score")
        print("\n=== 5 Lowest Scoring Questions ===")
        for _, row in worst.iterrows():
            q = row.get("inputs/question", row.get("inputs", ""))
            if isinstance(q, dict):
                q = q.get("question", str(q))
            print(f"  [{row['category']}] {q[:80]} -> {row['avg_score']:.2f}")

        if overall_score >= 0.58:
            print(f"\n  TARGET MET ({overall_score:.2f} >= 0.58) — ready to deploy!")
        else:
            print(f"\n  Below target ({overall_score:.2f} < 0.58) — keep iterating.")
    else:
        print("No score columns found in results.")


if __name__ == "__main__":
    main()
