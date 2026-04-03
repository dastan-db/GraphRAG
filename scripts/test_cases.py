"""Shared test cases and scoring utilities for GraphRAG agent validation.

Used by:
    - scripts/validate_local.py  (local quality gate)
    - scripts/validate_parity.py (local-vs-Databricks parity)
    - scripts/test_endpoint.py   (deployed endpoint test)
"""
import re

TEST_CASES = [
    {
        "question": "How is Ruth connected to Jesus? Trace the lineage step by step.",
        "expected_entities": ["Ruth", "Boaz", "Obed", "Jesse", "David", "Jesus"],
        "category": "multi-hop lineage",
    },
    {
        "question": "What happened on the road to Damascus in Acts?",
        "expected_entities": ["Saul", "Paul", "Damascus"],
        "category": "single-book event",
    },
    {
        "question": "What significant events happened in Egypt across all the books in our knowledge graph?",
        "expected_entities": ["Egypt", "Joseph", "Moses"],
        "category": "cross-book synthesis",
    },
    {
        "question": "Who was Abraham and what covenant did God make with him?",
        "expected_entities": ["Abraham", "God"],
        "category": "entity profile",
    },
    {
        "question": "Compare the leadership styles of Moses and Paul based on their actions and relationships.",
        "expected_entities": ["Moses", "Paul"],
        "category": "comparative analysis",
    },
]

ENRON_TEST_CASES = [
    {
        "question": "Who sent the most emails in the Enron corpus?",
        "expected_tools": ["get_top_individuals", "query_and_enrich"],
        "forbidden_tool": "get_top_email_pairs",
        "category": "individual_ranking",
    },
    {
        "question": "Which two people exchanged the most emails?",
        "expected_tools": ["get_top_email_pairs", "query_and_enrich"],
        "forbidden_tool": "get_top_individuals",
        "category": "corpus_ranking_pairs",
    },
    {
        "question": "Who did Jeff Skilling report to?",
        "expected_tool": "find_connections",
        "category": "org_hierarchy",
    },
    {
        "question": "What did Kenneth Lay and Jeff Skilling discuss?",
        "expected_tool": "get_dyad_topics",
        "category": "topic_pair",
    },
    {
        "question": "Find emails mentioning 'shred' or 'destroy'",
        "expected_tool": "search_emails",
        "category": "investigation",
    },
    {
        "question": "How are Andrew Fastow and Kenneth Lay connected?",
        "expected_tool": "trace_path",
        "category": "path",
    },
    {
        "question": "Who were Jeff Skilling's top email contacts?",
        "expected_tools": ["find_top_contacts", "query_and_enrich"],
        "category": "communication",
    },
    {
        "question": "What percentage of emails were internal?",
        "expected_tool": "query_and_enrich",
        "category": "genie_analytics",
    },
]

QUALITY_THRESHOLDS = {
    "entity_recall": 0.60,
    "citations": 1.0,
    "success_rate": 0.80,
}

ENRON_QUALITY_THRESHOLDS = {
    "expected_tool_rate": 0.75,
    "forbidden_tool_avoidance": 0.85,
    "success_rate": 0.80,
}

VERSE_PATTERN = re.compile(r"(Genesis|Exodus|Ruth|Matthew|Acts)\s+\d+:\d+")
PROVENANCE_HEADING = re.compile(r"#{1,3}\s*Provenance", re.IGNORECASE)
PATH_INDICATOR = re.compile(r"(→|-->|—\[)")
SOURCES_LINE = re.compile(r"\*?\*?Sources\*?\*?\s*:", re.IGNORECASE)
GROUNDING_LINE = re.compile(r"\*?\*?Grounding\*?\*?\s*:", re.IGNORECASE)
TOOL_CALL_PATTERN = re.compile(r"\b([a-z]+(?:_[a-z0-9]+)+)\s*\(")


def score_response(response: str, expected_entities: list[str]) -> dict:
    """Score an agent response against expected entities and structural quality."""
    citations = VERSE_PATTERN.findall(response)
    has_provenance = bool(PROVENANCE_HEADING.search(response))
    has_path = bool(PATH_INDICATOR.search(response))
    has_sources = bool(SOURCES_LINE.search(response))
    has_grounding = bool(GROUNDING_LINE.search(response))
    provenance_score = sum([has_provenance, has_path, has_sources, has_grounding]) / 4

    response_lower = response.lower()
    entity_hits = [e for e in expected_entities if e.lower() in response_lower]
    entity_recall = len(entity_hits) / len(expected_entities) if expected_entities else 1.0

    answer_section = response.split("### Provenance")[0] if "### Provenance" in response else response
    sentences = [s.strip() for s in re.split(r"[.!?\n]", answer_section) if len(s.strip()) > 20]
    cited_sentences = sum(1 for s in sentences if VERSE_PATTERN.search(s))
    citation_completeness = cited_sentences / len(sentences) if sentences else 0

    return {
        "citations": len(citations),
        "citation_completeness": round(citation_completeness, 2),
        "provenance_score": provenance_score,
        "provenance_components": {
            "heading": has_provenance,
            "path": has_path,
            "sources": has_sources,
            "grounding": has_grounding,
        },
        "entity_recall": round(entity_recall, 2),
        "entity_hits": entity_hits,
        "entity_misses": [e for e in expected_entities if e not in entity_hits],
        "response_length": len(response),
    }


