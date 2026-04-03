"""Compare row counts across Delta (SQL warehouse), DuckDB export, and Lakebase.

Exits with code 1 if any table differs or is missing from a source (unless skipped).

Examples:
    # Enron (default paths: data/graphrag_enron.duckdb, Lakebase project graphrag)
    python scripts/check_data_parity.py --corpus enron

    python scripts/check_data_parity.py --corpus enron --duckdb data/graphrag_enron.duckdb
    python scripts/check_data_parity.py --corpus bible --skip-lakebase

Environment (same as export / agent):
    GRAPHRAG_CATALOG, GRAPHRAG_ENRON_SCHEMA, GRAPHRAG_SCHEMA (bible),
    DATABRICKS_WAREHOUSE_ID, GRAPHRAG_ENRON_LOCAL_DB / GRAPHRAG_BIBLE_LOCAL_DB
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from src.runtime.enron_corpus_tables import ENRON_CORPUS_TABLE_NAMES

CATALOG = os.environ.get("GRAPHRAG_CATALOG", "serverless_8e8gyh_catalog")
BIBLE_SCHEMA = os.environ.get("GRAPHRAG_SCHEMA", "graphrag_bible")
ENRON_SCHEMA = os.environ.get("GRAPHRAG_ENRON_SCHEMA", "graphrag_enron")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")
LAKEBASE_PROJECT_ID = os.environ.get("GRAPHRAG_LAKEBASE_PROJECT_ID", "graphrag")

# Bible tables present in both export and setup_lakebase (Lakebase has no agent_prompts).
BIBLE_PARITY_TABLES: tuple[str, ...] = (
    "entities",
    "relationships",
    "verses",
    "entity_analytics",
    "entity_mentions",
)


def _default_duckdb_path(corpus: str) -> str:
    return "data/graphrag_enron.duckdb" if corpus == "enron" else "data/graphrag.duckdb"


def _resolve_duckdb_path(corpus: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    key = f"GRAPHRAG_{corpus.upper()}_LOCAL_DB"
    return os.environ.get(key) or _default_duckdb_path(corpus)


def _get_warehouse_id(w: WorkspaceClient) -> str:
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    warehouses = list(w.warehouses.list())
    running = [wh for wh in warehouses if str(wh.state) == "RUNNING"]
    target = running[0] if running else warehouses[0] if warehouses else None
    if target is None:
        raise RuntimeError("No SQL warehouse found; set DATABRICKS_WAREHOUSE_ID")
    return target.id


def _count_delta(
    w: WorkspaceClient,
    warehouse_id: str,
    sql: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
) -> tuple[int | None, str | None]:
    # Statement API: wait_timeout must be 0 or 5–50 seconds.
    kwargs: dict = {"warehouse_id": warehouse_id, "statement": sql, "wait_timeout": "50s"}
    if catalog is not None:
        kwargs["catalog"] = catalog
    if schema is not None:
        kwargs["schema"] = schema
    resp = w.statement_execution.execute_statement(**kwargs)
    if resp.status and resp.status.state == StatementState.FAILED:
        err = resp.status.error.message if resp.status.error else str(resp.status.state)
        return None, err
    if not resp.result or not resp.result.data_array:
        return 0, None
    try:
        return int(resp.result.data_array[0][0]), None
    except (TypeError, ValueError, IndexError):
        return None, "unexpected COUNT result"


def _count_duckdb(db_path: str, table: str) -> tuple[int | None, str | None]:
    try:
        import duckdb
    except ImportError as e:
        return None, f"duckdb not installed: {e}"
    if not Path(db_path).is_file():
        return None, f"file not found: {db_path}"
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            return int(n), None
        finally:
            con.close()
    except Exception as e:
        return None, str(e)


def _lakebase_endpoint_name() -> str:
    return f"projects/{LAKEBASE_PROJECT_ID}/branches/production/endpoints/primary"


def _count_lakebase(pg_schema_sql: str) -> tuple[int | None, str | None]:
    try:
        import psycopg
    except ImportError as e:
        return None, f"psycopg not installed: {e}"
    try:
        w = WorkspaceClient()
        endpoint_name = _lakebase_endpoint_name()
        endpoint = w.postgres.get_endpoint(name=endpoint_name)
        host = endpoint.status.hosts.host
        cred = w.postgres.generate_database_credential(endpoint=endpoint_name)
        username = w.current_user.me().user_name
        conn = psycopg.connect(
            host=host,
            dbname="databricks_postgres",
            user=username,
            password=cred.token,
            sslmode="require",
        )
        try:
            with conn.cursor() as cur:
                cur.execute(pg_schema_sql)
                row = cur.fetchone()
                return (int(row[0]) if row else 0), None
        finally:
            conn.close()
    except Exception as e:
        return None, str(e)


def _row_ok(
    d_val: str,
    k_val: str,
    l_val: str,
    *,
    skip_delta: bool,
    skip_duck: bool,
    skip_lakebase: bool,
) -> tuple[bool, list[str]]:
    """Return (all_ok, tags) for one table row."""
    nums: list[str] = []
    if not skip_delta:
        if d_val == "ERR":
            return False, ["delta"]
        if d_val != "—":
            nums.append(d_val)
    if not skip_duck:
        if k_val == "ERR":
            return False, ["duckdb"]
        if k_val != "—":
            nums.append(k_val)
    if not skip_lakebase:
        if l_val == "ERR":
            return False, ["lakebase"]
        if l_val != "—":
            nums.append(l_val)
    if len(nums) >= 2 and len(set(nums)) > 1:
        return False, ["count_mismatch"]
    return True, []


def _run(
    corpus: str,
    duckdb_path: str,
    skip_delta: bool,
    skip_duck: bool,
    skip_lakebase: bool,
) -> int:
    if corpus == "enron":
        tables = list(ENRON_CORPUS_TABLE_NAMES)
        delta_schema = ENRON_SCHEMA
        delta_fqn = lambda t: f"{CATALOG}.{ENRON_SCHEMA}.{t}"
        lakebase_sql = lambda t: f"SELECT COUNT(*) FROM enron.{t}"
    else:
        tables = list(BIBLE_PARITY_TABLES)
        delta_schema = BIBLE_SCHEMA
        delta_fqn = lambda t: f"{CATALOG}.{BIBLE_SCHEMA}.{t}"
        lakebase_sql = lambda t: f'SELECT COUNT(*) FROM "{t}"'

    w = WorkspaceClient() if not skip_delta else None
    warehouse_id = _get_warehouse_id(w) if w else ""

    mismatches = 0

    print(f"Corpus={corpus}  catalog={CATALOG}  tables={len(tables)}")
    print(f"DuckDB file: {duckdb_path}  (skip={skip_duck})")
    print(f"Lakebase project: {LAKEBASE_PROJECT_ID}  (skip={skip_lakebase})")
    print()

    hdr = f"{'table':<36} {'delta':>12} {'duckdb':>12} {'lakebase':>12} {'ok':>5}"
    print(hdr)
    print("-" * len(hdr))

    for t in tables:
        d_val: str = "—"
        k_val: str = "—"
        l_val: str = "—"
        err_bits: list[str] = []

        if not skip_delta and w:
            n, err = _count_delta(
                w,
                warehouse_id,
                f"SELECT COUNT(*) AS c FROM {delta_fqn(t)}",
                catalog=CATALOG,
                schema=delta_schema,
            )
            if err:
                d_val = "ERR"
                err_bits.append(f"delta: {err[:120]}")
            elif n is not None:
                d_val = str(n)
            else:
                d_val = "ERR"

        if not skip_duck:
            n, err = _count_duckdb(duckdb_path, t)
            if err:
                k_val = "ERR"
                err_bits.append(f"duckdb: {err[:120]}")
            elif n is not None:
                k_val = str(n)
            else:
                k_val = "ERR"

        if not skip_lakebase:
            n, err = _count_lakebase(lakebase_sql(t))
            if err:
                l_val = "ERR"
                err_bits.append(f"lakebase: {err[:120]}")
            elif n is not None:
                l_val = str(n)
            else:
                l_val = "ERR"

        ok, _ = _row_ok(
            d_val, k_val, l_val,
            skip_delta=skip_delta, skip_duck=skip_duck, skip_lakebase=skip_lakebase,
        )
        ok_str = "yes" if ok else "NO"
        if not ok:
            mismatches += 1

        line = f"{t:<36} {d_val:>12} {k_val:>12} {l_val:>12} {ok_str:>5}"
        print(line)
        if err_bits and not ok:
            for b in err_bits:
                print(f"    {b}")

    print()
    if mismatches:
        print(f"FAILED: {mismatches} table(s) with differing counts or errors.")
        return 1
    print("OK: all compared row counts match.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", choices=("enron", "bible"), default="enron")
    p.add_argument("--duckdb", default=None, help="Path to DuckDB file (default from env or data/...)")
    p.add_argument("--skip-delta", action="store_true", help="Do not query Unity Catalog via warehouse")
    p.add_argument("--skip-duckdb", action="store_true", help="Do not query local DuckDB")
    p.add_argument("--skip-lakebase", action="store_true", help="Do not query Lakebase Postgres")
    args = p.parse_args()

    duck_path = _resolve_duckdb_path(args.corpus, args.duckdb)
    if args.skip_duckdb and not args.skip_delta and not args.skip_lakebase:
        pass  # ok: compare delta vs lakebase only

    code = _run(
        args.corpus,
        duck_path,
        skip_delta=args.skip_delta,
        skip_duck=args.skip_duckdb,
        skip_lakebase=args.skip_lakebase,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
