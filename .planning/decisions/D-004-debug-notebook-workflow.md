# D-004: Debug-First Notebook Workflow

**Phase:** Cross-cutting (all phases with pipeline notebooks)
**Date Decided:** 2026-02-27
**Reversibility:** Two-way door (can stop producing debug notebooks anytime; production notebooks are unaffected)
**Implementation Fidelity:** Level 2 (validated — pattern proven with 02_Interactive_Debug.py)

## Decision
When building or modifying Databricks pipeline notebooks (`XX_*.py`), always produce a companion `XX_Interactive_Debug.py` with inlined configuration and diagnostic cells. The user runs the debug notebook interactively on a cluster to fix runtime errors, then the agent incorporates fixes back into the production notebook.

## Context
Pipeline notebooks execute headlessly in Databricks jobs. Runtime errors — schema mismatches from `ai_query()` structured output, unexpected `responseFormat` response shapes, Spark SQL edge cases — surface only at execution time and are painful to debug in the job run UI. You cannot step through cells, inspect intermediate DataFrames, or iterate on fixes.

## Why This Over Alternatives
- **Alternative 1: Debug directly in production notebooks.** Adds clutter (debug cells, inlined config) to notebooks that must stay clean for job execution. Rejected.
- **Alternative 2: Debug only via job run logs.** Slow iteration cycle — resubmit the entire job to test a one-line fix. Rejected.
- **Alternative 3: Separate debug notebooks (chosen).** Clean separation of concerns. Production notebooks stay job-ready. Debug notebooks are self-contained and disposable. The user gets interactive cell-by-cell execution. Fixes flow back into production via the agent.

## Trade-offs Accepted
- Two files to keep in sync per pipeline notebook (mitigated by the incorporate-back protocol).
- Debug notebooks duplicate some code from production notebooks (acceptable — they are intentionally self-contained).

## Platform Leverage Check
Debug notebooks run on any Databricks cluster with no special configuration. They use standard `# Databricks notebook source` format and work with the notebook UI's cell-by-cell execution. No additional infrastructure or tooling required.

## Validation
- [x] Does the existing `02_Interactive_Debug.py` demonstrate the pattern works? **Yes.** 512-line notebook with inlined config, diagnostic cells (printSchema, display, try/except probes), and self-contained execution. Mirrors production notebook structure.
- [x] Does the incorporate-back flow preserve production notebook cleanliness? **Yes.** Production `02_Build_Knowledge_Graph.py` uses `%run` for shared config and has no debug/diagnostic cells. Fixes from the debug notebook were incorporated without adding clutter.
- [x] Do debug notebooks reduce time-to-fix for pipeline errors? **Yes.** The `ai_query()` response structure issue (nested `result.result` vs `result`) was diagnosed and fixed in the debug notebook via interactive cell-by-cell execution. This would have been significantly slower via job run logs.

**Date Reviewed:** 2026-02-28
**Outcome:** Success
**Evidence:** `notebooks/spikes/02_Interactive_Debug.py` exists with DEBUG cells for schema inspection. Production notebook is clean. The pattern is documented as D-004 and codified in `gsd-execution.mdc`.
**Learning:** The debug-first pattern pays for itself on the first `ai_query()` schema mismatch. The overhead (maintaining two files) is minimal compared to the time saved debugging headless job failures. Recommend applying this pattern to any notebook that calls `ai_query()` or complex Spark operations.
