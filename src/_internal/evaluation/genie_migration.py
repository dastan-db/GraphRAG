from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Callable

BASELINE_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "baselines"
DEFAULT_BASELINE_JSON_PATH = BASELINE_DIR / "genie_iteration0_baseline.json"
DEFAULT_BASELINE_MD_PATH = BASELINE_DIR / "genie_iteration0_baseline.md"
DEFAULT_RUNTIME_BASELINE_PATH = BASELINE_DIR / "runtime_eval_baseline.json"
DEFAULT_LOCAL_DB_PATH = Path("data/graphrag_enron.duckdb")

GENIE_ITERATION0_QUESTION_IDS = (
    "enron-core-5675612b20",
    "enron-core-b875644912",
    "enron-curated-pacheco-lay-summary-count",
    "enron-curated-pacheco-lay-june19-dyad-count",
    "enron-curated-pacheco-lay-june26-dyad-count",
    "enron-curated-pacheco-lay-december-dyad-comparison",
)

WAVE1_GENIE_TOOLS = (
    "get_top_email_pairs",
    "get_top_individuals",
    "detect_self_emails",
)

HYBRID_ANALYTICS_TOOLS = (
    "query_and_enrich",
    "find_top_contacts",
    "get_dyad_topics",
    "get_external_contacts",
    "get_communication_stats",
    "get_topic_distribution",
    "get_communication_timeline",
    "find_emails",
    "get_emails_between",
    "browse_topics",
)

WAVE1_ROLLBACK_TRIGGERS = (
    "unexplained_count_drift_vs_duckdb_or_lakebase",
    "two_consecutive_red_scorecards_on_quantitative_slice",
    "p95_latency_regression_gt_20pct_without_quality_gain",
)

HYBRID_FALLBACK_ORDER = (
    "local_entity_resolution",
    "local_duckdb_or_lakebase_sql",
    "genie_or_databricks_sql_semantic_layer",
    "duckdb_or_lakebase_evidence_enrichment",
    "deterministic_abstention_on_missing_or_conflicting_evidence",
)

ADR_LOG = (
    {
        "id": "ADR-01",
        "title": "Keep governed question bank as the single evaluation SSOT",
        "status": "adopted",
        "context": "The repo already centralizes question curation and governed exports in src/evaluation/question_bank.py.",
        "options_considered": [
            "Create a separate Genie-only benchmark store",
            "Extend the governed question bank with Genie benchmark metadata",
        ],
        "decision": "Extend the governed question bank and freeze a derived Genie benchmark subset from it.",
        "rationale": "Avoids dataset drift and preserves one promotion path across regression, AVL, benchmark, and holdout layers.",
        "trade_offs": [
            "Question metadata grows richer",
            "Some benchmark-specific fields live outside the core bank as derived artifacts",
        ],
        "risks": [
            "Metadata sprawl",
            "Snapshot drift between reference answers and current local exports",
        ],
        "mitigations": [
            "Keep the benchmark as a generated derivative",
            "Track question-bank vs snapshot alignment explicitly",
        ],
        "validation_metrics": [
            "coverage_cell_completeness",
            "promotion_latency",
            "regression_stability",
        ],
        "rollback_conditions": [
            "The governed bank cannot express benchmark metadata without repeated manual duplication",
        ],
        "superseding_evidence_trigger": "Repeated evidence that a separate benchmark registry is materially simpler without introducing drift.",
    },
    {
        "id": "ADR-02",
        "title": "Migrate only the analytics_sql_genie slice first",
        "status": "adopted",
        "context": "Genie is strongest for benchmarkable analytical SQL and weakest for graph, evidence, and vector-heavy flows.",
        "options_considered": [
            "Broad Genie migration across graph and evidence tools",
            "No Genie migration",
            "Narrow quantitative migration first",
        ],
        "decision": "Advance the governed quantitative slice first, then require benchmark evidence before expanding.",
        "rationale": "This is the smallest valuable, reversible migration boundary already represented in the question bank.",
        "trade_offs": [
            "The architecture stays hybrid longer",
            "Some analytics remain on DuckDB/Lakebase while Genie matures",
        ],
        "risks": [
            "Partial complexity",
            "Benchmark vanity wins without broader trust improvements",
        ],
        "mitigations": [
            "Layered evaluation gates",
            "Holdout protection",
            "Rollback thresholds",
        ],
        "validation_metrics": [
            "sql_correctness",
            "benchmark_pass_rate",
            "canonical_regression_delta",
        ],
        "rollback_conditions": [
            "Quantitative slice fails to improve or preserve trust metrics",
        ],
        "superseding_evidence_trigger": "A shadow benchmark shows another tool family clearly outperforms the quantitative slice as the first migration target.",
    },
    {
        "id": "ADR-03",
        "title": "Use hybrid wrappers for entity-resolved analytics",
        "status": "adopted",
        "context": "Several analytical tools are SQL-native but depend on entity resolution, directional semantics, and evidence packaging.",
        "options_considered": [
            "Pure Genie rewrite",
            "Keep the full path on DuckDB/Lakebase",
            "Use a hybrid wrapper with local resolution and governed SQL execution",
        ],
        "decision": "Keep entity resolution and evidence packaging outside Genie while letting Genie or Databricks SQL handle the analytical core.",
        "rationale": "Separates deterministic correctness work from SQL natural-language generation.",
        "trade_offs": [
            "One extra orchestration layer",
            "Wrapper behavior must stay stable across backends",
        ],
        "risks": [
            "Wrapper sprawl",
            "Latency creep",
        ],
        "mitigations": [
            "Standardized wrapper contract",
            "Exact benchmark rows with failure-mode tags",
        ],
        "validation_metrics": [
            "wrong_entity_rate",
            "tool_selection_correctness",
            "latency_p95",
        ],
        "rollback_conditions": [
            "Wrapper complexity grows without measurable benchmark lift",
        ],
        "superseding_evidence_trigger": "Genie reliably absorbs entity resolution without regressions on benchmark or adversarial slices.",
    },
    {
        "id": "ADR-04",
        "title": "Preserve DuckDB as local-fast reference and Lakebase as governed remote reference",
        "status": "adopted",
        "context": "The project already depends on local-fast iteration and governed production-style SQL access.",
        "options_considered": [
            "Genie-first development loop",
            "DuckDB and Lakebase as reference layers",
        ],
        "decision": "Use local DuckDB for benchmark authoring and Lakebase/Databricks SQL for governed parity and remote validation.",
        "rationale": "Maintains fast iteration while keeping production-style SQL as a stable reference.",
        "trade_offs": [
            "Multiple backends stay active",
            "Parity work remains necessary",
        ],
        "risks": [
            "Snapshot drift",
            "Default-env ambiguity",
        ],
        "mitigations": [
            "Explicit backend manifests",
            "Pinned envs for every benchmark run",
        ],
        "validation_metrics": [
            "duckdb_lakebase_parity",
            "local_iteration_latency",
            "benchmark_reproducibility",
        ],
        "rollback_conditions": [
            "Parity becomes operationally unmanageable",
        ],
        "superseding_evidence_trigger": "A unified local semantic-layer emulator proves reliable enough to replace the current dual-reference model.",
    },
    {
        "id": "ADR-05",
        "title": "Keep graph, vector, evidence, and provenance tools off Genie until evidence says otherwise",
        "status": "adopted",
        "context": "Current graph, evidence, and provenance tools are specialized and governed by deterministic retrieval quality.",
        "options_considered": [
            "Expand Genie into graph and evidence lanes now",
            "Keep specialized lanes separate",
        ],
        "decision": "Keep those lanes on DuckDB/Lakebase or their specialized backends until a dedicated pilot proves a win.",
        "rationale": "Protects graph semantics and evidence-grounding quality.",
        "trade_offs": [
            "The target state is intentionally not fully uniform",
        ],
        "risks": [
            "Architecture may look less pure",
        ],
        "mitigations": [
            "Document clear layer boundaries",
            "Use targeted scorecards per lane",
        ],
        "validation_metrics": [
            "path_fidelity",
            "retrieval_grounding",
            "provenance_honesty",
        ],
        "rollback_conditions": [
            "Not applicable until a dedicated pilot exists",
        ],
        "superseding_evidence_trigger": "A controlled shadow benchmark shows a clear gain with zero trust loss.",
    },
    {
        "id": "ADR-06",
        "title": "Certify every migration with layered evaluation and holdout gates",
        "status": "adopted",
        "context": "The repo already has regression, adversarial, SQL-first, and improvement-loop assets.",
        "options_considered": [
            "Benchmark-only gating",
            "Layered gating across regression, adversarial, Genie benchmark, and holdout",
        ],
        "decision": "Use all four evaluation layers for promotion, pause, and rollback decisions.",
        "rationale": "Prevents benchmark overfitting and protects non-Genie behavior.",
        "trade_offs": [
            "More evaluation work per iteration",
        ],
        "risks": [
            "Longer promotion cycles",
        ],
        "mitigations": [
            "Start with a small governed Genie subset",
            "Automate artifact generation",
        ],
        "validation_metrics": [
            "green_scorecard_across_all_layers",
        ],
        "rollback_conditions": [
            "Holdout or AVL turns red while benchmark stays green",
        ],
        "superseding_evidence_trigger": "None expected.",
    },
    {
        "id": "ADR-07",
        "title": "Normalize backend defaults before comparing architectures",
        "status": "adopted",
        "context": "Runtime config defaults to local while agent_serving defaults to lakebase.",
        "options_considered": [
            "Ignore the mismatch",
            "Document and pin benchmark envs",
            "Unify defaults immediately before baselineing",
        ],
        "decision": "Record the mismatch in the SSOT manifest and pin benchmark environments explicitly in every run.",
        "rationale": "Avoids false wins or losses caused by implicit backend selection.",
        "trade_offs": [
            "Slightly more setup per run",
        ],
        "risks": [
            "Silent drift if env pinning is skipped",
        ],
        "mitigations": [
            "Freeze the backend manifest",
            "Make env pinning part of the baseline artifact",
        ],
        "validation_metrics": [
            "benchmark_reproducibility",
            "backend_manifest_consistency",
        ],
        "rollback_conditions": [
            "Not applicable until defaults are unified in code",
        ],
        "superseding_evidence_trigger": "The backend defaults are unified across runtime and agent surfaces.",
    },
)

