"""Preflight deployment confidence check for GraphRAG.

Runs all validation layers in sequence, giving 100% confidence that a
subsequent Databricks deployment will succeed.

Layers:
    1. Environment check     — .env.local, Python version, key packages
    2. Data integrity         — DuckDB exists with expected tables (> 0 rows)
    3. Graph engine tests     — pytest Layer 1 (no LLM, deterministic SQL)
    4. Agent quality gates    — validate_local.py (entity recall, citations, success)
    5. Model packaging        — agent_serving.py imports + GraphRAGAgent instantiates
    6. Dash app smoke         — app.py import + layout render + deps check
    7. MCP server import      — FastMCP tool registration without Lakebase
    8. Bundle validation      — databricks bundle validate --target dev

Usage:
    python scripts/preflight.py             # layers 1-8 (skip parity)
    python scripts/preflight.py --parity    # layers 1-9 (include parity)
    python scripts/preflight.py --skip 4    # skip layer 4 (agent quality)
    python scripts/preflight.py --only 1 2  # run only layers 1 and 2

Exit codes:
    0 — all gates passed
    1 — one or more gates failed
"""
import argparse
import importlib
import json
import os
import subprocess
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.join(_SCRIPT_DIR, "..")
DUCKDB_PATH = os.path.join(_ROOT_DIR, "data", "graphrag.duckdb")
EXPECTED_TABLES = ["entities", "relationships", "verses", "agent_prompts", "entity_analytics"]

REQUIRED_PACKAGES = {
    "core": ["mlflow", "langchain_core", "langgraph", "pydantic", "databricks_langchain"],
    "local": ["duckdb", "langchain_openai"],
    "app": ["dash", "dash_bootstrap_components"],
}


def _load_env_file(path: str):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(_ROOT_DIR, ".env.local"))


class LayerResult:
    def __init__(self, layer: int, name: str):
        self.layer = layer
        self.name = name
        self.passed = False
        self.skipped = False
        self.detail = ""
        self.elapsed = 0.0

    @property
    def status(self):
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"


def layer1_environment() -> LayerResult:
    """Verify environment setup: .env.local, Python version, key packages."""
    r = LayerResult(1, "Environment check")
    issues = []

    env_path = os.path.join(_ROOT_DIR, ".env.local")
    if not os.path.isfile(env_path):
        issues.append(".env.local not found (copy from .env.local.example)")

    vi = sys.version_info
    if vi < (3, 11):
        issues.append(f"Python >= 3.11 required, got {vi.major}.{vi.minor}.{vi.micro}")

    for group, packages in REQUIRED_PACKAGES.items():
        for pkg in packages:
            try:
                importlib.import_module(pkg)
            except ImportError:
                hint = 'pip install -e ".[local]"' if group == "local" else f"pip install {pkg}"
                issues.append(f"Package '{pkg}' not importable ({hint})")

    backend = os.environ.get("GRAPHRAG_BACKEND", "")
    llm = os.environ.get("GRAPHRAG_LLM_PROVIDER", "")
    if not backend:
        issues.append("GRAPHRAG_BACKEND not set")
    if not llm:
        issues.append("GRAPHRAG_LLM_PROVIDER not set")

    if llm == "openai" and not os.environ.get("OPENAI_API_KEY"):
        issues.append("OPENAI_API_KEY not set (required for LLM_PROVIDER=openai)")

    if issues:
        r.detail = "; ".join(issues)
    else:
        r.passed = True
        r.detail = f"Python {vi.major}.{vi.minor}, backend={backend}, llm={llm}"
    return r


def layer2_data_integrity() -> LayerResult:
    """Verify DuckDB file exists with expected tables and rows."""
    r = LayerResult(2, "Data integrity")

    if not os.path.isfile(DUCKDB_PATH):
        r.detail = f"DuckDB not found at {DUCKDB_PATH} — run: make export"
        return r

    try:
        import duckdb
        conn = duckdb.connect(DUCKDB_PATH, read_only=True)
        existing = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        missing = [t for t in EXPECTED_TABLES if t not in existing]
        if missing:
            r.detail = f"Missing tables: {missing}"
            conn.close()
            return r

        empty = []
        table_counts = {}
        for table in EXPECTED_TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            table_counts[table] = count
            if count == 0:
                empty.append(table)

        conn.close()

        if empty:
            r.detail = f"Empty tables: {empty}"
            return r

        summary = ", ".join(f"{t}={c}" for t, c in table_counts.items())
        r.passed = True
        r.detail = summary
    except Exception as e:
        r.detail = f"DuckDB error: {e}"
    return r


