"""Quick inference quality test for the graphrag-bible-agent endpoint.

Uses the same test cases and scoring as the local validation pipeline.
"""
import json
import os
import sys
import time

from databricks.sdk import WorkspaceClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cases import TEST_CASES, score_response, check_quality_gates

ENDPOINT = "graphrag-bible-agent"


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


def main():
    w = WorkspaceClient(profile="DEFAULT")
    print(f"Connected to: {w.config.host}")

    print("=" * 80)
    print(f"  INFERENCE QUALITY TEST — endpoint: {ENDPOINT}")
    print(f"  {len(TEST_CASES)} questions across diverse categories")
    print("=" * 80)

    results = []

    for i, test in enumerate(TEST_CASES, 1):
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
        scores["question"] = q[:60]
        scores["category"] = test["category"]
        scores["latency"] = round(latency, 1)
        results.append(scores)

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

    all_passed, gates = check_quality_gates(results)

    print(f"\n  QUALITY GATES:")
    for g in gates:
        status = "PASS" if g["passed"] else "FAIL"
        print(f"    [{status}] {g['label']}: {g['value']:.2f} (threshold: {g['threshold']})")

    print(f"\n  {'ALL GATES PASSED' if all_passed else 'SOME GATES FAILED'}")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