ITERATION_SCORECARD_METRICS = (
    {
        "name": "answer_accuracy",
        "baseline": "iteration0_measurement_pending",
        "target": "+3pp overall or +5pp on the targeted slice with no non-target regression >1pp",
        "measurement_method": "MLflow correctness-family scorer means on the promoted slice and canonical regression set",
        "data_source": "mlflow.genai.evaluate results for canonical regression and benchmark slices",
        "green": "meets target",
        "yellow": "within 1pp of baseline",
        "red": "below baseline or cross-slice regression >1pp",
    },
    {
        "name": "evidence_grounding",
        "baseline": "iteration0_measurement_pending",
        "target": "+5pp on migrated slice with zero fabricated evidence",
        "measurement_method": "grounding, citation, and provenance scorer means",
        "data_source": "enron evaluation scorers and factual quality metrics",
        "green": "target met with zero critical fabrications",
        "yellow": "flat to +4pp",
        "red": "any trust regression or evidence fabrication",
    },
    {
        "name": "tool_selection_correctness",
        "baseline": "iteration0_measurement_pending",
        "target": ">=0.95 on benchmark and +5pp on migrated routes",
        "measurement_method": "actual tool calls vs expected_tools",
        "data_source": "runtime traces and governed question metadata",
        "green": ">=0.95",
        "yellow": "0.90-0.949",
        "red": "<0.90",
    },
    {
        "name": "sql_correctness",
        "baseline": "measured_on_local_query_and_enrich",
        "target": ">=0.95 for Genie-native tools and >=0.90 for hybrid tools",
        "measurement_method": "exact result or tolerance match against local DuckDB benchmark goldens",
        "data_source": "generated Genie benchmark bundle",
        "green": "meets target",
        "yellow": "0.90-0.949 for Genie-native tools",
        "red": "<0.90 for Genie-native tools",
    },
    {
        "name": "benchmark_pass_rate",
        "baseline": "measured_on_local_query_and_enrich",
        "target": ">=0.90",
        "measurement_method": "fraction of benchmark questions passing route/result checks",
        "data_source": "generated Genie benchmark bundle",
        "green": ">=0.90",
        "yellow": "0.80-0.899",
        "red": "<0.80",
    },
    {
        "name": "latency",
        "baseline": "measured_on_local_query_and_enrich",
        "target": "p95 not worse than baseline for pure Genie and not worse than +10-15% for hybrid",
        "measurement_method": "wall-clock latency per benchmark case",
        "data_source": "generated Genie benchmark bundle",
        "green": "within target",
        "yellow": "+10-20%",
        "red": ">20%",
    },
    {
        "name": "failure_rate",
        "baseline": "measured_on_local_query_and_enrich",
        "target": "<0.02 and no increase vs baseline",
        "measurement_method": "failed or empty responses divided by total benchmark questions",
        "data_source": "generated Genie benchmark bundle",
        "green": "<0.02",
        "yellow": "0.02-0.05",
        "red": ">0.05",
    },
    {
        "name": "cost",
        "baseline": "iteration0_measurement_pending",
        "target": "<=+10% unless quality improves materially",
        "measurement_method": "token and SQL execution cost per question",
        "data_source": "MLflow traces and Databricks query metrics",
        "green": "<=+10%",
        "yellow": "+10-20%",
        "red": ">20% without quality gain",
    },
    {
        "name": "maintainability",
        "baseline": "iteration0_snapshot",
        "target": "20% reduction in duplicate analytics logic and smaller change surface",
        "measurement_method": "code inventory and files touched per analytics change",
        "data_source": "repo source and review stats",
        "green": "improved",
        "yellow": "flat",
        "red": "worse by >10%",
    },
    {
        "name": "developer_effort",
        "baseline": "iteration0_measurement_pending",
        "target": "25% reduction in median time to passing validation",
        "measurement_method": "cycle time and rework count",
        "data_source": "session logs and benchmark history",
        "green": "target met",
        "yellow": "flat",
        "red": "slower by >10%",
    },
    {
        "name": "governance_trust_fit",
        "baseline": "iteration0_measurement_pending",
        "target": "no regression and zero policy leaks",
        "measurement_method": "weighted trust composite over grounding, citation, provenance, and access control",
        "data_source": "enron evaluation, bible governance, and ABAC slices",
        "green": "flat or better with zero critical violations",
        "yellow": "minor non-critical dip <=1pp",
        "red": "any policy leak or trust regression >1pp",
    },
    {
        "name": "adversarial_robustness",
        "baseline": "iteration0_measurement_pending",
        "target": "+5pp on escalated slices and zero critical fabrication failures",
        "measurement_method": "adversarial scorer means and critical fail counts",
        "data_source": "adversarial evaluation pipeline",
        "green": "target met",
        "yellow": "partial improvement",
        "red": "any critical fail or no improvement over two iterations",
    },
)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    position = max(0.0, min(100.0, percentile)) / 100.0 * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    ordered = sorted(float(value) for value in values)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _load_question_bank_rows() -> list[dict[str, Any]]:
    from src.evaluation.question_bank import export_governed_flat_questions

    return export_governed_flat_questions(corpus="enron")


