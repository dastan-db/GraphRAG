#!/usr/bin/env python3
"""Rebuild `data_quality_report` in local DuckDB (column-level nulls and cardinality).

Scans the same logical table set as notebook 07j. Skips missing tables.

Usage:
    python scripts/build_data_quality.py
    python scripts/build_data_quality.py path/to/graphrag_enron.duckdb
"""
from __future__ import annotations

import os
import sys

KNOWN_TABLES = [
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
    "investigation_timeline",
    "threads",
]


def main() -> None:
    import duckdb

    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb"
    )
    if not os.path.isfile(db_path):
        print(f"ERROR: DuckDB file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(db_path)
    existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

    metrics: list[tuple] = []
    for tname in KNOWN_TABLES:
        if tname not in existing:
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info('{tname}')").fetchall()]
        total_rows = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
        for c in cols:
            qcol = f'"{c}"'
            if total_rows == 0:
                metrics.append((tname, c, 0, 0, 0.0, 0, 0.0))
                continue
            null_count = con.execute(
                f'SELECT COUNT(*) FROM "{tname}" WHERE {qcol} IS NULL'
            ).fetchone()[0]
            distinct_count = con.execute(
                f'SELECT COUNT(DISTINCT {qcol}) FROM "{tname}"'
            ).fetchone()[0]
            non_null = total_rows - null_count
            null_rate = 1.0 - (non_null / total_rows) if total_rows else 0.0
            card_ratio = distinct_count / total_rows if total_rows else 0.0
            metrics.append(
                (tname, c, total_rows, null_count, null_rate, distinct_count, card_ratio)
            )

    con.execute("DROP TABLE IF EXISTS data_quality_report")
    con.execute(
        """
        CREATE TABLE data_quality_report (
          table_name VARCHAR,
          column_name VARCHAR,
          refresh_date DATE,
          total_rows BIGINT,
          null_count BIGINT,
          null_rate DOUBLE,
          distinct_count BIGINT,
          cardinality_ratio DOUBLE
        )
        """
    )
    if metrics:
        con.executemany(
            """
            INSERT INTO data_quality_report VALUES (
              ?, ?, CURRENT_DATE, ?, ?, ?, ?, ?
            )
            """,
            [
                (
                    m[0],
                    m[1],
                    m[2],
                    m[3],
                    m[4],
                    m[5],
                    m[6],
                )
                for m in metrics
            ],
        )

    n = con.execute("SELECT COUNT(*) FROM data_quality_report").fetchone()[0]
    print(f"data_quality_report: {n:,} rows in {db_path}")
    con.close()


if __name__ == "__main__":
    main()
