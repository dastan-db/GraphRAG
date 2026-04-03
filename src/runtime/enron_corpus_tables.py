"""Canonical Enron Delta table names for local DuckDB export and Lakebase sync.

``export_local_data.py`` (DuckDB) and ``scripts/setup_lakebase.py`` (Postgres) must
cover the **same** set so ``GRAPHRAG_BACKEND=local`` and ``GRAPHRAG_BACKEND=lakebase``
see the same corpus shape for tools and evals.

**Schema and data** should match **Delta** (see ``enron_corpus_load.py``): same
table names, same column types (e.g. email recipients as arrays in DuckDB and
``TEXT[]`` in Postgres), and full refresh from the same warehouse queries.
"""

from __future__ import annotations

# Order: graph core → email → analytics → org / investigation → governance / meta
ENRON_CORPUS_TABLE_NAMES: tuple[str, ...] = (
    "entities",
    "relationships",
    "emails",
    "entity_analytics",
    "entity_paths",
    "entity_mentions",
    "entity_aliases",
    "communication_dyads",
    "person_activity",
    "participants",
    "org_hierarchy",
    "org_hierarchy_evidence",
    "investigation_timeline",
    "person_identity",
    "ontology_registry",
    "corpus_coverage",
    "extraction_provenance",
    "entity_resolution_audit",
    "email_classification",
    "data_quality_report",
    "person_role_timeline",
    "topic_taxonomy",
    "pipeline_lineage",
    "threads",
)
