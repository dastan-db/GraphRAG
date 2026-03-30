"""Dedicated evidence traceability evaluation harness.

Runs the evidence-focused scorers (provenance_completeness, citation_accuracy,
retrieval_relevance) plus the standard scorers, with support for knob-parameterized
runs for the tuning loop. Logs all KPIs to MLflow with structured run_tags.

Usage:
    python scripts/eval_evidence.py                           # full evidence eval
    python scripts/eval_evidence.py --phase C1                # C1 knobs only
    python scripts/eval_evidence.py --knob min_relevance_threshold=0.4
    python scripts/eval_evidence.py --baseline                # log baseline run
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

from scripts.eval_local import (
    EVAL_DATA,
    ALL_SCORERS,
    predict_fn,
    JUDGE_ENDPOINT,
)

EVIDENCE_CATEGORIES = [
    "org_hierarchy", "org_hierarchy_evidence", "entity_pair_evidence",
    "relationship_evidence", "keyword_evidence",
]


def compute_pipeline_kpis(results_df: pd.DataFrame, eval_df: pd.DataFrame) -> dict:
    """Compute the 7 KPIs from the plan (5 per-component + 2 cross-component)."""
    kpis = {}

    prov_col = "provenance_completeness/value"
    if prov_col in results_df.columns:
        valid = results_df[prov_col].dropna()
        kpis["C4_provenance_completeness"] = round(valid.mean(), 3) if len(valid) else 0.0

    cit_col = "citation_accuracy/value"
    if cit_col in results_df.columns:
        valid = results_df[cit_col].dropna()
        kpis["C5_citation_accuracy"] = round(valid.mean(), 3) if len(valid) else 0.0

    rel_col = "retrieval_relevance/value"
    if rel_col in results_df.columns:
        valid = results_df[rel_col].dropna()
        kpis["C3_ranking_precision"] = round(valid.mean(), 3) if len(valid) else 0.0

    ev_col = "evidence_quality/value"
    if ev_col in results_df.columns:
        valid = results_df[ev_col].dropna()
        kpis["C2_retrieval_relevance"] = round(valid.mean(), 3) if len(valid) else 0.0

    score_cols = [
        c for c in results_df.columns
        if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])
    ]
    if score_cols:
        kpis["overall_score"] = round(results_df[score_cols].mean().mean(), 3)

    categories = eval_df["expectations"].apply(lambda x: x.get("category", "unknown"))
    results_with_cat = results_df.copy()
    results_with_cat["category"] = categories.values

    evidence_mask = results_with_cat["category"].isin(EVIDENCE_CATEGORIES)
    if evidence_mask.any() and prov_col in results_df.columns:
        ev_scores = results_with_cat.loc[evidence_mask, prov_col].dropna()
        kpis["evidence_category_provenance"] = round(ev_scores.mean(), 3) if len(ev_scores) else 0.0

    return kpis


def main():
    parser = argparse.ArgumentParser(description="Evidence traceability evaluation")
    parser.add_argument("--cases", type=int, default=None)
    parser.add_argument("--phase", type=str, default=None, help="Filter: C1, C2, C3, C4, C5")
    parser.add_argument("--baseline", action="store_true", help="Tag as baseline run")
    parser.add_argument("--knob", action="append", default=[], help="key=value knob overrides")
    parser.add_argument("--run-name", type=str, default="evidence_eval")
    parser.add_argument("--judge", type=str, default=None)
    args = parser.parse_args()

    if args.judge:
        os.environ["GRAPHRAG_JUDGE_ENDPOINT"] = args.judge

    knob_overrides = {}
    for kv in args.knob:
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                knob_overrides[k] = json.loads(v)
            except json.JSONDecodeError:
                knob_overrides[k] = v

    if knob_overrides:
        from src.agent.agent_serving import EVIDENCE_CONFIG
        for k, v in knob_overrides.items():
            if k in EVIDENCE_CONFIG:
                EVIDENCE_CONFIG[k] = v
                print(f"  Knob override: {k} = {v}")

    data = EVAL_DATA
    if args.phase:
        phase_categories = {
            "C1": ["org_hierarchy", "org_hierarchy_evidence"],
            "C2": ["entity_pair_evidence", "relationship_evidence", "communication"],
            "C3": ["org_hierarchy_evidence", "entity_pair_evidence", "relationship_evidence"],
            "C4": EVIDENCE_CATEGORIES,
            "C5": None,
        }
        cats = phase_categories.get(args.phase.upper())
        if cats:
            data = [d for d in data if d["category"] in cats]

    if args.cases:
        data = data[:args.cases]

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
    print(f"Evidence Eval: {len(eval_df)} questions | phase={args.phase or 'all'}")
    if knob_overrides:
        print(f"Knob overrides: {knob_overrides}")
    print()

    run_tags = {
        "eval_type": "evidence_traceability",
        "phase": args.phase or "all",
    }
    if args.baseline:
        run_tags["version"] = "v0-baseline"
    if knob_overrides:
        run_tags["knobs"] = json.dumps(knob_overrides)

    t0 = time.time()
    with mlflow.start_run(run_name=args.run_name, tags=run_tags):
        results = mlflow.genai.evaluate(
            data=eval_df,
            predict_fn=predict_fn,
            scorers=ALL_SCORERS,
        )

        results_df = results.tables["eval_results"]
        kpis = compute_pipeline_kpis(results_df, eval_df)

        for k, v in kpis.items():
            mlflow.log_metric(k, v)

        if knob_overrides:
            for k, v in knob_overrides.items():
                mlflow.log_param(f"knob_{k}", str(v))

    elapsed = time.time() - t0

    print(f"\n=== Evidence Traceability KPIs ===")
    for k, v in sorted(kpis.items()):
        print(f"  {k:40s}: {v:.3f}")
    print(f"\n  Time: {elapsed:.0f}s ({elapsed / max(len(eval_df), 1):.1f}s/question)")

    score_cols = [
        c for c in results_df.columns
        if c.endswith("/value") and pd.api.types.is_numeric_dtype(results_df[c])
    ]
    if score_cols:
        overall = results_df[score_cols].mean()
        print("\n=== Per-Scorer Averages ===")
        for col in sorted(score_cols):
            name = col.replace("/value", "")
            print(f"  {name:35s}: {overall[col]:.3f}")


if __name__ == "__main__":
    main()
