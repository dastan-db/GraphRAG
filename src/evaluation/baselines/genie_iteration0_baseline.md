# Genie Iteration 0 Baseline

## Executive Summary

- Governed Genie benchmark questions: `6`
- Local `query_and_enrich` benchmark pass rate: `0.5`
- Question-bank vs local-snapshot alignment rate: `0.5`
- Question-bank vs local-snapshot drift rate: `0.5`
- The governed Enron quantitative slice is compact and benchmarkable.
- The current local SQL-fallback path passes only part of that slice, so Iteration 1 should stay narrow and reversible.
- The current local DuckDB snapshot already diverges from several governed reference answers, so benchmark goldens must record both the governed bank view and the measured snapshot view.

## SSOT Manifest

- Governed Enron questions: `93`
- Iteration 0 Genie question IDs: `enron-core-5675612b20, enron-core-b875644912, enron-curated-pacheco-lay-summary-count, enron-curated-pacheco-lay-june19-dyad-count, enron-curated-pacheco-lay-june26-dyad-count, enron-curated-pacheco-lay-december-dyad-comparison`
- Enron scorer set: `evidence_quality, participant_verification, organizational_accuracy, grounding_integrity, factual_accuracy, hallucination_detection, answer_completeness`
- Runtime default backend: `local`
- Agent-serving default backend: `lakebase`

## Benchmark Cases

| question_id | split | mode | classification | alignment |
|---|---|---|---|---|
| `enron-core-5675612b20` | `train` | `pair_summary` | `both` | `drifted` |
| `enron-core-b875644912` | `test` | `top_contact` | `both` | `drifted` |
| `enron-curated-pacheco-lay-summary-count` | `holdout` | `pair_summary` | `holdout_only` | `drifted` |
| `enron-curated-pacheco-lay-june19-dyad-count` | `train` | `weekly_exact` | `genie_benchmark_only` | `aligned` |
| `enron-curated-pacheco-lay-june26-dyad-count` | `train` | `weekly_exact` | `genie_benchmark_only` | `aligned` |
| `enron-curated-pacheco-lay-december-dyad-comparison` | `train` | `period_comparison` | `both` | `aligned` |

## Local Wrapper Baseline

- Execution surface: `query_and_enrich_local_sql_fallback`
- SQL correctness: `0.5`
- Benchmark pass rate: `0.5`
- Failure rate: `0.5`
- Latency p50 (ms): `2.837`
- Latency p95 (ms): `22.51`

| question_id | passed | latency_ms |
|---|---:|---:|
| `enron-core-5675612b20` | `True` | `27.435` |
| `enron-core-b875644912` | `True` | `7.733` |
| `enron-curated-pacheco-lay-summary-count` | `True` | `5.646` |
| `enron-curated-pacheco-lay-june19-dyad-count` | `False` | `0.028` |
| `enron-curated-pacheco-lay-june26-dyad-count` | `False` | `0.018` |
| `enron-curated-pacheco-lay-december-dyad-comparison` | `False` | `0.015` |

## Wave 1 Migration

- Tools: `get_top_email_pairs, get_top_individuals, detect_self_emails`
- Rollback triggers: `unexplained_count_drift_vs_duckdb_or_lakebase, two_consecutive_red_scorecards_on_quantitative_slice, p95_latency_regression_gt_20pct_without_quality_gain`

## Hybrid Contract

- Tools: `query_and_enrich, find_top_contacts, get_dyad_topics, get_external_contacts, get_communication_stats, get_topic_distribution, get_communication_timeline, find_emails, get_emails_between, browse_topics`
- Fallback order: `local_entity_resolution, local_duckdb_or_lakebase_sql, genie_or_databricks_sql_semantic_layer, duckdb_or_lakebase_evidence_enrichment, deterministic_abstention_on_missing_or_conflicting_evidence`

## ADR Log

- `ADR-01` Keep governed question bank as the single evaluation SSOT (`adopted`)
- `ADR-02` Migrate only the analytics_sql_genie slice first (`adopted`)
- `ADR-03` Use hybrid wrappers for entity-resolved analytics (`adopted`)
- `ADR-04` Preserve DuckDB as local-fast reference and Lakebase as governed remote reference (`adopted`)
- `ADR-05` Keep graph, vector, evidence, and provenance tools off Genie until evidence says otherwise (`adopted`)
- `ADR-06` Certify every migration with layered evaluation and holdout gates (`adopted`)
- `ADR-07` Normalize backend defaults before comparing architectures (`adopted`)

## Iteration Scorecard

| metric | baseline | target | green | yellow | red |
|---|---|---|---|---|---|
| `answer_accuracy` | `iteration0_measurement_pending` | `+3pp overall or +5pp on the targeted slice with no non-target regression >1pp` | `meets target` | `within 1pp of baseline` | `below baseline or cross-slice regression >1pp` |
| `evidence_grounding` | `iteration0_measurement_pending` | `+5pp on migrated slice with zero fabricated evidence` | `target met with zero critical fabrications` | `flat to +4pp` | `any trust regression or evidence fabrication` |
| `tool_selection_correctness` | `iteration0_measurement_pending` | `>=0.95 on benchmark and +5pp on migrated routes` | `>=0.95` | `0.90-0.949` | `<0.90` |
| `sql_correctness` | `measured_on_local_query_and_enrich` | `>=0.95 for Genie-native tools and >=0.90 for hybrid tools` | `meets target` | `0.90-0.949 for Genie-native tools` | `<0.90 for Genie-native tools` |
| `benchmark_pass_rate` | `measured_on_local_query_and_enrich` | `>=0.90` | `>=0.90` | `0.80-0.899` | `<0.80` |
| `latency` | `measured_on_local_query_and_enrich` | `p95 not worse than baseline for pure Genie and not worse than +10-15% for hybrid` | `within target` | `+10-20%` | `>20%` |
| `failure_rate` | `measured_on_local_query_and_enrich` | `<0.02 and no increase vs baseline` | `<0.02` | `0.02-0.05` | `>0.05` |
| `cost` | `iteration0_measurement_pending` | `<=+10% unless quality improves materially` | `<=+10%` | `+10-20%` | `>20% without quality gain` |
| `maintainability` | `iteration0_snapshot` | `20% reduction in duplicate analytics logic and smaller change surface` | `improved` | `flat` | `worse by >10%` |
| `developer_effort` | `iteration0_measurement_pending` | `25% reduction in median time to passing validation` | `target met` | `flat` | `slower by >10%` |
| `governance_trust_fit` | `iteration0_measurement_pending` | `no regression and zero policy leaks` | `flat or better with zero critical violations` | `minor non-critical dip <=1pp` | `any policy leak or trust regression >1pp` |
| `adversarial_robustness` | `iteration0_measurement_pending` | `+5pp on escalated slices and zero critical fabrication failures` | `target met` | `partial improvement` | `any critical fail or no improvement over two iterations` |
