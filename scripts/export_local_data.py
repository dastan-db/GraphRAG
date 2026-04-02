"""Export GraphRAG Delta tables to a local DuckDB database for offline development.

Connects to Databricks via the SDK's Statement Execution API, reads each table,
and writes the rows into a local DuckDB file.  Column types are inferred from the
Databricks manifest so that numeric, date, and **array** columns get proper DuckDB
types (especially ``VARCHAR[]`` for arrays so that ``array_length`` / ``UNNEST`` /
``array_to_string`` work without JSON workarounds).

Usage:
    python scripts/export_local_data.py --corpus enron
    python scripts/export_local_data.py --corpus bible --output data/graphrag.duckdb

Prerequisites:
    pip install duckdb databricks-sdk
    Databricks auth configured (DATABRICKS_HOST + DATABRICKS_TOKEN or ~/.databrickscfg)
"""
import argparse
import json
import os
import sys

CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
SCHEMA = os.environ.get("GRAPHRAG_SCHEMA", "graphrag_bible")
CORPUS = os.environ.get("GRAPHRAG_CORPUS", "bible")
ENRON_SCHEMA = os.environ.get("GRAPHRAG_ENRON_SCHEMA", "graphrag_enron")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")

BIBLE_TABLES = [
    "entities",
    "relationships",
    "verses",
    "agent_prompts",
    "entity_analytics",
]

ENRON_TABLES = [
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
]

TABLES = BIBLE_TABLES

# Databricks type_name → DuckDB column type.
# ARRAY is handled specially: all arrays become VARCHAR[] (string lists).
_TYPE_MAP = {
    "STRING":        "VARCHAR",
    "BINARY":        "BLOB",
    "BOOLEAN":       "BOOLEAN",
    "BYTE":          "TINYINT",
    "SHORT":         "SMALLINT",
    "INT":           "INTEGER",
    "LONG":          "BIGINT",
    "FLOAT":         "FLOAT",
    "DOUBLE":        "DOUBLE",
    "DATE":          "DATE",
    "TIMESTAMP":     "TIMESTAMP",
    "TIMESTAMP_NTZ": "TIMESTAMP",
    "DECIMAL":       "DOUBLE",
    "ARRAY":         "VARCHAR[]",
    "STRUCT":        "VARCHAR",
    "MAP":           "VARCHAR",
}


def _get_warehouse_id(w):
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    warehouses = list(w.warehouses.list())
    running = [wh for wh in warehouses if str(wh.state) == "RUNNING"]
    target = running[0] if running else warehouses[0] if warehouses else None
    if target is None:
        print("ERROR: No SQL warehouse found in workspace", file=sys.stderr)
        sys.exit(1)
    print(f"  Auto-selected warehouse: {target.name} ({target.id})")
    return target.id


def _resolve_type_name(col) -> str:
    """Extract a plain uppercase type string from a manifest column."""
    tn = col.type_name
    s = tn.value if hasattr(tn, "value") else str(tn)
    return s.rsplit(".", 1)[-1].upper()


def _convert_value(value, type_name: str):
    """Convert a Statement Execution API string value to a Python type."""
    if value is None:
        return None
    if type_name in ("INT", "LONG", "BYTE", "SHORT"):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    if type_name in ("FLOAT", "DOUBLE", "DECIMAL"):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if type_name == "BOOLEAN":
        return str(value).lower() in ("true", "1")
    if type_name == "ARRAY":
        if not value or value == "null":
            return []
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return [s.strip().strip('"').strip("'") for s in value.strip("[]").split(",") if s.strip()]
    return value


def _wait_for_result(w, result):
    """Poll until statement completes."""
    import time
    from databricks.sdk.service.sql import StatementState

    for _ in range(60):
        if result.status.state in (StatementState.SUCCEEDED, StatementState.FAILED, StatementState.CANCELED):
            return result
        time.sleep(2)
        result = w.statement_execution.get_statement(result.statement_id)
    return result


def _fetch_table(w, warehouse_id: str, table_name: str, schema: str = None):
    """Fetch all rows + column metadata from a Delta table via Statement Execution API.

    Falls back to paginated fetching (LIMIT/OFFSET) when the inline byte limit
    is exceeded for large tables like emails or threads.
    """
    from databricks.sdk.service.sql import StatementState

    effective_schema = schema or SCHEMA
    fqn = f"{CATALOG}.{effective_schema}.{table_name}"
    print(f"  Fetching {fqn}...", end="", flush=True)

    result = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=f"SELECT * FROM {fqn}",
        catalog=CATALOG,
        schema=effective_schema,
        wait_timeout="50s",
    )
    result = _wait_for_result(w, result)

    if result.status.state != StatementState.SUCCEEDED:
        msg = result.status.error.message if result.status.error else f"state={result.status.state}"
        if "Inline byte limit exceeded" in str(msg):
            print(f" (too large for inline, using pagination)...", end="", flush=True)
            return _fetch_table_paged(w, warehouse_id, fqn, effective_schema)
        print(f" FAILED: {msg}")
        return [], [], {}

    if not result.manifest or not result.result:
        print(" (empty)")
        return [], [], {}

    columns = [col.name for col in result.manifest.schema.columns]
    col_types = {col.name: _resolve_type_name(col) for col in result.manifest.schema.columns}
    rows = result.result.data_array or []
    type_summary = ", ".join(f"{c}={col_types[c]}" for c in columns if col_types[c] != "STRING")
    print(f" {len(rows)} rows, {len(columns)} columns" + (f" [{type_summary}]" if type_summary else ""))
    return columns, rows, col_types


