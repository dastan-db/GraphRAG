# D-001: Graph Backend Selection — Delta Tables over Neo4j

**Phase:** 01-Data-Prep
**Date Decided:** [YYYY-MM-DD — backfill from memory]
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

## Validation (Fill After v1 Completion)
- [ ] Did Delta perform adequately for demo corpus?
- [ ] Were any queries noticeably slow?
- [ ] Is there evidence we need a dedicated graph engine for v2?
- [ ] Would we make this same choice again?

**Date Reviewed:**
**Outcome:** [Success / Partial / Failed]
**Evidence:**
**Learning:**
**Decision for v2:**
