"""Export GraphRAG Delta tables to a local DuckDB database for offline development.

Connects to Databricks via the SDK's Statement Execution API, reads each table,
and writes the rows into a local DuckDB file. Run this whenever the graph schema
or data changes.

Usage:
    python scripts/export_local_data.py
    python scripts/export_local_data.py --output data/graphrag.duckdb

Prerequisites:
    pip install duckdb databricks-sdk
    Databricks auth configured (DATABRICKS_HOST + DATABRICKS_TOKEN or ~/.databrickscfg)
"""
import argparse
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
    "org_hierarchy",
    "investigation_timeline",
]

TABLES = BIBLE_TABLES


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


def _fetch_table(w, warehouse_id: str, table_name: str, schema: str = None) -> tuple[list[str], list[list]]:
    """Fetch all rows from a Delta table via Statement Execution API."""
    import time
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
    for _ in range(30):
        if result.status.state in (StatementState.SUCCEEDED, StatementState.FAILED, StatementState.CANCELED):
            break
        time.sleep(2)
        result = w.statement_execution.get_statement(result.statement_id)

    if result.status.state != StatementState.SUCCEEDED:
        msg = result.status.error.message if result.status.error else f"state={result.status.state}"
        print(f" FAILED: {msg}")
        return [], []

    if not result.manifest or not result.result:
        print(" (empty)")
        return [], []

    columns = [col.name for col in result.manifest.schema.columns]
    rows = result.result.data_array or []
    print(f" {len(rows)} rows, {len(columns)} columns")
    return columns, rows


def _write_to_duckdb(db_path: str, table_name: str, columns: list[str], rows: list[list]):
    """Write rows into a DuckDB table, replacing any existing data."""
    import duckdb

    conn = duckdb.connect(db_path)
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
    conn.execute(f"CREATE TABLE {table_name} ({col_defs})")

    if rows:
        placeholders = ", ".join(["?"] * len(columns))
        conn.executemany(
            f"INSERT INTO {table_name} VALUES ({placeholders})",
            rows,
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
            columns, rows = _fetch_table(w, warehouse_id, table, schema=schema)
            if columns:
                _write_to_duckdb(output_path, table, columns, rows)
        except Exception as exc:
            print(f"    -> {table}: SKIPPED ({exc})")

    print(f"\nDone. Local database: {output_path}")
    print("Run your agent with: GRAPHRAG_BACKEND=local python scripts/test_local.py")


if __name__ == "__main__":
    main()