def extract_answer_text(response) -> str:
    """Extract plain text from a ResponsesAgentResponse."""
    parts = []
    for item in response.output:
        item_type = getattr(item, "type", "")
        if item_type == "message":
            for block in getattr(item, "content", []):
                text = None
                if isinstance(block, dict) and block.get("text"):
                    text = block["text"]
                elif hasattr(block, "text"):
                    text = block.text
                if text:
                    parts.append(text)
        elif hasattr(item, "text"):
            parts.append(item.text)
    return "\n".join(parts)


def extract_tool_calls(response) -> list[str]:
    """Extract tool call names from a ResponsesAgentResponse."""
    calls = []
    for item in response.output:
        if getattr(item, "type", "") == "function_call":
            calls.append(getattr(item, "name", "unknown"))
    return calls


def infer_tool_calls_from_text(response: str) -> list[str]:
    seen: set[str] = set()
    tool_calls: list[str] = []
    for match in TOOL_CALL_PATTERN.finditer(response or ""):
        tool_name = match.group(1)
        if tool_name not in seen:
            seen.add(tool_name)
            tool_calls.append(tool_name)
    return tool_calls


def check_quality_gates(results: list[dict], thresholds: dict | None = None) -> tuple[bool, list[dict]]:
    """Evaluate quality gates against aggregated results.

    Returns (all_passed, gate_details) where gate_details is a list of
    {name, value, threshold, label, passed} dicts.
    """
    thresholds = thresholds or QUALITY_THRESHOLDS

    valid = [r for r in results if "entity_recall" in r]
    if not valid:
        return False, [{"name": "success_rate", "value": 0, "threshold": thresholds.get("success_rate", 0.80),
                        "label": "Success rate", "passed": False}]

    avg_entity = sum(r["entity_recall"] for r in valid) / len(valid)
    avg_citations = sum(r["citations"] for r in valid) / len(valid)
    success_rate = len(valid) / len(results) if results else 0

    gates = [
        {"name": "entity_recall", "value": avg_entity,
         "threshold": thresholds.get("entity_recall", 0.60),
         "label": "Avg entity recall"},
        {"name": "citations", "value": avg_citations,
         "threshold": thresholds.get("citations", 1.0),
         "label": "Avg citations"},
        {"name": "success_rate", "value": success_rate,
         "threshold": thresholds.get("success_rate", 0.80),
         "label": "Success rate"},
    ]
    for g in gates:
        g["passed"] = g["value"] >= g["threshold"]

    return all(g["passed"] for g in gates), gates


def score_enron_response(response: str, tool_calls: list[str], case: dict) -> dict:
    expected_tools = list(case.get("expected_tools", []) or [])
    if not expected_tools and case.get("expected_tool"):
        expected_tools = [case["expected_tool"]]
    expected_tool = " / ".join(expected_tools)
    forbidden_tool = case.get("forbidden_tool", "")
    tool_set = set(tool_calls) | set(infer_tool_calls_from_text(response))
    expected_tool_hit = (
        1.0 if not expected_tools else float(any(tool in tool_set for tool in expected_tools))
    )
    forbidden_tool_avoided = 1.0 if not forbidden_tool else float(forbidden_tool not in tool_set)
    response_ok = float(len((response or "").strip()) >= 20 and not response.startswith("ERROR:"))
    return {
        "question": case["question"][:60],
        "category": case["category"],
        "expected_tool": expected_tool,
        "expected_tools": expected_tools,
        "expected_tool_hit": expected_tool_hit,
        "forbidden_tool": forbidden_tool,
        "forbidden_tool_avoided": forbidden_tool_avoided,
        "non_empty_response": response_ok,
        "tool_calls": list(tool_calls),
        "response_length": len(response or ""),
    }


def check_enron_quality_gates(
    results: list[dict],
    thresholds: dict | None = None,
) -> tuple[bool, list[dict]]:
    thresholds = thresholds or ENRON_QUALITY_THRESHOLDS
    valid = [r for r in results if "expected_tool_hit" in r]
    if not valid:
        return False, [
            {
                "name": "success_rate",
                "value": 0.0,
                "threshold": thresholds.get("success_rate", 0.80),
                "label": "Success rate",
                "passed": False,
            }
        ]

    expected_tool_rate = sum(r["expected_tool_hit"] for r in valid) / len(valid)
    forbidden_tool_avoidance = sum(r["forbidden_tool_avoided"] for r in valid) / len(valid)
    success_rate = sum(r["non_empty_response"] for r in valid) / len(valid)

    gates = [
        {
            "name": "expected_tool_rate",
            "value": expected_tool_rate,
            "threshold": thresholds.get("expected_tool_rate", 0.75),
            "label": "Expected tool hit rate",
        },
        {
            "name": "forbidden_tool_avoidance",
            "value": forbidden_tool_avoidance,
            "threshold": thresholds.get("forbidden_tool_avoidance", 0.85),
            "label": "Forbidden tool avoidance",
        },
        {
            "name": "success_rate",
            "value": success_rate,
            "threshold": thresholds.get("success_rate", 0.80),
            "label": "Success rate",
        },
    ]
    for gate in gates:
        gate["passed"] = gate["value"] >= gate["threshold"]

    return all(g["passed"] for g in gates), gates