def load_governed_genie_rows() -> list[dict[str, Any]]:
    rows_by_id = {row["question_id"]: row for row in _load_question_bank_rows()}
    return [rows_by_id[qid] for qid in GENIE_ITERATION0_QUESTION_IDS if qid in rows_by_id]


def _load_scorer_names() -> list[str]:
    from src.evaluation.enron_evaluation import ENRON_SCORERS

    names: list[str] = []
    for scorer in ENRON_SCORERS:
        names.append(getattr(scorer, "name", getattr(scorer, "__name__", str(scorer))))
    return names


def _load_runtime_baseline_registry() -> dict[str, Any]:
    from src._internal.evaluation.runtime_baselines import load_runtime_baselines

    return load_runtime_baselines()


def _resolve_local_db_path(db_path: str | Path | None = None) -> Path:
    raw = (
        str(db_path)
        if db_path is not None
        else os.environ.get("GRAPHRAG_ENRON_LOCAL_DB")
        or os.environ.get("GRAPHRAG_LOCAL_DB")
        or str(DEFAULT_LOCAL_DB_PATH)
    )
    return Path(raw)


@contextlib.contextmanager
def _open_duckdb(db_path: str | Path | None = None):
    import duckdb

    resolved_path = _resolve_local_db_path(db_path)
    conn = duckdb.connect(str(resolved_path), read_only=True)
    try:
        yield conn, resolved_path
    finally:
        conn.close()


def _fetch_rows(
    conn: Any,
    sql: str,
) -> list[dict[str, Any]]:
    frame = conn.execute(sql).fetchdf()
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _pair_summary_sql() -> str:
    return """
SELECT
  'leonardo.pacheco@enron.com' AS entity_a,
  'kenneth.lay@enron.com' AS entity_b,
  SUM(CASE
        WHEN LOWER(person_a) = 'leonardo.pacheco@enron.com'
         AND LOWER(person_b) = 'kenneth.lay@enron.com'
        THEN total_count ELSE 0 END) AS sent_a_to_b,
  SUM(CASE
        WHEN LOWER(person_a) = 'kenneth.lay@enron.com'
         AND LOWER(person_b) = 'leonardo.pacheco@enron.com'
        THEN total_count ELSE 0 END) AS sent_b_to_a,
  SUM(total_count) AS total_emails
FROM communication_dyads
WHERE (LOWER(person_a) = 'leonardo.pacheco@enron.com'
   AND LOWER(person_b) = 'kenneth.lay@enron.com')
   OR (LOWER(person_a) = 'kenneth.lay@enron.com'
   AND LOWER(person_b) = 'leonardo.pacheco@enron.com')
""".strip()


def _top_contact_sql() -> str:
    return """
SELECT
  contact_email,
  SUM(sent_to_contact) AS sent_to_contact,
  SUM(received_from_contact) AS received_from_contact,
  SUM(total_emails) AS total_emails
FROM (
  SELECT
    d.person_b AS contact_email,
    SUM(d.total_count) AS sent_to_contact,
    0 AS received_from_contact,
    SUM(d.total_count) AS total_emails
  FROM communication_dyads d
  WHERE LOWER(d.person_a) = 'kenneth.lay@enron.com'
  GROUP BY d.person_b

  UNION ALL

  SELECT
    d.person_a AS contact_email,
    0 AS sent_to_contact,
    SUM(d.total_count) AS received_from_contact,
    SUM(d.total_count) AS total_emails
  FROM communication_dyads d
  WHERE LOWER(d.person_b) = 'kenneth.lay@enron.com'
  GROUP BY d.person_a
) combined
GROUP BY contact_email
ORDER BY total_emails DESC, contact_email ASC
LIMIT 1
""".strip()


