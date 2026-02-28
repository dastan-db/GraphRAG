# D-001: Graph Backend Selection — Delta Tables over Neo4j

**Phase:** 01-Data-Prep
**Date Decided:** 2026-02-27 (backfilled)
**Reversibility:** One-way door (migration cost is high mid-project)
**Implementation Fidelity:** Level 2 (validated, functional — proven through Phases 01-03)

## Decision
Use Delta tables as graph backend for v1. Defer Neo4j/Memgraph to v2.

## Why This Over Alternatives
Delta tables give us zero external infrastructure, full Spark SQL compatibility, and the fastest path to a working demo. The Bible corpus (~500 entities, ~2000 relationships) is well within Delta's performance envelope. We accept a query performance ceiling for complex multi-hop traversals — if that becomes a blocker in v2, we evaluate dedicated graph engines then. The key trade-off: speed-to-value now vs. optimal graph performance later.

## Options Considered
- **Delta Tables (chosen):** Native to Databricks, team knows Spark SQL, no vendor lock-in, fastest to ship
- **Neo4j:** Best graph query performance, but external dependency, new skill required, higher ops cost
- **Memgraph:** Similar trade-offs to Neo4j with slightly lower cost

## Trade-offs Accepted
- Query performance ceiling for deep multi-hop queries
- Possible migration to dedicated graph engine in v2
- Graph traversal patterns limited to what Spark SQL can express efficiently

## Platform Leverage Check
Does this demonstrate something only Databricks can do well? **Yes.** Delta-native graph storage + Unity Catalog governance + MLflow lineage is a uniquely Databricks pattern. This decision strengthens the Solution Accelerator story.

## Validation (Filled After v1 Completion)
- [x] Did Delta perform adequately for demo corpus? **Yes.** ~500 entities, ~2000 relationships — well within Delta's performance envelope. All 5 graph traversal tools return results within acceptable latency for demo purposes.
- [x] Were any queries noticeably slow? **No.** The `trace_path` tool (1/2/3-hop joins) is the most expensive operation but performs adequately at demo scale. Spark SQL handles the self-joins cleanly.
- [x] Is there evidence we need a dedicated graph engine for v2? **Not yet.** At current corpus size, Delta is sufficient. If v2 scales to thousands of entities or requires complex graph algorithms (PageRank, community detection), a dedicated engine would be warranted.
- [x] Would we make this same choice again? **Yes.** The zero-infrastructure, fully Databricks-native approach was the right call for v1. It unblocked the entire project and strengthened the Solution Accelerator story.

**Date Reviewed:** 2026-02-28
**Outcome:** Success
**Evidence:** All 5 graph tools query Delta via Spark SQL with no performance issues. Pipeline runs end-to-end. Agent produces multi-hop answers using Delta-backed graph traversal.
**Learning:** Delta tables are excellent for demo-scale knowledge graphs. The key constraint isn't performance — it's the lack of native graph algorithms. For v2, evaluate whether Lakebase (indexed OLTP lookups) closes the gap before jumping to a dedicated graph engine.
**Decision for v2:** Proceed with Lakebase evaluation (Phase 05) before considering Neo4j/Memgraph (Phase 07).
