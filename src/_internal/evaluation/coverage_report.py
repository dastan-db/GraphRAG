from __future__ import annotations

import argparse
import json
from pathlib import Path

from .capability_scoring import build_score_report, load_eval_results
from .question_bank_avl import build_avl_curation_queue
from src.evaluation.question_bank import build_coverage_rows, build_domain_coverage_rows, load_question_bank
from .question_bank_curation import (
    build_curation_rows,
    build_curation_summary,
    get_latency_playbook,
)


def _format_table(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    headers = list(rows[0].keys())
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row.get(header, ""))))

    header_line = " | ".join(header.ljust(widths[header]) for header in headers)
    divider = "-+-".join("-" * widths[header] for header in headers)
    body = [
        " | ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers)
        for row in rows
    ]
    return "\n".join([header_line, divider, *body])


def _write_output(text: str, output_path: str | None) -> None:
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        print(text)


def _render_score_report(report: dict) -> str:
    parts: list[str] = []

    summary_rows = [
        {
            "corpus": row.get("corpus"),
            "capability_coverage": row.get("capability_coverage"),
            "capability_quality": row.get("capability_quality"),
            "domain_coverage": row.get("domain_coverage"),
            "readiness": row.get("readiness"),
            "required_cells": row.get("required_cells"),
            "thin_required_cells": row.get("thin_required_cells"),
        }
        for row in report.get("summary", [])
    ]
    parts.append("## Summary")
    parts.append(_format_table(summary_rows))

    required_rows = [
        row for row in report.get("capability_scorecard", [])
        if row.get("policy") == "required"
    ]
    parts.append("\n## Required Capability Cells")
    parts.append(_format_table(required_rows))

    thin_rows = [
        row for row in required_rows
        if row.get("thin_splits")
    ]
    parts.append("\n## Thin Or Empty Cells")
    parts.append(_format_table(thin_rows[:20]))

    weakest_cells = sorted(
        required_rows,
        key=lambda row: (
            row.get("coverage_score") is None,
            row.get("coverage_score", 0),
            row.get("quality_score") is None,
            row.get("quality_score", 0),
        ),
    )
    parts.append("\n## Weakest Capability Cells")
    parts.append(_format_table(weakest_cells[:10]))

    weakest_domains = sorted(
        report.get("domain_scorecard", []),
        key=lambda row: (
            row.get("domain_coverage_score") is None,
            row.get("domain_coverage_score", 0),
        ),
    )
    parts.append("\n## Weakest Domains")
    parts.append(_format_table(weakest_domains[:10]))

    backlog = [
        {
            "corpus": row.get("corpus"),
            "domain_primary": row.get("domain_primary"),
            "attorney_category": row.get("attorney_category"),
            "architecture_primary": row.get("architecture_primary"),
            "gap_type": row.get("gap_type"),
            "severity": row.get("severity"),
            "detail": row.get("detail"),
        }
        for row in report.get("gap_backlog", [])
    ]
    parts.append("\n## AVL Gap Backlog")
    parts.append(_format_table(backlog[:20]))

    overlays = report.get("data_confidence_overlay", {})
    if overlays:
        overlay_rows = [
            {
                "corpus": row.get("corpus"),
                "status": row.get("status"),
                "score": row.get("score"),
                "avg_coverage_pct": row.get("avg_coverage_pct"),
                "worst_null_rate": row.get("worst_null_rate"),
                "reason": row.get("reason", ""),
            }
            for row in overlays.values()
        ]
        parts.append("\n## Data Confidence Overlay")
        parts.append(_format_table(overlay_rows))

    return "\n".join(parts)


