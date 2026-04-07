from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.evaluation.question_bank import (
    SPLIT_BUCKETS,
    _normalize_record,
    get_domain_categories,
    get_required_coverage_cells,
    load_question_bank,
)

COVERAGE_TARGETS = {"train": 3, "test": 1, "holdout": 1}
QUALITY_SPLIT_WEIGHTS = {"test": 0.4, "holdout": 0.6}
TARGET_SPLIT_DISTRIBUTION = {"train": 0.6, "test": 0.2, "holdout": 0.2}
LOW_QUALITY_THRESHOLD = 0.70
HIGH_VARIANCE_STD = 0.20

DOMAIN_EXPECTED_CAPABILITY_CELLS = {
    "enron": {
        "governance_leadership": [
            ("org_structure", "org_hierarchy_retrieval"),
            ("person_profile", "entity_summary_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("case_synthesis", "synthesis_provenance"),
        ],
        "financial_structures_risk": [
            ("person_profile", "entity_summary_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("documentary_evidence", "evidence_drilldown"),
            ("timeline_reconstruction", "timeline_retrieval"),
            ("corroboration_challenge", "entity_resolution"),
        ],
        "trading_markets_platforms": [
            ("quantitative_analysis", "analytics_sql_genie"),
            ("topic_investigation", "topic_keyword_retrieval"),
            ("timeline_reconstruction", "timeline_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
        ],
        "business_units_projects": [
            ("person_profile", "entity_summary_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("case_synthesis", "synthesis_provenance"),
            ("documentary_evidence", "evidence_drilldown"),
        ],
        "legal_audit_external_stakeholders": [
            ("documentary_evidence", "evidence_drilldown"),
            ("topic_investigation", "topic_keyword_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("case_synthesis", "synthesis_provenance"),
        ],
        "communications_dynamics_temporal": [
            ("timeline_reconstruction", "timeline_retrieval"),
            ("topic_investigation", "topic_keyword_retrieval"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("documentary_evidence", "evidence_drilldown"),
        ],
        "access_privilege_governance": [
            ("access_control_probe", "access_control_isolation"),
            ("documentary_evidence", "evidence_drilldown"),
            ("case_synthesis", "synthesis_provenance"),
        ],
    },
    "bible": {
        "torah_origins_covenant": [
            ("case_synthesis", "synthesis_provenance"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("person_profile", "entity_summary_retrieval"),
            ("timeline_reconstruction", "timeline_retrieval"),
            ("topic_investigation", "topic_keyword_retrieval"),
        ],
        "messianic_lineage": [
            ("relationship_analysis", "relationship_path_retrieval"),
            ("person_profile", "entity_summary_retrieval"),
            ("case_synthesis", "synthesis_provenance"),
            ("corroboration_challenge", "entity_resolution"),
        ],
        "gospels_matthew": [
            ("case_synthesis", "synthesis_provenance"),
            ("person_profile", "entity_summary_retrieval"),
            ("documentary_evidence", "evidence_drilldown"),
            ("topic_investigation", "topic_keyword_retrieval"),
        ],
        "acts_apostolic_church": [
            ("case_synthesis", "synthesis_provenance"),
            ("person_profile", "entity_summary_retrieval"),
            ("timeline_reconstruction", "timeline_retrieval"),
            ("documentary_evidence", "evidence_drilldown"),
        ],
        "cross_book_narrative_synthesis": [
            ("case_synthesis", "synthesis_provenance"),
            ("relationship_analysis", "relationship_path_retrieval"),
            ("timeline_reconstruction", "timeline_retrieval"),
            ("corroboration_challenge", "entity_resolution"),
        ],
    },
}


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
    return {}


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return [value]


def _extract_field(
    frame: pd.DataFrame,
    flat_column: str,
    nested_column: str,
    nested_key: str,
) -> pd.Series:
    if flat_column in frame.columns:
        return frame[flat_column]
    if nested_column in frame.columns:
        return frame[nested_column].apply(
            lambda payload: _coerce_mapping(payload).get(nested_key)
        )
    return pd.Series([None] * len(frame), index=frame.index)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _geometric_mean(values: Iterable[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    if any(value <= 0 for value in usable):
        return 0.0
    return math.exp(sum(math.log(value) for value in usable) / len(usable))


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    cleaned = frame.astype(object).where(pd.notnull(frame), None)
    return cleaned.to_dict(orient="records")


def load_eval_results(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = data.get("rows", data.get("eval_results", data))
        return pd.DataFrame(data)
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return pd.DataFrame(rows)
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    raise ValueError(f"Unsupported results format: {path}")


def question_inventory_frame(
    *,
    corpus: str | None = None,
    include_interaction_types: Iterable[str] | None = None,
) -> pd.DataFrame:
    allowed_types = set(include_interaction_types or ["single_turn", "multi_turn"])
    records = [
        record
        for record in load_question_bank(corpus=corpus, status="active")
        if record.get("interaction_type") in allowed_types
    ]
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    frame["avg_score"] = pd.NA
    frame["bank_match"] = True
    return frame


def normalize_eval_results(eval_results: pd.DataFrame) -> pd.DataFrame:
    frame = eval_results.copy()
    normalized = pd.DataFrame(index=frame.index)
    normalized["question_id"] = _extract_field(frame, "expectations/question_id", "expectations", "question_id")
    normalized["question_text"] = _extract_field(frame, "inputs/question", "inputs", "question")
    normalized["corpus"] = _extract_field(frame, "expectations/corpus", "expectations", "corpus")
    normalized["eval_split"] = _extract_field(frame, "expectations/eval_split", "expectations", "eval_split")
    normalized["attorney_category"] = _extract_field(
        frame,
        "expectations/attorney_category",
        "expectations",
        "attorney_category",
    )
    normalized["architecture_primary"] = _extract_field(
        frame,
        "expectations/architecture_primary",
        "expectations",
        "architecture_primary",
    )
    normalized["architecture_secondary"] = _extract_field(
        frame,
        "expectations/architecture_secondary",
        "expectations",
        "architecture_secondary",
    ).apply(_coerce_list)
    normalized["domain_primary"] = _extract_field(
        frame,
        "expectations/domain_primary",
        "expectations",
        "domain_primary",
    )
    normalized["domain_secondary"] = _extract_field(
        frame,
        "expectations/domain_secondary",
        "expectations",
        "domain_secondary",
    ).apply(_coerce_list)
    normalized["source_type"] = _extract_field(frame, "expectations/source_type", "expectations", "source_type")
    normalized["coverage_policy"] = _extract_field(
        frame,
        "expectations/coverage_policy",
        "expectations",
        "coverage_policy",
    )
    normalized["suite_tags"] = _extract_field(frame, "expectations/suite_tags", "expectations", "suite_tags").apply(_coerce_list)
    normalized["expected_entities"] = _extract_field(
        frame,
        "expectations/expected_entities",
        "expectations",
        "expected_entities",
    ).apply(_coerce_list)
    normalized["permitted_books"] = _extract_field(frame, "inputs/permitted_books", "inputs", "permitted_books").apply(_coerce_list)
    normalized["access_tier"] = _extract_field(frame, "inputs/access_tier", "inputs", "access_tier")

    score_columns = [column for column in frame.columns if column.endswith("/value")]
    for column in score_columns:
        normalized[column] = pd.to_numeric(frame[column], errors="coerce")
    if "avg_score" in frame.columns:
        normalized["avg_score"] = pd.to_numeric(frame["avg_score"], errors="coerce")
    elif score_columns:
        normalized["avg_score"] = normalized[score_columns].mean(axis=1)
    else:
        normalized["avg_score"] = pd.NA
    normalized["score_column_count"] = len(score_columns)
    return normalized


def enrich_eval_results(
    eval_results: pd.DataFrame,
    *,
    corpus: str | None = None,
    bank_records: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    normalized = normalize_eval_results(eval_results)
    bank_records = bank_records or load_question_bank(corpus=corpus, status="active")
    by_id = {record["question_id"]: record for record in bank_records}
    by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in bank_records:
        by_question[record.get("question_text", "")].append(record)

    rows: list[dict[str, Any]] = []
    for row in normalized.to_dict(orient="records"):
        bank_record = by_id.get(row.get("question_id"))
        if not bank_record and row.get("question_text"):
            candidates = by_question.get(row["question_text"], [])
            if row.get("permitted_books"):
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.get("input_overrides", {}).get("permitted_books", []) == row["permitted_books"]
                ]
            if row.get("access_tier"):
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.get("input_overrides", {}).get("access_tier") == row["access_tier"]
                ]
            if len(candidates) == 1:
                bank_record = candidates[0]

        fallback_record = bank_record or _normalize_record(
            {
                "question_id": row.get("question_id", ""),
                "question_text": row.get("question_text", ""),
                "corpus": row.get("corpus") or corpus,
                "attorney_category": row.get("attorney_category"),
                "architecture_primary": row.get("architecture_primary"),
                "architecture_secondary": row.get("architecture_secondary", []),
                "suite_tags": row.get("suite_tags", []),
                "expected_entities": row.get("expected_entities", []),
                "source_type": row.get("source_type", "eval_result"),
                "interaction_type": "single_turn",
            }
        )

        merged = {
            **fallback_record,
            **row,
            "question_id": row.get("question_id") or fallback_record.get("question_id"),
            "question_text": row.get("question_text") or fallback_record.get("question_text"),
            "corpus": row.get("corpus") or fallback_record.get("corpus") or corpus,
            "attorney_category": row.get("attorney_category") or fallback_record.get("attorney_category"),
            "architecture_primary": row.get("architecture_primary") or fallback_record.get("architecture_primary"),
            "architecture_secondary": row.get("architecture_secondary") or fallback_record.get("architecture_secondary", []),
            "domain_primary": row.get("domain_primary") or fallback_record.get("domain_primary"),
            "domain_secondary": row.get("domain_secondary") or fallback_record.get("domain_secondary", []),
            "coverage_policy": row.get("coverage_policy") or fallback_record.get("coverage_policy", "optional"),
            "source_type": row.get("source_type") or fallback_record.get("source_type"),
            "suite_tags": row.get("suite_tags") or fallback_record.get("suite_tags", []),
            "bank_match": bank_record is not None,
        }
        if not merged.get("eval_split"):
            merged["eval_split"] = fallback_record.get("eval_split")
        rows.append(merged)

    return pd.DataFrame(rows)


def _domain_expected_cells(corpus: str, domain_primary: str) -> list[tuple[str, str]]:
    return list(DOMAIN_EXPECTED_CAPABILITY_CELLS.get(corpus, {}).get(domain_primary, []))


def build_capability_scorecard(
    per_question_scores: pd.DataFrame,
    *,
    corpus: str | None = None,
    coverage_targets: dict[str, int] | None = None,
    quality_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    coverage_targets = coverage_targets or COVERAGE_TARGETS
    quality_weights = quality_weights or QUALITY_SPLIT_WEIGHTS
    corpora = [corpus] if corpus else sorted(set(per_question_scores.get("corpus", pd.Series(dtype=str)).dropna()) or {"bible", "enron"})
    rows: list[dict[str, Any]] = []

    for corpus_name in corpora:
        corpus_frame = per_question_scores[per_question_scores["corpus"] == corpus_name].copy()
        required_cells = set(get_required_coverage_cells(corpus_name))
        observed_cells = {
            (row.attorney_category, row.architecture_primary)
            for row in corpus_frame[["attorney_category", "architecture_primary"]].itertuples(index=False)
            if row.attorney_category and row.architecture_primary
        }
        all_cells = sorted(observed_cells | required_cells)
        for attorney_category, architecture_primary in all_cells:
            cell_frame = corpus_frame[
                (corpus_frame["attorney_category"] == attorney_category)
                & (corpus_frame["architecture_primary"] == architecture_primary)
            ].copy()
            counts = {
                split: int((cell_frame.get("eval_split", pd.Series(dtype=str)) == split).sum())
                for split in SPLIT_BUCKETS
            }
            total = int(len(cell_frame))
            scored = cell_frame.dropna(subset=["avg_score"])
            split_scores = {
                split: _rounded(scored.loc[scored["eval_split"] == split, "avg_score"].mean())
                for split in ("test", "holdout")
            }
            quality_numerator = 0.0
            quality_denominator = 0.0
            for split, weight in quality_weights.items():
                split_score = split_scores.get(split)
                if split_score is not None:
                    quality_numerator += split_score * weight
                    quality_denominator += weight
            quality_score = quality_numerator / quality_denominator if quality_denominator else None
            coverage_components = [
                min(counts[split] / target, 1.0)
                for split, target in coverage_targets.items()
            ]
            coverage_score = sum(coverage_components) / len(coverage_components)
            weakest_questions = []
            if not scored.empty:
                weakest = scored.nsmallest(min(3, len(scored)), "avg_score")
                weakest_questions = weakest["question_text"].astype(str).tolist()
            rows.append(
                {
                    "corpus": corpus_name,
                    "attorney_category": attorney_category,
                    "architecture_primary": architecture_primary,
                    "policy": "required" if (attorney_category, architecture_primary) in required_cells else ("optional" if total else "n/a"),
                    "train": counts["train"],
                    "test": counts["test"],
                    "holdout": counts["holdout"],
                    "total": total,
                    "train_target": coverage_targets["train"],
                    "test_target": coverage_targets["test"],
                    "holdout_target": coverage_targets["holdout"],
                    "split_sufficiency": {
                        split: counts[split] >= target
                        for split, target in coverage_targets.items()
                    },
                    "insufficient_splits": [
                        split for split, target in coverage_targets.items()
                        if counts[split] < target
                    ],
                    "thin_splits": [
                        split for split, target in coverage_targets.items()
                        if counts[split] <= target
                    ],
                    "coverage_score": _rounded(coverage_score),
                    "quality_score": _rounded(quality_score),
                    "overall_score": _rounded(_as_float(scored["avg_score"].mean()) if not scored.empty else None),
                    "score_std": _rounded(_as_float(scored["avg_score"].std(ddof=0)) if len(scored) > 1 else 0.0 if len(scored) == 1 else None),
                    "scored_questions": int(len(scored)),
                    "weakest_questions": weakest_questions,
                }
            )

    return pd.DataFrame(rows).sort_values(
        ["corpus", "policy", "coverage_score", "quality_score", "attorney_category", "architecture_primary"],
        ascending=[True, True, True, True, True, True],
        na_position="last",
    )


def _split_balance_score(frame: pd.DataFrame) -> float:
    total = max(len(frame), 1)
    distance = 0.0
    for split, target_ratio in TARGET_SPLIT_DISTRIBUTION.items():
        observed_ratio = float((frame["eval_split"] == split).sum()) / total
        distance += abs(observed_ratio - target_ratio)
    return max(0.0, 1.0 - (distance / 2.0))


def build_domain_scorecard(
    per_question_scores: pd.DataFrame,
    *,
    corpus: str | None = None,
    quality_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    quality_weights = quality_weights or QUALITY_SPLIT_WEIGHTS
    corpora = [corpus] if corpus else sorted(set(per_question_scores.get("corpus", pd.Series(dtype=str)).dropna()) or {"bible", "enron"})
    rows: list[dict[str, Any]] = []

    for corpus_name in corpora:
        corpus_frame = per_question_scores[per_question_scores["corpus"] == corpus_name].copy()
        for domain_primary in get_domain_categories(corpus_name):
            domain_frame = corpus_frame[corpus_frame["domain_primary"] == domain_primary].copy()
            expected_cells = _domain_expected_cells(corpus_name, domain_primary)
            observed_cells = {
                (row.attorney_category, row.architecture_primary)
                for row in domain_frame[["attorney_category", "architecture_primary"]].itertuples(index=False)
                if row.attorney_category and row.architecture_primary
            }
            covered_expected = [cell for cell in expected_cells if cell in observed_cells]
            breadth_score = len(covered_expected) / max(len(expected_cells), 1)
            split_balance = _split_balance_score(domain_frame) if not domain_frame.empty else 0.0
            scored = domain_frame.dropna(subset=["avg_score"])
            quality_numerator = 0.0
            quality_denominator = 0.0
            for split, weight in quality_weights.items():
                split_scores = scored.loc[scored["eval_split"] == split, "avg_score"]
                if not split_scores.empty:
                    quality_numerator += float(split_scores.mean()) * weight
                    quality_denominator += weight
            quality_score = quality_numerator / quality_denominator if quality_denominator else None
            domain_coverage_score = _geometric_mean([breadth_score, split_balance, quality_score])
            rows.append(
                {
                    "corpus": corpus_name,
                    "domain_primary": domain_primary,
                    "train": int((domain_frame.get("eval_split", pd.Series(dtype=str)) == "train").sum()),
                    "test": int((domain_frame.get("eval_split", pd.Series(dtype=str)) == "test").sum()),
                    "holdout": int((domain_frame.get("eval_split", pd.Series(dtype=str)) == "holdout").sum()),
                    "total": int(len(domain_frame)),
                    "expected_capability_cells": expected_cells,
                    "covered_capability_cells": covered_expected,
                    "missing_capability_cells": [cell for cell in expected_cells if cell not in observed_cells],
                    "domain_breadth_score": _rounded(breadth_score),
                    "split_balance_score": _rounded(split_balance),
                    "quality_score": _rounded(quality_score),
                    "domain_coverage_score": _rounded(domain_coverage_score),
                }
            )
    return pd.DataFrame(rows).sort_values(
        ["corpus", "domain_coverage_score", "domain_primary"],
        ascending=[True, True, True],
        na_position="last",
    )


def load_data_confidence_overlay(corpus: str) -> dict[str, Any]:
    if corpus != "enron":
        return {
            "corpus": corpus,
            "status": "unavailable",
            "reason": "No corpus_coverage/data_quality_report overlay is implemented for this corpus yet.",
        }

    try:
        from src.agent.agent_serving import CATALOG, ENRON_SCHEMA, LocalBackend, _get_backend

        coverage_query = (
            f"SELECT metric_name, metric_value, denominator, coverage_pct"
            f" FROM {CATALOG}.{ENRON_SCHEMA}.corpus_coverage"
        )
        quality_query = (
            f"SELECT table_name, SUM(null_count) AS total_nulls, AVG(null_rate) AS avg_null_rate"
            f" FROM {CATALOG}.{ENRON_SCHEMA}.data_quality_report"
            f" GROUP BY table_name"
            f" ORDER BY avg_null_rate DESC LIMIT 5"
        )

        coverage_rows = []
        quality_rows = []
        local_candidates = [
            Path("data/graphrag_enron.duckdb"),
            Path("data/graphrag.duckdb"),
        ]
        for candidate in local_candidates:
            if not candidate.exists():
                continue
            backend = LocalBackend(str(candidate))
            coverage_rows = backend.execute_sql(coverage_query)
            quality_rows = backend.execute_sql(quality_query)
            if coverage_rows or quality_rows:
                break

        if not coverage_rows and not quality_rows:
            backend = _get_backend()
            coverage_rows = backend.execute_sql(coverage_query)
            quality_rows = backend.execute_sql(quality_query)
    except Exception as exc:
        return {
            "corpus": corpus,
            "status": "unavailable",
            "reason": str(exc),
        }

    coverage_values = [
        max(0.0, min(_as_float(row.get("coverage_pct")) or 0.0, 100.0))
        for row in coverage_rows or []
        if _as_float(row.get("coverage_pct")) is not None
    ]
    avg_coverage = sum(coverage_values) / len(coverage_values) if coverage_values else None
    null_rates = [
        _as_float(row.get("avg_null_rate"))
        for row in quality_rows or []
        if _as_float(row.get("avg_null_rate")) is not None
    ]
    worst_null_rate = max(null_rates) if null_rates else None
    score_components = []
    if avg_coverage is not None:
        score_components.append(max(0.0, min(avg_coverage / 100.0, 1.0)))
    if worst_null_rate is not None:
        score_components.append(max(0.0, 1.0 - min(worst_null_rate, 1.0)))
    return {
        "corpus": corpus,
        "status": "available",
        "score": _rounded(sum(score_components) / len(score_components) if score_components else None),
        "avg_coverage_pct": _rounded(avg_coverage),
        "worst_null_rate": _rounded(worst_null_rate),
        "coverage_warnings": [
            row for row in (coverage_rows or [])
            if (_as_float(row.get("coverage_pct")) or 100.0) < 80.0
        ],
        "data_quality_caveats": [
            row for row in (quality_rows or [])
            if (_as_float(row.get("avg_null_rate")) or 0.0) > 0.10
        ],
    }


def _gap_prompt(
    *,
    corpus: str,
    domain_primary: str,
    attorney_category: str,
    architecture_primary: str,
    gap_type: str,
    weak_examples: list[str],
) -> str:
    examples = "\n".join(f"- {question}" for question in weak_examples[:3]) or "- none available"
    return (
        f"You are generating governed evaluation questions for the {corpus} corpus.\n"
        f"Target domain: {domain_primary}\n"
        f"Target attorney perspective: {attorney_category}\n"
        f"Target architecture capability: {architecture_primary}\n"
        f"Gap type: {gap_type}\n"
        "Use AVL-style escalation: start from observed weak or missing coverage, then generate 3-5 harder"
        " questions that force grounded, evidence-backed answers. Promotion into the canonical bank remains manual.\n"
        f"Weak examples to escalate from:\n{examples}"
    )


def build_gap_backlog(
    per_question_scores: pd.DataFrame,
    *,
    corpus: str | None = None,
    coverage_targets: dict[str, int] | None = None,
    low_quality_threshold: float = LOW_QUALITY_THRESHOLD,
    high_variance_std: float = HIGH_VARIANCE_STD,
) -> list[dict[str, Any]]:
    coverage_targets = coverage_targets or COVERAGE_TARGETS
    corpora = [corpus] if corpus else sorted(set(per_question_scores.get("corpus", pd.Series(dtype=str)).dropna()) or {"bible", "enron"})
    backlog: list[dict[str, Any]] = []

    for corpus_name in corpora:
        corpus_frame = per_question_scores[per_question_scores["corpus"] == corpus_name].copy()
        for domain_primary in get_domain_categories(corpus_name):
            domain_frame = corpus_frame[corpus_frame["domain_primary"] == domain_primary].copy()
            for attorney_category, architecture_primary in _domain_expected_cells(corpus_name, domain_primary):
                cell_frame = domain_frame[
                    (domain_frame["attorney_category"] == attorney_category)
                    & (domain_frame["architecture_primary"] == architecture_primary)
                ].copy()
                counts = {
                    split: int((cell_frame.get("eval_split", pd.Series(dtype=str)) == split).sum())
                    for split in SPLIT_BUCKETS
                }
                scored = cell_frame.dropna(subset=["avg_score"])
                weak_examples = []
                if not scored.empty:
                    weak_examples = scored.nsmallest(min(3, len(scored)), "avg_score")["question_text"].astype(str).tolist()

                def add_gap(gap_type: str, severity: str, detail: str) -> None:
                    backlog.append(
                        {
                            "corpus": corpus_name,
                            "domain_primary": domain_primary,
                            "attorney_category": attorney_category,
                            "architecture_primary": architecture_primary,
                            "gap_type": gap_type,
                            "severity": severity,
                            "detail": detail,
                            "current_counts": counts,
                            "candidate_question_prompt": _gap_prompt(
                                corpus=corpus_name,
                                domain_primary=domain_primary,
                                attorney_category=attorney_category,
                                architecture_primary=architecture_primary,
                                gap_type=gap_type,
                                weak_examples=weak_examples,
                            ),
                            "weak_examples": weak_examples,
                        }
                    )

                if cell_frame.empty:
                    add_gap("no_questions", "high", "No governed questions exist for this corpus/domain/capability slice.")
                    continue
                for split, target in coverage_targets.items():
                    if counts[split] < target:
                        severity = "high" if split == "holdout" else "medium"
                        add_gap(f"missing_{split}", severity, f"{split} coverage is {counts[split]} but target is {target}.")
                if not scored.empty:
                    mean_quality = float(scored["avg_score"].mean())
                    if mean_quality < low_quality_threshold:
                        add_gap("low_quality", "high", f"Average scored quality is {mean_quality:.3f}, below the {low_quality_threshold:.2f} threshold.")
                    if len(scored) >= 3:
                        std = float(scored["avg_score"].std(ddof=0))
                        if std > high_variance_std:
                            add_gap("high_variance", "medium", f"Score standard deviation is {std:.3f}, above the {high_variance_std:.2f} threshold.")

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    backlog.sort(key=lambda item: (severity_rank.get(item["severity"], 99), item["corpus"], item["domain_primary"], item["attorney_category"], item["architecture_primary"], item["gap_type"]))
    return backlog


def build_summary(
    capability_scorecard: pd.DataFrame,
    domain_scorecard: pd.DataFrame,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    corpora = sorted(set(capability_scorecard["corpus"]).union(set(domain_scorecard["corpus"])))
    for corpus_name in corpora:
        capability_rows = capability_scorecard[
            (capability_scorecard["corpus"] == corpus_name)
            & (capability_scorecard["policy"] == "required")
        ]
        domain_rows = domain_scorecard[domain_scorecard["corpus"] == corpus_name]
        capability_coverage = _as_float(capability_rows["coverage_score"].mean()) if not capability_rows.empty else None
        capability_quality = _as_float(capability_rows["quality_score"].dropna().mean()) if capability_rows["quality_score"].notna().any() else None
        domain_coverage = _as_float(domain_rows["domain_coverage_score"].dropna().mean()) if domain_rows["domain_coverage_score"].notna().any() else None
        readiness = (
            _geometric_mean([capability_coverage, capability_quality, domain_coverage])
            if None not in {capability_coverage, capability_quality, domain_coverage}
            else None
        )
        weakest_cells = capability_rows.nsmallest(
            min(5, len(capability_rows)),
            ["coverage_score", "quality_score"],
        )[["attorney_category", "architecture_primary", "coverage_score", "quality_score"]]
        weakest_domains = domain_rows.nsmallest(
            min(5, len(domain_rows)),
            "domain_coverage_score",
        )[["domain_primary", "domain_coverage_score", "quality_score"]]
        rows.append(
            {
                "corpus": corpus_name,
                "capability_coverage": _rounded(capability_coverage),
                "capability_quality": _rounded(capability_quality),
                "domain_coverage": _rounded(domain_coverage),
                "readiness": _rounded(readiness),
                "required_cells": int(len(capability_rows)),
                "thin_required_cells": int(capability_rows["thin_splits"].apply(bool).sum()) if not capability_rows.empty else 0,
                "weakest_capability_cells": _records(weakest_cells),
                "weakest_domains": _records(weakest_domains),
            }
        )
    return rows


def build_score_report(
    *,
    eval_results: pd.DataFrame | None = None,
    corpus: str | None = None,
    include_interaction_types: Iterable[str] | None = None,
) -> dict[str, Any]:
    if eval_results is None:
        per_question_scores = question_inventory_frame(
            corpus=corpus,
            include_interaction_types=include_interaction_types,
        )
    else:
        per_question_scores = enrich_eval_results(eval_results, corpus=corpus)

    capability_scorecard = build_capability_scorecard(per_question_scores, corpus=corpus)
    domain_scorecard = build_domain_scorecard(per_question_scores, corpus=corpus)
    corpora = sorted(set(per_question_scores["corpus"].dropna())) if not per_question_scores.empty else ([corpus] if corpus else ["bible", "enron"])
    report = {
        "coverage_targets": COVERAGE_TARGETS,
        "quality_weights": QUALITY_SPLIT_WEIGHTS,
        "summary": build_summary(capability_scorecard, domain_scorecard),
        "per_question_scores": _records(per_question_scores),
        "capability_scorecard": _records(capability_scorecard),
        "domain_scorecard": _records(domain_scorecard),
        "gap_backlog": build_gap_backlog(per_question_scores, corpus=corpus),
        "data_confidence_overlay": {
            corpus_name: load_data_confidence_overlay(corpus_name)
            for corpus_name in corpora
        },
    }
    return report
