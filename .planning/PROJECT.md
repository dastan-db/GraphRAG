# GraphRAG — Project context

> **End in mind:** See [PRFAQ.md](PRFAQ.md) for the Working Backwards artifact — the customer, the problem, the press release, and the FAQ that define what we are building and why. Requirements and roadmap are derived from it.

## Vision

GraphRAG: build a retrieval-augmented system that uses a knowledge graph to deliver **auditable, traceable, reproducible** AI reasoning over documents. The core thesis is that structured retrieval (graph traversal with provenance) solves the enterprise governance problem — every answer can be audited, every claim traced to source data, every result reproduced deterministically.

## Scope (current milestone)

- Purpose: Demonstrate that graph-structured retrieval produces auditable, grounded AI reasoning — and prove it with governance-focused evaluation metrics.
- Demo corpus: Five books of the King James Bible (Genesis, Exodus, Ruth, Matthew, Acts) — chosen for lineage density and verifiable ground truth.
- Stack: Python/Databricks ecosystem — Unity Catalog, Delta Lake, Foundation Model API, LangGraph, MLflow.
- Constraints: Keep plans atomic, context-sized; use .planning/ as single source of truth.

## Out of scope (this milestone)

- Full product UI, multi-tenant auth, automated policy enforcement rules, and production ops are out of scope until later milestones.