def _render_curation_report(payload: dict) -> str:
    parts: list[str] = []
    parts.append("## Curation Summary")
    parts.append(_format_table(payload.get("summary", [])))

    review_queue = [
        {
            "question_id": row.get("question_id"),
            "corpus": row.get("corpus"),
            "status": row.get("status"),
            "eval_split": row.get("eval_split"),
            "recommended_status": row.get("recommended_status"),
            "recommended_eval_split": row.get("recommended_eval_split"),
            "validation_status": row.get("validation_status"),
            "review_priority": row.get("review_priority"),
            "bucket_mismatch": row.get("bucket_mismatch"),
            "latency_profile": row.get("latency_profile"),
            "parallel_safe": row.get("parallel_safe"),
            "question_text": row.get("question_text"),
        }
        for row in payload.get("rows", [])
        if row.get("review_priority") != "low" or row.get("bucket_mismatch")
    ]
    parts.append("\n## Review Queue")
    parts.append(_format_table(review_queue[:30]))

    validated_rows = [
        {
            "question_id": row.get("question_id"),
            "corpus": row.get("corpus"),
            "eval_split": row.get("eval_split"),
            "quality_score": row.get("quality_score"),
            "source_count": row.get("source_count"),
            "latency_profile": row.get("latency_profile"),
            "question_text": row.get("question_text"),
        }
        for row in payload.get("rows", [])
        if row.get("validation_status") == "validated"
    ]
    parts.append("\n## Validated Seed Batch")
    parts.append(_format_table(validated_rows[:20]))

    parts.append("\n## Latency Playbook")
    parts.append(_format_table(payload.get("latency_playbook", [])))
    return "\n".join(parts)


def _render_avl_report(payload: dict) -> str:
    parts: list[str] = []
    parts.append("## AVL Queue Summary")
    parts.append(_format_table(payload.get("summary", [])))

    gap_rows = [
        {
            "target_id": row.get("target_id"),
            "corpus": row.get("corpus"),
            "domain_primary": row.get("domain_primary"),
            "attorney_category": row.get("attorney_category"),
            "architecture_primary": row.get("architecture_primary"),
            "gap_type": row.get("gap_type"),
            "severity": row.get("severity"),
            "latency_profile": row.get("latency_profile"),
            "detail": row.get("detail"),
        }
        for row in payload.get("gap_targets", [])
    ]
    parts.append("\n## Gap Targets")
    parts.append(_format_table(gap_rows))

    review_rows = [
        {
            "target_id": row.get("target_id"),
            "question_id": row.get("question_id"),
            "corpus": row.get("corpus"),
            "validation_status": row.get("validation_status"),
            "review_priority": row.get("review_priority"),
            "bucket_mismatch": row.get("bucket_mismatch"),
            "latency_profile": row.get("latency_profile"),
            "question_text": row.get("question_text"),
        }
        for row in payload.get("review_targets", [])
    ]
    parts.append("\n## Review Targets")
    parts.append(_format_table(review_rows))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the unified evaluation-bank coverage or score reports")
    parser.add_argument("--mode", choices=["counts", "scorecard", "curation", "avl"], default="counts", help="Which reporting surface to render")
    parser.add_argument("--corpus", choices=["enron", "bible"], default=None, help="Limit coverage to one corpus")
    parser.add_argument("--results-path", type=str, default=None, help="Optional saved mlflow eval_results table (json/jsonl/csv/parquet/pickle)")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Render as plain table text or JSON")
    parser.add_argument("--output", type=str, default=None, help="Optional file to write instead of stdout")
    args = parser.parse_args()

    if args.mode == "counts":
        payload = {
            "capability_rows": build_coverage_rows(corpus=args.corpus),
            "domain_rows": build_domain_coverage_rows(corpus=args.corpus),
        }
        if args.format == "json":
            _write_output(json.dumps(payload, indent=2), args.output)
            return
        text = "\n".join(
            [
                "## Capability Cells",
                _format_table(payload["capability_rows"]),
                "",
                "## Domains",
                _format_table(payload["domain_rows"]),
            ]
        )
        _write_output(text, args.output)
        return

    if args.mode == "curation":
        records = load_question_bank(corpus=args.corpus, status=None)
        rows = build_curation_rows(records)
        payload = {
            "summary": build_curation_summary(records),
            "rows": rows,
            "latency_playbook": get_latency_playbook(),
        }
        if args.format == "json":
            _write_output(json.dumps(payload, indent=2), args.output)
            return
        _write_output(_render_curation_report(payload), args.output)
        return

    if args.mode == "avl":
        payload = build_avl_curation_queue(corpus=args.corpus)
        if args.format == "json":
            _write_output(json.dumps(payload, indent=2), args.output)
            return
        _write_output(_render_avl_report(payload), args.output)
        return

    eval_results = load_eval_results(args.results_path) if args.results_path else None
    report = build_score_report(eval_results=eval_results, corpus=args.corpus)
    if args.format == "json":
        _write_output(json.dumps(report, indent=2), args.output)
        return
    _write_output(_render_score_report(report), args.output)


if __name__ == "__main__":
    main()