def _weekly_pair_sql(period: str) -> str:
    return f"""
SELECT
  CAST(period AS DATE) AS period,
  total_count,
  to_count,
  cc_count,
  bcc_count
FROM communication_dyads
WHERE LOWER(person_a) = 'leonardo.pacheco@enron.com'
  AND LOWER(person_b) = 'kenneth.lay@enron.com'
  AND CAST(period AS DATE) = DATE '{period}'
ORDER BY period
""".strip()


def _period_comparison_sql(start_period: str, end_period: str) -> str:
    return f"""
WITH weekly_counts AS (
  SELECT
    CAST(period AS DATE) AS period,
    total_count,
    to_count,
    cc_count,
    bcc_count
  FROM communication_dyads
  WHERE LOWER(person_a) = 'leonardo.pacheco@enron.com'
    AND LOWER(person_b) = 'kenneth.lay@enron.com'
    AND CAST(period AS DATE) IN (DATE '{start_period}', DATE '{end_period}')
),
comparison AS (
  SELECT
    MAX(CASE WHEN period = DATE '{start_period}' THEN total_count END) AS start_count,
    MAX(CASE WHEN period = DATE '{end_period}' THEN total_count END) AS end_count
  FROM weekly_counts
)
SELECT
  weekly_counts.period,
  weekly_counts.total_count,
  weekly_counts.to_count,
  weekly_counts.cc_count,
  weekly_counts.bcc_count,
  comparison.end_count - comparison.start_count AS delta_total_count
FROM weekly_counts
CROSS JOIN comparison
ORDER BY weekly_counts.period
""".strip()


def _exchange_type(row: dict[str, Any]) -> str:
    sent = int(row.get("sent_to_contact") or 0)
    received = int(row.get("received_from_contact") or 0)
    if sent > 0 and received > 0:
        return "bidirectional"
    if sent > 0:
        return "outbound_only"
    return "inbound_only"


CASE_DEFINITIONS = {
    "enron-core-5675612b20": {
        "classification": "both",
        "benchmark_mode": "exact_pair_total_and_direction",
        "target_lane": "genie_native_analytics",
        "canonical_sql": _pair_summary_sql,
        "gold_builder": lambda conn: _fetch_rows(conn, _pair_summary_sql()),
        "comparison": "pair_summary",
        "required_instructions": [
            "Honor directionality and avoid saying 'exchanged' when all traffic is one-way.",
            "Use communication_dyads as the benchmark reference for pair totals.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["SUM(CASE WHEN ... THEN total_count ELSE 0 END)"],
            "functions": [],
        },
        "likely_failure_modes": [
            "one_way_vs_bidirectional_mislabel",
            "snapshot_drift_vs_question_bank_reference",
            "entity_alias_resolution_failure",
        ],
        "benchmark_layer": "genie_benchmark",
        "reusable_in_avl": True,
    },
    "enron-core-b875644912": {
        "classification": "both",
        "benchmark_mode": "top_contact_exact",
        "target_lane": "genie_native_analytics",
        "canonical_sql": _top_contact_sql,
        "gold_builder": lambda conn: [
            {**row, "exchange_type": _exchange_type(row)}
            for row in _fetch_rows(conn, _top_contact_sql())
        ],
        "comparison": "top_contact",
        "required_instructions": [
            "Rank by total_emails and preserve inbound/outbound directionality in the answer.",
            "Treat this as a ranking query over communication_dyads rather than evidence search.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["UNION ALL", "SUM(total_count)", "ORDER BY total_emails DESC"],
            "functions": [],
        },
        "likely_failure_modes": [
            "top_contact_snapshot_drift",
            "directionality_dropped_from_ranking",
            "wrong_rank_ordering",
        ],
        "benchmark_layer": "genie_benchmark",
        "reusable_in_avl": True,
    },
    "enron-curated-pacheco-lay-summary-count": {
        "classification": "holdout_only",
        "benchmark_mode": "exact_pair_total_and_direction",
        "target_lane": "genie_benchmark_holdout",
        "canonical_sql": _pair_summary_sql,
        "gold_builder": lambda conn: _fetch_rows(conn, _pair_summary_sql()),
        "comparison": "pair_summary",
        "required_instructions": [
            "Return the total and the traffic direction from the current local export.",
            "Do not collapse one-way reporting traffic into balanced exchange language.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["SUM(CASE WHEN ... THEN total_count ELSE 0 END)"],
            "functions": [],
        },
        "likely_failure_modes": [
            "holdout_overfit_to_prior_snapshot",
            "one_way_vs_bidirectional_mislabel",
            "question_bank_snapshot_alignment_regression",
        ],
        "benchmark_layer": "holdout_certification",
        "reusable_in_avl": False,
    },
    "enron-curated-pacheco-lay-june19-dyad-count": {
        "classification": "genie_benchmark_only",
        "benchmark_mode": "weekly_exact",
        "target_lane": "genie_native_analytics",
        "canonical_sql": lambda: _weekly_pair_sql("2000-06-19"),
        "gold_builder": lambda conn: _fetch_rows(conn, _weekly_pair_sql("2000-06-19")),
        "comparison": "weekly_exact",
        "required_instructions": [
            "Apply the explicit weekly date filter before aggregating the dyad row.",
            "Preserve to/cc/bcc breakdown in the result set.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["CAST(period AS DATE)", "exact period filter"],
            "functions": [],
        },
        "likely_failure_modes": [
            "date_window_ignored",
            "falls_back_to_all_time_pair_total",
            "weekly_granularity_missing",
        ],
        "benchmark_layer": "genie_benchmark",
        "reusable_in_avl": False,
    },
    "enron-curated-pacheco-lay-june26-dyad-count": {
        "classification": "genie_benchmark_only",
        "benchmark_mode": "weekly_exact",
        "target_lane": "genie_native_analytics",
        "canonical_sql": lambda: _weekly_pair_sql("2000-06-26"),
        "gold_builder": lambda conn: _fetch_rows(conn, _weekly_pair_sql("2000-06-26")),
        "comparison": "weekly_exact",
        "required_instructions": [
            "Apply the explicit weekly date filter before aggregating the dyad row.",
            "Preserve to/cc/bcc breakdown in the result set.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["CAST(period AS DATE)", "exact period filter"],
            "functions": [],
        },
        "likely_failure_modes": [
            "date_window_ignored",
            "falls_back_to_all_time_pair_total",
            "weekly_granularity_missing",
        ],
        "benchmark_layer": "genie_benchmark",
        "reusable_in_avl": False,
    },
    "enron-curated-pacheco-lay-december-dyad-comparison": {
        "classification": "both",
        "benchmark_mode": "period_comparison_exact",
        "target_lane": "hybrid_candidate",
        "canonical_sql": lambda: _period_comparison_sql("2000-12-04", "2000-12-11"),
        "gold_builder": lambda conn: _fetch_rows(conn, _period_comparison_sql("2000-12-04", "2000-12-11")),
        "comparison": "period_comparison",
        "required_instructions": [
            "Return both weekly rows and the delta between them.",
            "Keep the comparison on the same dyad and the same local export snapshot.",
        ],
        "required_sql_capabilities": {
            "joins": [],
            "expressions": ["WITH", "MAX(CASE WHEN ...)", "delta_total_count"],
            "functions": [],
        },
        "likely_failure_modes": [
            "date_window_ignored",
            "comparison_collapsed_to_single_total",
            "delta_sign_error",
        ],
        "benchmark_layer": "genie_benchmark",
        "reusable_in_avl": True,
    },
}