def _fetch_page(w, warehouse_id: str, fqn: str, effective_schema: str,
                limit: int, offset: int):
    """Fetch a single page; returns (result, ok) with adaptive retry on inline overflow."""
    from databricks.sdk.service.sql import StatementState

    result = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=f"SELECT * FROM {fqn} LIMIT {limit} OFFSET {offset}",
        catalog=CATALOG,
        schema=effective_schema,
        wait_timeout="50s",
    )
    result = _wait_for_result(w, result)
    if result.status.state == StatementState.SUCCEEDED:
        return result, True
    msg = str(result.status.error.message if result.status.error else "")
    if "Inline byte limit exceeded" in msg and limit > 200:
        smaller = max(200, limit // 4)
        print(f"\n    Page {offset}+{limit} too large, retrying with {smaller}...", end="", flush=True)
        return _fetch_page(w, warehouse_id, fqn, effective_schema, smaller, offset)
    return result, False


def _fetch_table_paged(w, warehouse_id: str, fqn: str, effective_schema: str,
                       page_size: int = 2000):
    """Paginated fetch for tables that exceed the inline byte limit."""
    from databricks.sdk.service.sql import StatementState

    count_result = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=f"SELECT COUNT(*) AS cnt FROM {fqn}",
        catalog=CATALOG,
        schema=effective_schema,
        wait_timeout="50s",
    )
    count_result = _wait_for_result(w, count_result)
    total = int(count_result.result.data_array[0][0]) if count_result.result and count_result.result.data_array else 0
    print(f" {total} rows total...", end="", flush=True)

    all_rows = []
    columns = None
    col_types = {}
    offset = 0

    while offset < total:
        result, ok = _fetch_page(w, warehouse_id, fqn, effective_schema, page_size, offset)

        if not ok:
            msg = result.status.error.message if result.status.error else f"state={result.status.state}"
            print(f"\n    Page at offset {offset} FAILED: {msg}")
            offset += page_size
            continue

        if columns is None and result.manifest:
            columns = [col.name for col in result.manifest.schema.columns]
            col_types = {col.name: _resolve_type_name(col) for col in result.manifest.schema.columns}

        page_rows = result.result.data_array or []
        all_rows.extend(page_rows)
        fetched = len(page_rows)
        print(f" +{fetched}", end="", flush=True)
        offset += fetched if fetched > 0 else page_size

        if fetched < page_size:
            break

    type_summary = ", ".join(f"{c}={col_types[c]}" for c in (columns or []) if col_types.get(c, "STRING") != "STRING")
    print(f"\n    Fetched {len(all_rows)} rows, {len(columns or [])} columns" +
          (f" [{type_summary}]" if type_summary else ""))
    return columns or [], all_rows, col_types


def _write_to_duckdb(db_path: str, table_name: str, columns: list[str],
                     rows: list[list], col_types: dict[str, str]):
    """Write rows into a DuckDB table with proper types."""
    import duckdb

    conn = duckdb.connect(db_path)
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    col_defs = ", ".join(
        f'"{c}" {_TYPE_MAP.get(col_types.get(c, "STRING"), "VARCHAR")}'
        for c in columns
    )
    conn.execute(f"CREATE TABLE {table_name} ({col_defs})")

    if rows:
        converted = []
        for row in rows:
            new_row = [_convert_value(row[i], col_types.get(columns[i], "STRING"))
                       for i in range(len(columns))]
            converted.append(new_row)

        placeholders = ", ".join(["?"] * len(columns))
        conn.executemany(
            f"INSERT INTO {table_name} VALUES ({placeholders})",
            converted,
        )

    count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    conn.close()
    print(f"    -> {table_name}: {count} rows in DuckDB")


def main():
    parser = argparse.ArgumentParser(description="Export Delta tables to local DuckDB")
    parser.add_argument(
        "--output", "-o",
        default="data/graphrag.duckdb",
        help="Output DuckDB file path (default: data/graphrag.duckdb)",
    )
    parser.add_argument(
        "--tables", "-t",
        nargs="+",
        default=None,
        help="Tables to export (defaults depend on --corpus)",
    )
    parser.add_argument(
        "--corpus",
        choices=["bible", "enron"],
        default=CORPUS,
        help=f"Corpus to export (default: {CORPUS})",
    )
    args = parser.parse_args()

    if args.corpus == "enron":
        schema = ENRON_SCHEMA
        tables = args.tables or ENRON_TABLES
        default_output = "data/graphrag_enron.duckdb"
    else:
        schema = SCHEMA
        tables = args.tables or BIBLE_TABLES
        default_output = "data/graphrag.duckdb"

    output_path = args.output if args.output != "data/graphrag.duckdb" else default_output
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    warehouse_id = _get_warehouse_id(w)

    print(f"\nExporting {args.corpus} corpus ({len(tables)} tables) to {output_path}:")
    for table in tables:
        try:
            columns, rows, col_types = _fetch_table(w, warehouse_id, table, schema=schema)
            if columns:
                _write_to_duckdb(output_path, table, columns, rows, col_types)
        except Exception as exc:
            print(f"    -> {table}: SKIPPED ({exc})")

    print(f"\nDone. Local database: {output_path}")
    print(f"Run agent with: GRAPHRAG_BACKEND=local GRAPHRAG_CORPUS={args.corpus} "
          f"GRAPHRAG_LOCAL_DB={output_path} python -m src.agent.latency_benchmark")


if __name__ == "__main__":
    main()
