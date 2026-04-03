"""How Enron Delta tables are materialized into DuckDB and Lakebase.

**Unity Catalog Delta is the source of truth.** Local DuckDB and Lakebase should
mirror the same logical schema and row values as Delta:

- **DuckDB** (``scripts/export_local_data.py``): ``SELECT *`` per table via the
  Statement Execution API — no Spark-side transforms; types come from the
  manifest (arrays become ``VARCHAR[]``, etc.).
- **Lakebase** (``scripts/setup_lakebase.py``): same idea — for each Enron table,
  the warehouse query selects the same columns as Delta (``SELECT <columns>``
  matching the Delta table, or ``SELECT *`` for manifest-driven loads). Email
  recipient columns are stored as PostgreSQL ``TEXT[]``, matching Delta
  ``ARRAY<STRING>``, not comma-flattened ``TEXT``.

Keep ``ENRON_CORPUS_TABLE_NAMES`` in ``enron_corpus_tables.py`` aligned with the
tables you actually export/sync so all three stores cover the same graph.

"""

from __future__ import annotations