def _canonical_sql_for(question_id: str) -> str:
    sql_builder = CASE_DEFINITIONS[question_id]["canonical_sql"]
    return sql_builder() if callable(sql_builder) else str(sql_builder)


def _gold_result_for(question_id: str, conn: Any) -> list[dict[str, Any]]:
    return CASE_DEFINITIONS[question_id]["gold_builder"](conn)


def _question_bank_alignment(record: dict[str, Any], gold_result: list[dict[str, Any]]) -> dict[str, Any]:
    sources = [source for source in record.get("ground_truth_sources", []) if source.get("type") == "communication_dyads"]
    if not sources:
        return {"status": "unknown", "details": []}

    details: list[dict[str, Any]] = []
    aligned = True
    for source in sources:
        comparison = {"source": _json_clone(source), "status": "aligned"}
        if "total_count" in source:
            expected_total = int(source["total_count"])
            if record["question_id"] in {
                "enron-core-5675612b20",
                "enron-curated-pacheco-lay-summary-count",
            }:
                actual_total = int((gold_result or [{}])[0].get("total_emails") or 0)
            elif record["question_id"] in {
                "enron-curated-pacheco-lay-june19-dyad-count",
                "enron-curated-pacheco-lay-june26-dyad-count",
            }:
                actual_total = int((gold_result or [{}])[0].get("total_count") or 0)
            elif record["question_id"] == "enron-curated-pacheco-lay-december-dyad-comparison":
                matching = next(
                    (
                        row for row in gold_result
                        if str(row.get("period", ""))[:10] == str(source.get("period", ""))[:10]
                    ),
                    {},
                )
                actual_total = int(matching.get("total_count") or 0)
            elif record["question_id"] == "enron-core-b875644912":
                actual_total = int((gold_result or [{}])[0].get("total_emails") or 0)
            else:
                actual_total = expected_total
            comparison["expected_total_count"] = expected_total
            comparison["actual_total_count"] = actual_total
            if actual_total != expected_total:
                aligned = False
                comparison["status"] = "drifted"
        if "top_sender" in source:
            actual_sender = (gold_result or [{}])[0].get("contact_email")
            comparison["expected_top_sender"] = source["top_sender"]
            comparison["actual_top_sender"] = actual_sender
            if actual_sender != source["top_sender"]:
                aligned = False
                comparison["status"] = "drifted"
        details.append(comparison)
    return {
        "status": "aligned" if aligned else "drifted",
        "details": details,
    }


def build_ssot_manifest() -> dict[str, Any]:
    all_enron_rows = _load_question_bank_rows()
    genie_rows = load_governed_genie_rows()
    return {
        "created_at": _utc_now(),
        "source_of_truth": {
            "question_bank": "src/evaluation/question_bank.py",
            "question_bank_curation": "src/evaluation/question_bank_curation.py",
            "runtime_baselines": "src/evaluation/runtime_baselines.py",
        },
        "governed_inventory": {
            "enron_governed_question_count": len(all_enron_rows),
            "genie_iteration0_question_count": len(genie_rows),
            "genie_iteration0_question_ids": [row["question_id"] for row in genie_rows],
        },
        "scorer_manifest": {
            "source": "src/evaluation/enron_evaluation.py",
            "count": len(_load_scorer_names()),
            "scorers": _load_scorer_names(),
        },
        "backend_manifest": {
            "runtime_config_default_backend": {
                "value": "local",
                "source": "src/runtime/config.py::resolve_data_backend",
            },
            "agent_serving_default_backend": {
                "value": "lakebase",
                "source": "src/agent/agent_serving.py::_resolve_backend_type",
            },
            "analytics_backend_default": {
                "value": "databricks_sql",
                "source": "src/runtime/config.py::RuntimeConfig.from_env",
            },
            "local_snapshot_default": {
                "value": str(DEFAULT_LOCAL_DB_PATH),
                "source": "src/agent/agent_serving.py::_default_local_db_path",
            },
        },
        "baseline_registry": _load_runtime_baseline_registry(),
        "warnings": [
            "Benchmark runs must pin backend and transport explicitly because runtime and agent defaults diverge.",
            "The governed bank remains authoritative, but benchmark goldens are measured against the current local DuckDB snapshot.",
        ],
    }


def build_genie_benchmark_subset(db_path: str | Path | None = None) -> dict[str, Any]:
    rows = load_governed_genie_rows()
    with _open_duckdb(db_path) as (conn, resolved_path):
        cases = []
        aligned = 0
        drifted = 0
        for row in rows:
            question_id = row["question_id"]
            spec = CASE_DEFINITIONS[question_id]
            gold_result = _gold_result_for(question_id, conn)
            alignment = _question_bank_alignment(row, gold_result)
            if alignment["status"] == "aligned":
                aligned += 1
            elif alignment["status"] == "drifted":
                drifted += 1
            cases.append(
                {
                    "question_id": question_id,
                    "question_text": row["question"],
                    "normalized_question": row["question"],
                    "eval_split": row["eval_split"],
                    "classification": spec["classification"],
                    "benchmark_layer": spec["benchmark_layer"],
                    "reusable_in_avl": spec["reusable_in_avl"],
                    "primitive": row["primitive"],
                    "architecture_primary": row["architecture_primary"],
                    "expected_tools": row["expected_tools"],
                    "reference_answer": row["reference_answer"],
                    "question_bank_ground_truth_sources": _json_clone(row.get("ground_truth_sources", [])),
                    "canonical_sql": _canonical_sql_for(question_id),
                    "gold_result": gold_result,
                    "comparison_mode": spec["comparison"],
                    "tolerance": {"mode": "exact", "numeric_delta": 0},
                    "required_instructions": _json_clone(spec["required_instructions"]),
                    "required_sql_capabilities": _json_clone(spec["required_sql_capabilities"]),
                    "likely_failure_modes": _json_clone(spec["likely_failure_modes"]),
                    "question_bank_snapshot_alignment": alignment,
                }
            )
    return {
        "version": "1.0",
        "created_at": _utc_now(),
        "local_snapshot_path": str(resolved_path),
        "question_count": len(cases),
        "question_ids": [case["question_id"] for case in cases],
        "alignment_summary": {
            "aligned": aligned,
            "drifted": drifted,
            "unknown": len(cases) - aligned - drifted,
        },
        "cases": cases,
    }


