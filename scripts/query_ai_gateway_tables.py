"""Query AI Gateway system tables and inference tables for the GraphRAG endpoint.

Provides quick access to:
  - Usage tracking (system.serving.endpoint_usage + served_entities)
  - Payload logs (inference tables in the project catalog/schema)
  - Cost attribution and latency analysis

Usage:
    python scripts/query_ai_gateway_tables.py usage       # recent usage metrics
    python scripts/query_ai_gateway_tables.py payloads    # recent request/response payloads
    python scripts/query_ai_gateway_tables.py stats       # aggregated statistics
    python scripts/query_ai_gateway_tables.py all         # all of the above
"""
import argparse
import sys

from databricks.sdk import WorkspaceClient

ENDPOINT_NAME = "graphrag-enron-agent"
CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_enron"
INFERENCE_TABLE_PREFIX = "graphrag_gw"
INFERENCE_TABLE = f"{CATALOG}.{SCHEMA}.{INFERENCE_TABLE_PREFIX}_payload"
LEGACY_INFERENCE_TABLE = f"{CATALOG}.{SCHEMA}.graphrag_agent_payload"


def _run_sql(w: WorkspaceClient, sql: str, label: str) -> list[dict]:
    """Execute SQL via the statement execution API and print results."""
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  SQL: {sql[:120]}{'...' if len(sql) > 120 else ''}\n")

    try:
        result = w.statement_execution.execute_statement(
            warehouse_id=_get_warehouse_id(w),
            statement=sql,
            wait_timeout="30s",
        )
        if result.status and result.status.state and "FAILED" in str(result.status.state):
            error = result.status.error if result.status.error else "Unknown error"
            print(f"  Query failed: {error}")
            return []

        if not result.manifest or not result.manifest.schema or not result.result:
            print("  No results returned.")
            return []

        columns = [c.name for c in result.manifest.schema.columns]
        rows = []
        for chunk in (result.result.data_array or []):
            row = dict(zip(columns, chunk))
            rows.append(row)

        if not rows:
            print("  No rows found.")
            return []

        col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in columns}
        header = " | ".join(c.ljust(col_widths[c]) for c in columns)
        print(f"  {header}")
        print(f"  {'-+-'.join('-' * col_widths[c] for c in columns)}")
        for row in rows[:25]:
            line = " | ".join(str(row.get(c, "")).ljust(col_widths[c]) for c in columns)
            print(f"  {line}")

        if len(rows) > 25:
            print(f"  ... and {len(rows) - 25} more rows")

        print(f"\n  Total: {len(rows)} rows")
        return rows

    except Exception as e:
        print(f"  Error executing query: {e}")
        return []


_warehouse_id_cache = None

def _get_warehouse_id(w: WorkspaceClient) -> str:
    global _warehouse_id_cache
    if _warehouse_id_cache:
        return _warehouse_id_cache

    for wh in w.warehouses.list():
        if wh.name and "Starter" in wh.name:
            _warehouse_id_cache = wh.id
            return wh.id

    for wh in w.warehouses.list():
        if wh.state and "RUNNING" in str(wh.state):
            _warehouse_id_cache = wh.id
            return wh.id

    warehouses = list(w.warehouses.list())
    if warehouses:
        _warehouse_id_cache = warehouses[0].id
        return warehouses[0].id

    raise RuntimeError("No SQL warehouse found. Create one or set warehouse_id manually.")


def query_usage(w: WorkspaceClient) -> None:
    """Query recent usage from system tables."""
    _run_sql(w, f"""
        SELECT
            eu.request_time,
            eu.status_code,
            eu.input_token_count,
            eu.output_token_count,
            eu.request_streaming,
            eu.requester
        FROM system.serving.endpoint_usage AS eu
        JOIN system.serving.served_entities AS se
            ON eu.served_entity_id = se.served_entity_id
        WHERE se.endpoint_name = '{ENDPOINT_NAME}'
        ORDER BY eu.request_time DESC
        LIMIT 20
    """, f"Recent Usage — {ENDPOINT_NAME}")


def query_payloads(w: WorkspaceClient) -> None:
    """Query recent payloads from the inference table (AI Gateway or legacy)."""
    rows = _run_sql(w, f"""
        SELECT
            request_time,
            status_code,
            execution_duration_ms,
            requester,
            LEFT(request, 200) AS request_preview,
            LEFT(response, 200) AS response_preview
        FROM {INFERENCE_TABLE}
        ORDER BY request_time DESC
        LIMIT 10
    """, f"Recent Payloads — {INFERENCE_TABLE}")

    if not rows:
        _run_sql(w, f"""
            SELECT
                request_time,
                status_code,
                execution_duration_ms,
                requester,
                LEFT(request, 200) AS request_preview,
                LEFT(response, 200) AS response_preview
            FROM {LEGACY_INFERENCE_TABLE}
            ORDER BY request_time DESC
            LIMIT 10
        """, f"Recent Payloads (legacy) — {LEGACY_INFERENCE_TABLE}")


def query_stats(w: WorkspaceClient) -> None:
    """Show aggregated statistics."""
    _run_sql(w, f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN eu.status_code = 200 THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN eu.status_code != 200 THEN 1 ELSE 0 END) AS error_count,
            ROUND(AVG(eu.input_token_count), 0) AS avg_input_tokens,
            ROUND(AVG(eu.output_token_count), 0) AS avg_output_tokens,
            SUM(eu.input_token_count) AS total_input_tokens,
            SUM(eu.output_token_count) AS total_output_tokens,
            MIN(eu.request_time) AS first_request,
            MAX(eu.request_time) AS last_request
        FROM system.serving.endpoint_usage AS eu
        JOIN system.serving.served_entities AS se
            ON eu.served_entity_id = se.served_entity_id
        WHERE se.endpoint_name = '{ENDPOINT_NAME}'
    """, f"Aggregated Stats — {ENDPOINT_NAME}")

    _run_sql(w, f"""
        SELECT
            DATE(eu.request_time) AS day,
            COUNT(*) AS requests,
            SUM(eu.input_token_count + eu.output_token_count) AS total_tokens,
            SUM(CASE WHEN eu.status_code != 200 THEN 1 ELSE 0 END) AS errors
        FROM system.serving.endpoint_usage AS eu
        JOIN system.serving.served_entities AS se
            ON eu.served_entity_id = se.served_entity_id
        WHERE se.endpoint_name = '{ENDPOINT_NAME}'
        GROUP BY DATE(eu.request_time)
        ORDER BY day DESC
        LIMIT 14
    """, f"Daily Breakdown — {ENDPOINT_NAME}")


def main():
    parser = argparse.ArgumentParser(
        description="Query AI Gateway tables for the GraphRAG endpoint"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["usage", "payloads", "stats", "all"],
        help="What to query (default: all)",
    )
    args = parser.parse_args()

    w = WorkspaceClient()
    print(f"Querying AI Gateway tables for endpoint: {ENDPOINT_NAME}")

    if args.command in ("usage", "all"):
        query_usage(w)
    if args.command in ("payloads", "all"):
        query_payloads(w)
    if args.command in ("stats", "all"):
        query_stats(w)


if __name__ == "__main__":
    main()
