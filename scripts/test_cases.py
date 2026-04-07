"""Shared Enron test cases and scoring utilities for GraphRAG validation.

Used by:
    - scripts/validate_local.py
    - scripts/validate_parity.py
    - scripts/test_endpoint.py
"""
import re

ENRON_TEST_CASES = [
    {
        "question": "Who communicated most frequently with Kenneth Lay?",
        "expected_entities": ["Kenneth Lay"],
        "expected_tools": ["find_top_contacts", "query_and_enrich"],
        "category": "communication_summary",
    },
    {
        "question": "How are Kenneth Lay and Andrew Fastow connected?",
        "expected_entities": ["Kenneth Lay", "Andrew Fastow"],
        "expected_tools": ["trace_path", "get_relationship_evidence"],
        "category": "path",
    },
    {
        "question": "Find emails mentioning California energy trading decisions.",
        "expected_entities": ["California"],
        "expected_tools": ["search_emails"],
        "category": "investigation",
    },
    {
        "question": "Who did Jeff Skilling report to?",
        "expected_entities": ["Jeff Skilling"],
        "expected_tool": "find_connections",
        "category": "org_hierarchy",
    },
    {
        "question": "What did Kenneth Lay and Jeff Skilling discuss?",
        "expected_entities": ["Kenneth Lay", "Jeff Skilling"],
        "expected_tool": "get_dyad_topics",
        "category": "topic_pair",
    },
    {
        "question": "Who sent the most emails in the Enron corpus?",
        "expected_entities": [],
        "expected_tools": ["get_top_individuals", "query_and_enrich"],
        "forbidden_tool": "get_top_email_pairs",
        "category": "individual_ranking",
    },
    {
        "question": "Which two people exchanged the most emails?",
        "expected_entities": [],
        "expected_tools": ["get_top_email_pairs", "query_and_enrich"],
        "forbidden_tool": "get_top_individuals",
        "category": "corpus_ranking_pairs",
    },
    {
        "question": "Who were Jeff Skilling's top email contacts?",
        "expected_entities": ["Jeff Skilling"],
        "expected_tools": ["find_top_contacts", "query_and_enrich"],
        "category": "communication",
    },
    {
        "question": "What percentage of emails were internal?",
        "expected_entities": [],
        "expected_tool": "query_and_enrich",
        "category": "genie_analytics",
    },
]

ENRON_QUALITY_THRESHOLDS = {
    "expected_tool_rate": 0.75,
    "forbidden_tool_avoidance": 0.85,
    "success_rate": 0.80,
}

PROVENANCE_HEADING = re.compile(r"#{1,3}\s*Provenance", re.IGNORECASE)
PATH_INDICATOR = re.compile(r"(→|-->|—\[)")
SOURCES_LINE = re.compile(r"\*?\*?Sources\*?\*?\s*:", re.IGNORECASE)
GROUNDING_LINE = re.compile(r"\*?\*?Grounding\*?\*?\s*:", re.IGNORECASE)
TOOL_CALL_PATTERN = re.compile(r"\b([a-z]+(?:_[a-z0-9]+)+)\s*\(")


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


def score_enron_response(response: str, tool_calls: list[str], case: dict) -> dict:
    expected_tools = list(case.get("expected_tools", []) or [])
    if not expected_tools and case.get("expected_tool"):
        expected_tools = [case["expected_tool"]]
    expected_tool = " / ".join(expected_tools)
    forbidden_tool = case.get("forbidden_tool", "")
    tool_set = set(tool_calls) | set(infer_tool_calls_from_text(response))
    response_lower = (response or "").lower()
    expected_entities = case.get("expected_entities", []) or []
    entity_hits = [entity for entity in expected_entities if entity.lower() in response_lower]
    expected_tool_hit = (
        1.0 if not expected_tools else float(any(tool in tool_set for tool in expected_tools))
    )
    forbidden_tool_avoided = 1.0 if not forbidden_tool else float(forbidden_tool not in tool_set)
    response_ok = float(len((response or "").strip()) >= 20 and not response.startswith("ERROR:"))
    has_provenance = bool(PROVENANCE_HEADING.search(response or ""))
    has_path = bool(PATH_INDICATOR.search(response or ""))
    has_sources = bool(SOURCES_LINE.search(response or ""))
    has_grounding = bool(GROUNDING_LINE.search(response or ""))
    provenance_score = sum([has_provenance, has_path, has_sources, has_grounding]) / 4
    return {
        "question": case["question"][:60],
        "category": case["category"],
        "expected_tool": expected_tool,
        "expected_tools": expected_tools,
        "expected_tool_hit": expected_tool_hit,
        "forbidden_tool": forbidden_tool,
        "forbidden_tool_avoided": forbidden_tool_avoided,
        "non_empty_response": response_ok,
        "expected_entities": expected_entities,
        "entity_hits": entity_hits,
        "entity_recall": (
            round(len(entity_hits) / len(expected_entities), 2)
            if expected_entities
            else 1.0
        ),
        "provenance_score": provenance_score,
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