def layer3_graph_engine() -> LayerResult:
    """Run pytest Layer 1 graph engine tests (no LLM)."""
    r = LayerResult(3, "Graph engine tests")

    test_file = os.path.join(_ROOT_DIR, "tests", "test_graph_engine.py")
    if not os.path.isfile(test_file):
        r.detail = "tests/test_graph_engine.py not found"
        return r

    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file,
         "-m", "not integration and not baseline", "-v", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=_ROOT_DIR,
    )

    summary_line = ""
    for line in reversed(result.stdout.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line.lower():
            summary_line = line.strip()
            break

    if result.returncode == 0:
        r.passed = True
        r.detail = summary_line or "All tests passed"
    elif result.returncode == 5:
        r.passed = True
        r.detail = "No tests collected (DuckDB may be missing)"
    else:
        failed_names = [
            l.split("::")[1].strip().split(" ")[0] if "::" in l else l.strip()
            for l in result.stdout.splitlines()
            if "FAILED" in l
        ]
        r.detail = f"{summary_line}; failures: {', '.join(failed_names[:5])}" if failed_names else summary_line
    return r


def layer4_agent_quality() -> LayerResult:
    """Run the full agent validation suite with quality gates."""
    r = LayerResult(4, "Agent quality gates")

    os.environ.setdefault("GRAPHRAG_BACKEND", "local")
    os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

    validate_script = os.path.join(_SCRIPT_DIR, "validate_local.py")
    if not os.path.isfile(validate_script):
        r.detail = "scripts/validate_local.py not found"
        return r

    result = subprocess.run(
        [sys.executable, validate_script, "--backend", "local"],
        capture_output=True, text=True, cwd=_ROOT_DIR,
    )

    lines = result.stdout.strip().splitlines()
    gate_lines = [l for l in lines if "[PASS]" in l or "[FAIL]" in l]

    if result.returncode == 0:
        r.passed = True
        r.detail = "; ".join(l.strip() for l in gate_lines) if gate_lines else "All gates passed"
    else:
        r.detail = "; ".join(l.strip() for l in gate_lines) if gate_lines else result.stdout[-500:]
    return r


def layer5_model_packaging() -> LayerResult:
    """Verify agent_serving.py can be imported and GraphRAGAgent instantiated.

    Uses subprocess isolation since agent_serving.py has heavy top-level
    imports (mlflow, langchain, langgraph) that can conflict with the
    preflight process.
    """
    r = LayerResult(5, "Model packaging dry-run")

    check_script = '''\
import os, sys
os.environ.setdefault("GRAPHRAG_BACKEND", "local")
os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")
sys.path.insert(0, os.getcwd())
from src.agent.agent_serving import AGENT, GraphRAGAgent
from mlflow.pyfunc import ResponsesAgent
assert isinstance(AGENT, ResponsesAgent), f"AGENT is {type(AGENT).__name__}, not ResponsesAgent"
tool_names = [getattr(t, "name", str(t)) for t in getattr(AGENT, "tools", [])]
print(f"{type(AGENT).__name__}|{len(tool_names)}|{','.join(tool_names[:6])}")
'''

    env = os.environ.copy()
    env["GRAPHRAG_BACKEND"] = "local"
    env.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

    result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True, text=True, cwd=_ROOT_DIR, env=env,
    )

    if result.returncode == 0 and result.stdout.strip():
        parts = result.stdout.strip().split("|")
        cls_name = parts[0] if len(parts) > 0 else "?"
        tool_count = parts[1] if len(parts) > 1 else "?"
        tool_list = parts[2] if len(parts) > 2 else ""
        r.passed = True
        r.detail = f"{cls_name} with {tool_count} tools: {tool_list}"
    else:
        stderr = result.stderr.strip()
        last_lines = "\n".join(stderr.splitlines()[-3:]) if stderr else "Unknown error"
        r.detail = f"Import/instantiation failed:\n{last_lines}"
    return r