@contextlib.contextmanager
def _temporary_enron_local_env():
    updates = {
        "GRAPHRAG_BACKEND": "local",
        "GRAPHRAG_DATA_BACKEND": "local",
        "GRAPHRAG_CORPUS": "enron",
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _load_query_and_enrich_callable() -> Callable[[str], str]:
    with _temporary_enron_local_env():
        import src.agent.agent_serving as agent_serving

        agent_serving = importlib.reload(agent_serving)
        tool = agent_serving.query_and_enrich
        fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None)
        if fn is None:
            raise RuntimeError("query_and_enrich did not expose an underlying callable")
        return fn


def _compare_results(case: dict[str, Any], observed: Any) -> dict[str, Any]:
    expected = case["gold_result"]
    mode = case["comparison_mode"]
    if mode == "pair_summary":
        expected_row = (expected or [{}])[0]
        observed_row = (observed or [{}])[0]
        expected_counts = sorted(
            [
                int(expected_row.get("sent_a_to_b") or 0),
                int(expected_row.get("sent_b_to_a") or 0),
            ]
        )
        observed_counts = sorted(
            [
                int(observed_row.get("sent_a_to_b") or 0),
                int(observed_row.get("sent_b_to_a") or 0),
            ]
        )
        expected_direction = "bidirectional" if all(value > 0 for value in expected_counts) else "one_way"
        observed_direction = "bidirectional" if all(value > 0 for value in observed_counts) else "one_way"
        passed = (
            expected_counts == observed_counts
            and int(expected_row.get("total_emails") or 0) == int(observed_row.get("total_emails") or 0)
            and expected_direction == observed_direction
        )
        return {
            "passed": passed,
            "expected_summary": {
                "directional_counts_sorted": expected_counts,
                "total_emails": int(expected_row.get("total_emails") or 0),
                "directionality": expected_direction,
            },
            "observed_summary": {
                "directional_counts_sorted": observed_counts,
                "total_emails": int(observed_row.get("total_emails") or 0),
                "directionality": observed_direction,
            },
        }
    if mode == "top_contact":
        expected_row = (expected or [{}])[0]
        observed_row = (observed or [{}])[0]
        passed = (
            expected_row.get("contact_email") == observed_row.get("contact_email")
            and int(expected_row.get("total_emails") or 0) == int(observed_row.get("total_emails") or 0)
            and expected_row.get("exchange_type") == observed_row.get("exchange_type")
        )
        return {
            "passed": passed,
            "expected_summary": {
                "contact_email": expected_row.get("contact_email"),
                "total_emails": int(expected_row.get("total_emails") or 0),
                "exchange_type": expected_row.get("exchange_type"),
            },
            "observed_summary": {
                "contact_email": observed_row.get("contact_email"),
                "total_emails": int(observed_row.get("total_emails") or 0),
                "exchange_type": observed_row.get("exchange_type"),
            },
        }
    if mode == "weekly_exact":
        expected_row = (expected or [{}])[0]
        observed_row = (observed or [{}])[0]
        passed = (
            str(expected_row.get("period", ""))[:10] == str(observed_row.get("period", ""))[:10]
            and int(expected_row.get("total_count") or 0) == int(observed_row.get("total_count") or 0)
            and int(expected_row.get("to_count") or 0) == int(observed_row.get("to_count") or 0)
            and int(expected_row.get("cc_count") or 0) == int(observed_row.get("cc_count") or 0)
            and int(expected_row.get("bcc_count") or 0) == int(observed_row.get("bcc_count") or 0)
        )
        return {
            "passed": passed,
            "expected_summary": expected_row,
            "observed_summary": observed_row,
        }
    if mode == "period_comparison":
        expected_periods = {
            str(row.get("period", ""))[:10]: row for row in expected
        }
        observed_periods = {
            str(row.get("period", ""))[:10]: row for row in (observed or [])
        }
        passed = True
        for period, expected_row in expected_periods.items():
            observed_row = observed_periods.get(period, {})
            if (
                int(expected_row.get("total_count") or 0) != int(observed_row.get("total_count") or 0)
                or int(expected_row.get("delta_total_count") or 0) != int(observed_row.get("delta_total_count") or 0)
            ):
                passed = False
                break
        return {
            "passed": passed and bool(expected_periods),
            "expected_summary": expected_periods,
            "observed_summary": observed_periods,
        }
    return {"passed": False, "expected_summary": expected, "observed_summary": observed}


def measure_local_query_and_enrich_baseline(benchmark_subset: dict[str, Any]) -> dict[str, Any]:
    run_rows = []
    latencies_ms: list[float] = []
    passed_count = 0
    empty_count = 0
    fn = _load_query_and_enrich_callable()
    for case in benchmark_subset["cases"]:
        started = time.perf_counter()
        raw = fn(case["question_text"])
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies_ms.append(elapsed_ms)
        payload = json.loads(raw)
        genie_result = payload.get("genie_result", {})
        observed_rows = genie_result.get("results")
        comparison = _compare_results(case, observed_rows)
        if comparison["passed"]:
            passed_count += 1
        if not observed_rows:
            empty_count += 1
        run_rows.append(
            {
                "question_id": case["question_id"],
                "latency_ms": round(elapsed_ms, 3),
                "passed": comparison["passed"],
                "description": genie_result.get("description"),
                "observed_sql": genie_result.get("sql_generated"),
                "observed_result": observed_rows,
                "comparison": comparison,
            }
        )
    total = max(1, len(run_rows))
    return {
        "execution_surface": "query_and_enrich_local_sql_fallback",
        "created_at": _utc_now(),
        "question_count": len(run_rows),
        "benchmark_pass_rate": round(passed_count / total, 3),
        "sql_correctness": round(passed_count / total, 3),
        "failure_rate": round(empty_count / total, 3),
        "latency_ms": {
            "p50": round(statistics.median(latencies_ms), 3) if latencies_ms else None,
            "p95": round(_percentile(latencies_ms, 95) or 0.0, 3) if latencies_ms else None,
            "max": round(max(latencies_ms), 3) if latencies_ms else None,
        },
        "question_results": run_rows,
        "notes": [
            "This baseline measures the current local SQL-fallback wrapper, not the full routed agent.",
            "Route-level metrics still require MLflow-evaluated end-to-end runs over the frozen benchmark subset.",
        ],
    }


