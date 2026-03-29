#!/usr/bin/env python3
"""Rebuild `extraction_provenance` in local DuckDB from exported Enron tables.

Expects `threads`, `entity_mentions`, and `relationships` (optional rel counts).
Default DB: data/graphrag_enron.duckdb or GRAPHRAG_LOCAL_DB.

Usage:
    python scripts/build_extraction_provenance.py
    python scripts/build_extraction_provenance.py path/to/graphrag_enron.duckdb
"""
from __future__ import annotations

import ast
import json
import os
import sys


def _parse_thread_list(raw) -> list[str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    s = str(raw).strip()
    if s.startswith("["):
        try:
            v = json.loads(s)
        except json.JSONDecodeError:
            try:
                v = ast.literal_eval(s)
            except (ValueError, SyntaxError):
                return []
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
    return []


def main() -> None:
    import duckdb

    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb"
    )
    if not os.path.isfile(db_path):
        print(f"ERROR: DuckDB file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(db_path)
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    for req in ("threads", "entity_mentions"):
        if req not in tables:
            print(f"ERROR: required table `{req}` missing in {db_path}", file=sys.stderr)
            sys.exit(1)

    llm = os.environ.get("GRAPHRAG_LLM_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")
    small = os.environ.get(
        "GRAPHRAG_SMALL_LLM_ENDPOINT", "databricks-meta-llama-3-1-8b-instruct"
    )

    con.execute("DROP TABLE IF EXISTS extraction_provenance")

    con.execute(
        f"""
        CREATE TABLE extraction_provenance AS
        WITH entity_counts AS (
          SELECT thread_id, COUNT(DISTINCT entity_id) AS cnt
          FROM entity_mentions
          GROUP BY thread_id
        )
        SELECT
          CAST(uuid() AS VARCHAR) AS extraction_id,
          t.thread_id,
          'entity_extraction' AS step,
          '{llm.replace("'", "''")}' AS model_endpoint,
          'corporate_entity_v1' AS prompt_template_version,
          LENGTH(COALESCE(t.thread_text, ''))::INTEGER AS input_char_count,
          CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 6000 THEN 6000 ELSE NULL END
            AS input_truncated_at,
          COALESCE(ec.cnt, 0)::INTEGER AS output_entity_count,
          0::INTEGER AS output_rel_count,
          NULL::VARCHAR AS error_message,
          NULL::BIGINT AS latency_ms,
          CURRENT_TIMESTAMP AS created_at
        FROM threads t
        LEFT JOIN entity_counts ec USING (thread_id)
        """
    )

    # Relationship counts per thread (explode source_threads if present)
    rel_counts: dict[str, int] = {}
    if "relationships" in tables:
        colinfo = {
            r[1].lower(): r[1]
            for r in con.execute("PRAGMA table_info('relationships')").fetchall()
        }
        st_col = colinfo.get("source_threads")
        if st_col:
            for (raw,) in con.execute(f'SELECT "{st_col}" FROM relationships').fetchall():
                for tid in _parse_thread_list(raw):
                    rel_counts[tid] = rel_counts.get(tid, 0) + 1

    if rel_counts:
        con.execute("CREATE TEMP TABLE _rel_counts(thread_id VARCHAR, cnt INTEGER)")
        con.executemany(
            "INSERT INTO _rel_counts VALUES (?, ?)",
            list(rel_counts.items()),
        )
        con.execute(
            f"""
            INSERT INTO extraction_provenance
            SELECT
              CAST(uuid() AS VARCHAR),
              t.thread_id,
              'relationship_extraction',
              '{llm.replace("'", "''")}',
              'corporate_relationship_v1',
              LENGTH(COALESCE(t.thread_text, ''))::INTEGER,
              CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 6000 THEN 6000 ELSE NULL END,
              0,
              COALESCE(rc.cnt, 0)::INTEGER,
              NULL::VARCHAR,
              NULL::BIGINT,
              CURRENT_TIMESTAMP
            FROM threads t
            LEFT JOIN _rel_counts rc USING (thread_id)
            """
        )
    else:
        con.execute(
            f"""
            INSERT INTO extraction_provenance
            SELECT
              CAST(uuid() AS VARCHAR),
              t.thread_id,
              'relationship_extraction',
              '{llm.replace("'", "''")}',
              'corporate_relationship_v1',
              LENGTH(COALESCE(t.thread_text, ''))::INTEGER,
              CASE WHEN LENGTH(COALESCE(t.thread_text, '')) > 6000 THEN 6000 ELSE NULL END,
              0,
              0,
              NULL::VARCHAR,
              NULL::BIGINT,
              CURRENT_TIMESTAMP
            FROM threads t
            """
        )

    # Summarization rows (column may be missing in old exports)
    ti = {r[1].lower(): r[1] for r in con.execute("PRAGMA table_info('threads')").fetchall()}
    if "summary" in ti:
        scol = ti["summary"]
        con.execute(
            f"""
            INSERT INTO extraction_provenance
            SELECT
              CAST(uuid() AS VARCHAR),
              thread_id,
              'thread_summarization',
              '{small.replace("'", "''")}',
              'thread_summary_v1',
              LENGTH(COALESCE(thread_text, ''))::INTEGER,
              CASE WHEN LENGTH(COALESCE(thread_text, '')) > 4000 THEN 4000 ELSE NULL END,
              NULL::INTEGER,
              NULL::INTEGER,
              NULL::VARCHAR,
              NULL::BIGINT,
              CURRENT_TIMESTAMP
            FROM threads
            WHERE "{scol}" IS NOT NULL AND TRIM(CAST("{scol}" AS VARCHAR)) <> ''
            """
        )

    n = con.execute("SELECT COUNT(*) FROM extraction_provenance").fetchone()[0]
    print(f"extraction_provenance: {n:,} rows in {db_path}")
    con.close()


if __name__ == "__main__":
    main()
