# Decision Gate

## Outcome

Decision: `Continue`

## Evidence

- Phase-2 overall: `0.6913`
- Accepted overall: `0.7513`
- Phase-2 holdout: `0.7139`
- Accepted holdout: `0.8308`
- Holdout floor met: `True`
- GraphRAG vs GPT overall delta: `+0.3096`
- GraphRAG question record: `wins=89`, `losses=0`, `near_ties=4`
- Candidate run: `factual_baseline_quality_crosscut_v4.json`
- Candidate promoted: `False`
- Candidate overall: `0.7622`
- Candidate holdout: `0.8450`
- Candidate regressions > 0.02: `1`
- Blocking regression: `timeline_reconstruction x timeline_retrieval` fell from `0.8190` to `0.7240` (`-0.0950`)

## Rationale

- The accepted baseline is materially above the phase-2 floor on both overall and holdout metrics.
- Raw GPT-5.4 remains far behind on corpus-grounded trust, especially evidence quality, grounding integrity, and completeness.
- Remaining failures are narrow and actionable, concentrated in synthesis tails plus a few coverage-thin required cells.
- The newest candidate is better in aggregate but still fails the hard gate because it meaningfully regressed a required timeline cell.
- `Expand` is premature because required-cell coverage is still thin in a few places; `Pause` or `Rollback` would ignore a clear and measurable trust gain.
