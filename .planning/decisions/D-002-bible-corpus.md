# D-002: Demo Corpus Selection — King James Bible

**Phase:** 01-Data-Prep
**Date Decided:** 2026-02-27 (backfilled)
**Reversibility:** Two-way door (corpus can be swapped without architecture changes)
**Implementation Fidelity:** Level 2 (validated — extraction and reasoning patterns work)

## Decision
Use King James Bible (Genesis, Exodus, Ruth, Matthew, Acts) as demo corpus.

## Why This Over Alternatives
The Bible provides exceptional entity/relationship density, verifiable ground truth (anyone can check answers against source text), and domain-agnostic patterns that transfer to enterprise use cases. It's non-proprietary, universally recognizable, and its cross-reference structure mirrors the kind of connected data enterprises deal with. The trade-off: it's not a "real" enterprise dataset, so we must validate that extraction and reasoning patterns generalize.

## Trade-offs Accepted
- Not a real enterprise use case — must prove pattern transferability
- Religious content may distract from technical message in some audiences

## Platform Leverage Check
Corpus-agnostic — this decision doesn't specifically leverage Databricks, but it doesn't conflict either. The extraction pipeline is the same regardless of corpus.

## Validation (Filled After v1 Completion)
- [x] Entity/relationship patterns extracted accurately? **Yes.** The 6 entity types (Person, Place, Event, Group, Object, Concept) and relationship extraction via `ai_query()` produce a dense, interconnected graph. Entity deduplication via slug normalization handles name variants.
- [x] Multi-hop reasoning works on this corpus? **Yes.** The Ruth→Obed→Jesse→David→Jesus lineage trace and cross-book entity connections (Moses in Exodus, Matthew, Acts) demonstrate multi-hop reasoning convincingly.
- [x] Answers verifiable against source text? **Yes.** This is the corpus's strongest property. Anyone can verify that "Ruth married Boaz" appears in Ruth 4:13 or that "Saul was blinded on the road to Damascus" is in Acts 9. Ground truth is universally accessible.
- [x] Confidence this generalizes to enterprise in v2? **High.** The extraction patterns (entity/relationship types, deduplication, mention tracking) are domain-agnostic. The Bible's cross-reference density mirrors enterprise knowledge bases (legal cross-references, medical guideline chains, financial regulatory dependencies).

**Date Reviewed:** 2026-02-28
**Outcome:** Success
**Evidence:** 20-question evaluation dataset with expected facts validates extraction accuracy. Multi-hop queries (lineage tracing, cross-book connections) work reliably. The governance scorers confirm answers are grounded in source text.
**Learning:** The Bible was an excellent choice for v1. Its density and verifiability made it easy to build and validate the full pipeline. For v2, the corpus can be swapped with zero architecture changes — just point at a different data source and adjust entity types.
