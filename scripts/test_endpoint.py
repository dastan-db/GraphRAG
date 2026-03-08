"""Quick inference quality test for the graphrag-bible-agent endpoint."""

import json
import re
import sys
import time

from databricks.sdk import WorkspaceClient

ENDPOINT = "graphrag-bible-agent"

TEST_QUESTIONS = [
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

VERSE_PATTERN = re.compile(r"(Genesis|Exodus|Ruth|Matthew|Acts)\s+\d+:\d+")
PROVENANCE_HEADING = re.compile(r"#{1,3}\s*Provenance", re.IGNORECASE)
PATH_INDICATOR = re.compile(r"(→|-->|—\[)")
SOURCES_LINE = re.compile(r"\*?\*?Sources\*?\*?\s*:", re.IGNORECASE)
GROUNDING_LINE = re.compile(r"\*?\*?Grounding\*?\*?\s*:", re.IGNORECASE)


def query_endpoint(w: WorkspaceClient, question: str) -> tuple[str, float]:
    """Query the ResponsesAgent endpoint using the Responses API format."""
    start = time.time()
    try:
        payload = {"input": [{"role": "user", "content": question}]}
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{ENDPOINT}/invocations",
            body=payload,
        )
        elapsed = time.time() - start

        # ResponsesAgent output format
        texts = []
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            elif "text" in item:
                texts.append(item["text"])
        if texts:
            return "\n".join(texts), elapsed
        return f"EMPTY RESPONSE: {json.dumps(resp)[:500]}", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return f"ERROR: {e}", elapsed


def score_response(response: str, expected_entities: list[str]) -> dict:
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


def main():
    w = WorkspaceClient(profile="DEFAULT")
    print(f"Connected to: {w.config.host}")

    print("=" * 80)
    print(f"  INFERENCE QUALITY TEST — endpoint: {ENDPOINT}")
    print(f"  {len(TEST_QUESTIONS)} questions across diverse categories")
    print("=" * 80)

    results = []

    for i, test in enumerate(TEST_QUESTIONS, 1):
        q = test["question"]
        print(f"\n{'─' * 80}")
        print(f"  Q{i} [{test['category']}]")
        print(f"  {q}")
        print(f"{'─' * 80}")

        response, latency = query_endpoint(w, q)

        if response.startswith("ERROR"):
            print(f"  FAILED: {response}")
            results.append({"question": q, "status": "ERROR", "latency": latency})
            continue

        scores = score_response(response, test["expected_entities"])
        results.append({**scores, "question": q[:60], "category": test["category"], "latency": round(latency, 1)})

        print(f"  Latency:               {latency:.1f}s")
        print(f"  Response length:       {scores['response_length']} chars")
        print(f"  Verse citations:       {scores['citations']}")
        print(f"  Citation completeness: {scores['citation_completeness']:.0%}")
        print(f"  Provenance score:      {scores['provenance_score']:.0%}  {scores['provenance_components']}")
        print(f"  Entity recall:         {scores['entity_recall']:.0%}  hits={scores['entity_hits']}")
        if scores["entity_misses"]:
            print(f"                         misses={scores['entity_misses']}")

        print(f"\n  --- Response preview (first 600 chars) ---")
        print(f"  {response[:600].replace(chr(10), chr(10) + '  ')}")
        if len(response) > 600:
            print(f"  ... ({len(response) - 600} more chars)")

    print(f"\n{'=' * 80}")
    print("  SUMMARY")
    print(f"{'=' * 80}")

    valid = [r for r in results if "citations" in r]
    errors = [r for r in results if "status" in r and r["status"] == "ERROR"]

    if not valid:
        print(f"  All {len(results)} queries failed!")
        for r in errors:
            print(f"    - {r['question'][:60]}")
        sys.exit(1)

    avg_latency = sum(r["latency"] for r in valid) / len(valid)
    avg_citations = sum(r["citations"] for r in valid) / len(valid)
    avg_cite_comp = sum(r["citation_completeness"] for r in valid) / len(valid)
    avg_prov = sum(r["provenance_score"] for r in valid) / len(valid)
    avg_entity = sum(r["entity_recall"] for r in valid) / len(valid)

    print(f"  Questions:             {len(valid)}/{len(results)} succeeded")
    if errors:
        print(f"  Errors:                {len(errors)}")
    print(f"  Avg latency:           {avg_latency:.1f}s")
    print(f"  Avg verse citations:   {avg_citations:.1f}")
    print(f"  Avg citation complete: {avg_cite_comp:.0%}")
    print(f"  Avg provenance score:  {avg_prov:.0%}")
    print(f"  Avg entity recall:     {avg_entity:.0%}")

    thresholds = {
        "provenance": (avg_prov, 0.75, "Provenance structure >= 75%"),
        "entity_recall": (avg_entity, 0.60, "Entity recall >= 60%"),
        "citations": (avg_citations, 1.0, "Avg citations >= 1"),
        "success_rate": (len(valid) / len(results), 0.80, "Success rate >= 80%"),
    }

    print(f"\n  QUALITY GATES:")
    gate_pass = True
    for key, (value, threshold, label) in thresholds.items():
        passed = value >= threshold
        status = "PASS" if passed else "FAIL"
        print(f"    [{status}] {label}: {value:.2f} (threshold: {threshold})")
        if not passed:
            gate_pass = False

    print(f"\n  {'ALL GATES PASSED' if gate_pass else 'SOME GATES FAILED'}")
    sys.exit(0 if gate_pass else 1)


if __name__ == "__main__":
    main()
