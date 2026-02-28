# D-002: Demo Corpus Selection — King James Bible

**Phase:** 01-Data-Prep
**Date Decided:** [YYYY-MM-DD — backfill]
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

## Validation (Fill After v1 Completion)
- [ ] Entity/relationship patterns extracted accurately?
- [ ] Multi-hop reasoning works on this corpus?
- [ ] Answers verifiable against source text?
- [ ] Confidence this generalizes to enterprise in v2?

**Date Reviewed:**
**Outcome:**
**Learning:**