def build_wave1_migration() -> dict[str, Any]:
    return {
        "title": "Iteration 1 - low-risk/high-value Genie moves",
        "scope": {
            "tools": list(WAVE1_GENIE_TOOLS),
            "classification": "migrate_to_genie_now",
        },
        "wrapper_contract": {
            "surface": "preserve current tool names while moving analytical SQL behind governed semantic-layer execution",
            "fallback_order": [
                "local_duckdb_sql",
                "databricks_sql_semantic_layer",
                "lakebase_sql_parity_check",
                "explicit_abstention_on_unexplained_drift",
            ],
        },
        "success_metrics": {
            "sql_correctness_min": 0.95,
            "tool_selection_correctness_min": 0.95,
            "targeted_slice_accuracy_lift_pp": 5,
            "canonical_regression_max_drop_pp": 1,
        },
        "rollback_triggers": list(WAVE1_ROLLBACK_TRIGGERS),
        "dependencies": [
            "stable metric-view definitions in src/runtime/analytics_sql.py",
            "exact-answer benchmark goldens in the generated Genie baseline bundle",
            "route-level tests before expanding beyond the first wave",
        ],
    }


def build_hybrid_wrapper_contract() -> dict[str, Any]:
    return {
        "title": "Hybrid analytics wrapper contract",
        "scope": {
            "tools": list(HYBRID_ANALYTICS_TOOLS),
            "classification": "hybridize",
        },
        "required_sequence": list(HYBRID_FALLBACK_ORDER),
        "contract": {
            "request_shape": {
                "question": "string",
                "resolved_entities": ["canonical names and email patterns"],
                "time_window": "optional normalized range",
                "analytics_intent": "count|ranking|distribution|timeline|listing",
            },
            "response_shape": {
                "analytics_result": "deterministic SQL or Genie payload",
                "enrichment": "role, entity, data quality, and coverage annotations",
                "fallback_used": "bool",
                "fallback_reason": "string|null",
                "evidence_ready": "bool",
            },
            "non_goals": [
                "Genie-driven vector retrieval",
                "Genie-driven entity resolution",
                "Genie-driven provenance synthesis",
            ],
        },
        "success_metrics": {
            "sql_correctness_min": 0.90,
            "tool_selection_correctness_lift_pp": 5,
            "p95_latency_budget_multiplier": 1.15,
            "wrong_entity_rate_max": 0.05,
        },
        "rollback_triggers": [
            "wrong_entity_rate_gt_0.05",
            "date_window_failures_persist_for_two_iterations",
            "hybrid_complexity_increases_without_benchmark_lift",
        ],
    }


