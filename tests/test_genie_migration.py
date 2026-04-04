from __future__ import annotations

import json
from pathlib import Path

from src.evaluation.genie_migration import (
    ADR_LOG,
    CASE_DEFINITIONS,
    DEFAULT_BASELINE_JSON_PATH,
    GENIE_ITERATION0_QUESTION_IDS,
    HYBRID_ANALYTICS_TOOLS,
    ITERATION_SCORECARD_METRICS,
    WAVE1_GENIE_TOOLS,
    build_hybrid_wrapper_contract,
    build_wave1_migration,
    load_governed_genie_rows,
)
from src.evaluation.runtime_baselines import load_runtime_baselines


def _load_generated_baseline() -> dict:
    return json.loads(Path(DEFAULT_BASELINE_JSON_PATH).read_text())


def test_iteration0_question_ids_remain_governed():
    rows = load_governed_genie_rows()
    assert [row["question_id"] for row in rows] == list(GENIE_ITERATION0_QUESTION_IDS)


def test_case_definitions_cover_every_iteration0_question():
    assert set(CASE_DEFINITIONS) == set(GENIE_ITERATION0_QUESTION_IDS)


def test_wave1_scope_stays_narrow_and_reversible():
    wave1 = build_wave1_migration()
    assert wave1["scope"]["tools"] == list(WAVE1_GENIE_TOOLS)
    assert wave1["scope"]["classification"] == "migrate_to_genie_now"
    assert "sql_correctness_min" in wave1["success_metrics"]
    assert wave1["rollback_triggers"]


def test_hybrid_contract_keeps_resolution_outside_genie():
    contract = build_hybrid_wrapper_contract()
    assert contract["scope"]["tools"] == list(HYBRID_ANALYTICS_TOOLS)
    assert contract["required_sequence"][0] == "local_entity_resolution"
    assert "Genie-driven entity resolution" in contract["contract"]["non_goals"]


def test_generated_baseline_artifact_has_required_sections():
    baseline = _load_generated_baseline()
    assert baseline["iteration"] == 0
    assert baseline["benchmark_subset"]["question_ids"] == list(GENIE_ITERATION0_QUESTION_IDS)
    assert baseline["wave1_migration"]["scope"]["tools"] == list(WAVE1_GENIE_TOOLS)
    assert baseline["hybrid_wrapper_contract"]["scope"]["tools"] == list(HYBRID_ANALYTICS_TOOLS)
    assert len(baseline["adr_log"]) == len(ADR_LOG)
    assert len(baseline["iteration_scorecard"]["metrics"]) == len(ITERATION_SCORECARD_METRICS)


def test_generated_benchmark_cases_have_required_fields():
    baseline = _load_generated_baseline()
    for case in baseline["benchmark_subset"]["cases"]:
        assert case["canonical_sql"]
        assert case["gold_result"] is not None
        assert case["expected_tools"] == ["query_and_enrich"]
        assert case["likely_failure_modes"]
        assert case["question_bank_snapshot_alignment"]["status"] in {"aligned", "drifted", "unknown"}


def test_runtime_baselines_register_genie_iteration0_slice():
    registry = load_runtime_baselines()
    genie_slice = registry["enron"]["genie_quantitative_iteration0"]
    assert genie_slice["question_count"] == len(GENIE_ITERATION0_QUESTION_IDS)
    assert genie_slice["artifact_path"] == "src/evaluation/baselines/genie_iteration0_baseline.json"