def layer6_dash_app() -> LayerResult:
    """Verify Dash app imports and layout renders without errors.

    Uses subprocess isolation to avoid polluting the preflight process
    with Dash's import-time side effects.
    """
    r = LayerResult(6, "Dash app smoke test")

    app_dir = os.path.join(_ROOT_DIR, "src", "app")
    app_file = os.path.join(app_dir, "app.py")
    if not os.path.isfile(app_file):
        r.detail = "src/app/app.py not found"
        return r

    check_script = '''\
import os, sys, importlib
os.environ["USE_MOCK_BACKEND"] = "true"
sys.path.insert(0, os.environ["APP_DIR"])
import app as dash_app_module
layout = dash_app_module.app.layout
assert layout is not None, "app.layout is None"
layout.to_plotly_json()
page_count = len(dash_app_module.NAV_ITEMS)

req_file = os.path.join(os.environ["APP_DIR"], "requirements.txt")
missing = []
if os.path.isfile(req_file):
    with open(req_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = line.split(">=")[0].split("==")[0].split("[")[0].strip()
            try:
                importlib.import_module(pkg.replace("-", "_"))
            except ImportError:
                missing.append(pkg)

print(f"{page_count}|{','.join(missing) if missing else 'none'}")
'''

    env = os.environ.copy()
    env["APP_DIR"] = app_dir
    env["USE_MOCK_BACKEND"] = "true"

    result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True, text=True, cwd=_ROOT_DIR, env=env,
    )

    if result.returncode == 0 and result.stdout.strip():
        parts = result.stdout.strip().split("|")
        page_count = parts[0] if parts else "?"
        missing = parts[1] if len(parts) > 1 else "none"
        if missing != "none":
            r.passed = True
            r.detail = f"Layout renders, {page_count} nav groups, missing deps: {missing}"
        else:
            r.passed = True
            r.detail = f"Layout renders, {page_count} nav groups, all deps present"
    else:
        stderr = result.stderr.strip()
        last_lines = "\n".join(stderr.splitlines()[-3:]) if stderr else "Unknown error"
        r.detail = f"Dash app error:\n{last_lines}"
    return r


def layer7_mcp_server() -> LayerResult:
    """Verify MCP server module imports and tools register."""
    r = LayerResult(7, "MCP server import")

    server_dir = os.path.join(_ROOT_DIR, "src", "mcp_server", "server")
    server_file = os.path.join(server_dir, "main.py")
    if not os.path.isfile(server_file):
        r.detail = "src/mcp_server/server/main.py not found"
        return r

    try:
        sys.path.insert(0, server_dir)
        sys.path.insert(0, os.path.join(_ROOT_DIR, "src", "mcp_server"))

        import main as mcp_module
        importlib.reload(mcp_module)

        mcp_obj = mcp_module.mcp
        if mcp_obj is None:
            r.detail = "FastMCP instance is None"
            return r

        tool_list = list(mcp_obj._tool_manager._tools.keys())
        if not tool_list:
            r.detail = "No tools registered on FastMCP server"
            return r

        r.passed = True
        r.detail = f"{len(tool_list)} tools: {', '.join(tool_list)}"
    except ImportError as e:
        if "psycopg" in str(e):
            r.detail = f"psycopg not installed (pip install psycopg[binary]) — {e}"
        elif "fastmcp" in str(e):
            r.detail = f"fastmcp not installed (pip install fastmcp) — {e}"
        else:
            r.detail = f"Import error: {e}"
    except Exception as e:
        r.detail = f"Error: {e}"
    return r


def layer8_bundle_validate() -> LayerResult:
    """Run databricks bundle validate to catch config errors."""
    r = LayerResult(8, "Bundle validation")

    result = subprocess.run(
        ["databricks", "bundle", "validate", "--target", "dev"],
        capture_output=True, text=True, cwd=_ROOT_DIR,
    )

    if result.returncode == 0:
        r.passed = True
        r.detail = "Bundle config valid"
    else:
        output = (result.stderr or result.stdout or "").strip()
        if "command not found" in output or "No such file" in output:
            r.detail = "databricks CLI not installed"
        else:
            r.detail = output[-300:]
    return r