def build_iteration_scorecard_definition(
    baseline_measurements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scorecard = {
        "title": "Genie migration iteration scorecard",
        "gating_policy": {
            "continue": "two consecutive green iterations",
            "expand": "green on benchmark, regression, AVL, and holdout layers",
            "pause": "plateau below 2pp over two iterations or maintainability worsens",
            "rollback": "any red trust metric, >2pp canonical regression, >5% hard-failure rate, or >20% latency regression without compensating gains",
        },
        "metrics": _json_clone(ITERATION_SCORECARD_METRICS),
    }
    if baseline_measurements:
        scorecard["baseline_measurements"] = _json_clone(baseline_measurements)
    return scorecard


def build_baseline_bundle(db_path: str | Path | None = None) -> dict[str, Any]:
    ssot_manifest = build_ssot_manifest()
    benchmark_subset = build_genie_benchmark_subset(db_path)
    baseline_measurements = measure_local_query_and_enrich_baseline(benchmark_subset)
    aligned = benchmark_subset["alignment_summary"]["aligned"]
    drifted = benchmark_subset["alignment_summary"]["drifted"]
    total = max(1, benchmark_subset["question_count"])
    return {
        "version": "1.0",
        "created_at": _utc_now(),
        "iteration": 0,
        "executive_summary": {
            "governed_genie_question_count": benchmark_subset["question_count"],
            "local_wrapper_benchmark_pass_rate": baseline_measurements["benchmark_pass_rate"],
            "question_bank_snapshot_alignment_rate": round(aligned / total, 3),
            "question_bank_snapshot_drift_rate": round(drifted / total, 3),
            "primary_findings": [
                "The governed Enron quantitative slice is compact and benchmarkable.",
                "The current local SQL-fallback path passes only part of that slice, so Iteration 1 should stay narrow and reversible.",
                "The current local DuckDB snapshot already diverges from several governed reference answers, so benchmark goldens must record both the governed bank view and the measured snapshot view.",
            ],
        },
        "ssot_manifest": ssot_manifest,
        "benchmark_subset": benchmark_subset,
        "current_local_wrapper_baseline": baseline_measurements,
        "wave1_migration": build_wave1_migration(),
        "hybrid_wrapper_contract": build_hybrid_wrapper_contract(),
        "adr_log": _json_clone(ADR_LOG),
        "iteration_scorecard": build_iteration_scorecard_definition(baseline_measurements),
    }


def _render_markdown(bundle: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = bundle["executive_summary"]
    benchmark = bundle["benchmark_subset"]
    baseline = bundle["current_local_wrapper_baseline"]
    ssot = bundle["ssot_manifest"]

    lines.append("# Genie Iteration 0 Baseline")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- Governed Genie benchmark questions: `{summary['governed_genie_question_count']}`")
    lines.append(f"- Local `query_and_enrich` benchmark pass rate: `{summary['local_wrapper_benchmark_pass_rate']}`")
    lines.append(f"- Question-bank vs local-snapshot alignment rate: `{summary['question_bank_snapshot_alignment_rate']}`")
    lines.append(f"- Question-bank vs local-snapshot drift rate: `{summary['question_bank_snapshot_drift_rate']}`")
    for finding in summary["primary_findings"]:
        lines.append(f"- {finding}")
    lines.append("")

    lines.append("## SSOT Manifest")
    lines.append("")
    lines.append(f"- Governed Enron questions: `{ssot['governed_inventory']['enron_governed_question_count']}`")
    lines.append(f"- Iteration 0 Genie question IDs: `{', '.join(ssot['governed_inventory']['genie_iteration0_question_ids'])}`")
    lines.append(f"- Enron scorer set: `{', '.join(ssot['scorer_manifest']['scorers'])}`")
    lines.append(f"- Runtime default backend: `{ssot['backend_manifest']['runtime_config_default_backend']['value']}`")
    lines.append(f"- Agent-serving default backend: `{ssot['backend_manifest']['agent_serving_default_backend']['value']}`")
    lines.append("")

    lines.append("## Benchmark Cases")
    lines.append("")
    lines.append("| question_id | split | mode | classification | alignment |")
    lines.append("|---|---|---|---|---|")
    for case in benchmark["cases"]:
        lines.append(
            f"| `{case['question_id']}` | `{case['eval_split']}` | `{case['comparison_mode']}` | `{case['classification']}` | `{case['question_bank_snapshot_alignment']['status']}` |"
        )
    lines.append("")

    lines.append("## Local Wrapper Baseline")
    lines.append("")
    lines.append(f"- Execution surface: `{baseline['execution_surface']}`")
    lines.append(f"- SQL correctness: `{baseline['sql_correctness']}`")
    lines.append(f"- Benchmark pass rate: `{baseline['benchmark_pass_rate']}`")
    lines.append(f"- Failure rate: `{baseline['failure_rate']}`")
    lines.append(f"- Latency p50 (ms): `{baseline['latency_ms']['p50']}`")
    lines.append(f"- Latency p95 (ms): `{baseline['latency_ms']['p95']}`")
    lines.append("")
    lines.append("| question_id | passed | latency_ms |")
    lines.append("|---|---:|---:|")
    for row in baseline["question_results"]:
        lines.append(f"| `{row['question_id']}` | `{row['passed']}` | `{row['latency_ms']}` |")
    lines.append("")

    wave1 = bundle["wave1_migration"]
    lines.append("## Wave 1 Migration")
    lines.append("")
    lines.append(f"- Tools: `{', '.join(wave1['scope']['tools'])}`")
    lines.append(f"- Rollback triggers: `{', '.join(wave1['rollback_triggers'])}`")
    lines.append("")

    hybrid = bundle["hybrid_wrapper_contract"]
    lines.append("## Hybrid Contract")
    lines.append("")
    lines.append(f"- Tools: `{', '.join(hybrid['scope']['tools'])}`")
    lines.append(f"- Fallback order: `{', '.join(hybrid['required_sequence'])}`")
    lines.append("")

    lines.append("## ADR Log")
    lines.append("")
    for adr in bundle["adr_log"]:
        lines.append(f"- `{adr['id']}` {adr['title']} (`{adr['status']}`)")
    lines.append("")

    lines.append("## Iteration Scorecard")
    lines.append("")
    lines.append("| metric | baseline | target | green | yellow | red |")
    lines.append("|---|---|---|---|---|---|")
    for metric in bundle["iteration_scorecard"]["metrics"]:
        lines.append(
            f"| `{metric['name']}` | `{metric['baseline']}` | `{metric['target']}` | `{metric['green']}` | `{metric['yellow']}` | `{metric['red']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_baseline_bundle(
    *,
    json_path: str | Path = DEFAULT_BASELINE_JSON_PATH,
    markdown_path: str | Path = DEFAULT_BASELINE_MD_PATH,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    bundle = build_baseline_bundle(db_path)
    resolved_json_path = Path(json_path)
    resolved_json_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_json_path.write_text(json.dumps(bundle, indent=2))

    resolved_markdown_path = Path(markdown_path)
    resolved_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_markdown_path.write_text(_render_markdown(bundle))
    return bundle


def inject_runtime_baseline_registry(existing: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    updated = _json_clone(existing)
    updated.setdefault("enron", {})
    updated["enron"]["genie_quantitative_iteration0"] = {
        "slice": "governed_genie_iteration0",
        "question_count": bundle["benchmark_subset"]["question_count"],
        "artifact_path": "src/evaluation/baselines/genie_iteration0_baseline.json",
        "sql_correctness_current": bundle["current_local_wrapper_baseline"]["sql_correctness"],
        "benchmark_pass_rate_current": bundle["current_local_wrapper_baseline"]["benchmark_pass_rate"],
        "failure_rate_current": bundle["current_local_wrapper_baseline"]["failure_rate"],
        "target_sql_correctness": 0.95,
        "target_benchmark_pass_rate": 0.90,
        "target_failure_rate_max": 0.02,
        "target_latency_p95_multiplier_max": 1.10,
        "notes": "Iteration 0 frozen benchmark and scorecard for the governed Enron quantitative/Genie slice.",
    }
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Iteration 0 Genie migration artifacts.")
    parser.add_argument(
        "--json-path",
        default=str(DEFAULT_BASELINE_JSON_PATH),
        help="Where to write the frozen baseline bundle JSON.",
    )
    parser.add_argument(
        "--markdown-path",
        default=str(DEFAULT_BASELINE_MD_PATH),
        help="Where to write the frozen baseline bundle markdown summary.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional local DuckDB path override.",
    )
    parser.add_argument(
        "--write-runtime-baselines",
        action="store_true",
        help="Also update src/evaluation/baselines/runtime_eval_baseline.json with the new Genie slice entry.",
    )
    args = parser.parse_args(argv)

    bundle = write_baseline_bundle(
        json_path=args.json_path,
        markdown_path=args.markdown_path,
        db_path=args.db_path,
    )
    if args.write_runtime_baselines:
        updated_registry = inject_runtime_baseline_registry(
            _load_runtime_baseline_registry(),
            bundle,
        )
        Path(DEFAULT_RUNTIME_BASELINE_PATH).write_text(json.dumps(updated_registry, indent=2))

    print(
        json.dumps(
            {
                "json_path": str(Path(args.json_path)),
                "markdown_path": str(Path(args.markdown_path)),
                "question_count": bundle["benchmark_subset"]["question_count"],
                "benchmark_pass_rate": bundle["current_local_wrapper_baseline"]["benchmark_pass_rate"],
                "alignment_summary": bundle["benchmark_subset"]["alignment_summary"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