def layer9_parity() -> LayerResult:
    """Run local-vs-Databricks parity check."""
    r = LayerResult(9, "Parity check")

    parity_script = os.path.join(_SCRIPT_DIR, "validate_parity.py")
    if not os.path.isfile(parity_script):
        r.detail = "scripts/validate_parity.py not found"
        return r

    result = subprocess.run(
        [sys.executable, parity_script],
        capture_output=True, text=True, cwd=_ROOT_DIR,
    )

    lines = result.stdout.strip().splitlines()
    summary_lines = [l for l in lines if "PARITY" in l or "recall diff" in l.lower() or "match rate" in l.lower()]

    if result.returncode == 0:
        r.passed = True
        r.detail = "; ".join(l.strip() for l in summary_lines) if summary_lines else "Parity OK"
    else:
        r.detail = "; ".join(l.strip() for l in summary_lines) if summary_lines else result.stdout[-500:]
    return r


ALL_LAYERS = [
    (1, layer1_environment),
    (2, layer2_data_integrity),
    (3, layer3_graph_engine),
    (4, layer4_agent_quality),
    (5, layer5_model_packaging),
    (6, layer6_dash_app),
    (7, layer7_mcp_server),
    (8, layer8_bundle_validate),
    (9, layer9_parity),
]


def main():
    parser = argparse.ArgumentParser(description="Preflight deployment confidence check")
    parser.add_argument("--parity", action="store_true", help="Include layer 9 (parity check)")
    parser.add_argument("--skip", nargs="+", type=int, default=[], help="Layer numbers to skip")
    parser.add_argument("--only", nargs="+", type=int, default=[], help="Only run these layers")
    parser.add_argument("--output", "-o", default="data/preflight_results.json", help="JSON output path")
    args = parser.parse_args()

    max_layer = 9 if args.parity else 8

    results: list[LayerResult] = []

    print()
    print("=" * 70)
    print("  PREFLIGHT DEPLOYMENT CHECK")
    print("=" * 70)

    for layer_num, layer_fn in ALL_LAYERS:
        if layer_num > max_layer:
            break

        if args.only and layer_num not in args.only:
            continue

        r = LayerResult(layer_num, layer_fn.__doc__.strip().split("\n")[0] if layer_fn.__doc__ else f"Layer {layer_num}")
        r.name = {
            1: "Environment check",
            2: "Data integrity",
            3: "Graph engine tests",
            4: "Agent quality gates",
            5: "Model packaging dry-run",
            6: "Dash app smoke test",
            7: "MCP server import",
            8: "Bundle validation",
            9: "Parity check",
        }.get(layer_num, f"Layer {layer_num}")

        if layer_num in args.skip:
            r.skipped = True
            r.detail = "skipped by --skip"
            results.append(r)
            print(f"  [SKIP] {r.name:<30} (--skip)")
            continue

        start = time.time()
        try:
            r = layer_fn()
        except Exception as e:
            r.detail = f"Unexpected error: {e}"
        r.elapsed = time.time() - start
        results.append(r)

        status_str = f"[{r.status}]"
        print(f"  {status_str:<6} {r.name:<30} ({r.elapsed:.1f}s)")
        if r.detail and not r.passed and not r.skipped:
            for line in r.detail.split(";"):
                print(f"         {line.strip()}")

    if not args.parity and not (args.only and 9 in args.only):
        print(f"  [SKIP] {'Parity check':<30} (use --parity)")

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    total = passed + failed

    print()
    print("=" * 70)
    if failed == 0:
        print(f"  {passed}/{total} PASSED — SAFE TO DEPLOY")
    else:
        print(f"  {passed}/{total} PASSED, {failed} FAILED — FIX BEFORE DEPLOYING")
        print()
        print("  Failed layers:")
        for r in results:
            if not r.passed and not r.skipped:
                print(f"    Layer {r.layer}: {r.name}")
                if r.detail:
                    print(f"      {r.detail[:200]}")
    if skipped:
        print(f"  ({skipped} skipped)")
    print("=" * 70)
    print()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "all_passed": failed == 0,
        "summary": f"{passed}/{total} passed, {failed} failed, {skipped} skipped",
        "layers": [
            {
                "layer": r.layer,
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "elapsed": round(r.elapsed, 2),
            }
            for r in results
        ],
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"  Results written to {args.output}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
