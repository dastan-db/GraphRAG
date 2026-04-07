#!/usr/bin/env python3
"""Pre-deploy smoke tests for the GraphRAG web app.

Two layers:
  Layer 1 — Import: modules load, SDK classes resolve
  Layer 2 — Render: Dash components instantiate with valid props

Catches both import errors (JobsEnvironmentSpec) and prop errors
(Spinner className) before code reaches the deployed app.

Usage:
    python tests/test_app_backend.py           # standalone
    pytest tests/test_app_backend.py -v        # via pytest
"""
import importlib
import sys
from pathlib import Path

# Ensure src/app is on the path so backend.* imports resolve
_APP_DIR = Path(__file__).resolve().parent.parent / "src" / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


# ── Layer 1: Import tests ──────────────────────────────────────────

def test_pipeline_client_imports():
    """pipeline_client.py must import without errors."""
    mod = importlib.import_module("backend.pipeline_client")
    assert hasattr(mod, "submit_add_books")
    assert hasattr(mod, "submit_remove_books")
    assert hasattr(mod, "get_run_status")
    assert callable(mod.submit_add_books)


def test_pipeline_client_environment_classes():
    """The SDK classes used for serverless environments must resolve."""
    from databricks.sdk.service.jobs import JobEnvironment

    env = JobEnvironment.from_dict({
        "environment_key": "default",
        "spec": {
            "client": "2",
            "dependencies": ["mlflow>=3.0", "networkx"],
        },
    })
    assert env.environment_key == "default"
    assert env.spec is not None


def test_agent_client_imports():
    """agent_client.py must import without errors."""
    mod = importlib.import_module("backend.agent_client")
    assert hasattr(mod, "query_agent_enron")


def test_all_page_modules_import():
    """Every page module must import cleanly."""
    page_modules = [
        "pages.home",
        "pages.how_it_works",
        "pages.architecture",
        "pages.corporate_demo",
        "pages.apply",
    ]
    for mod_name in page_modules:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"{mod_name} failed to import"


# ── Layer 2: Component render tests ────────────────────────────────
#
# Dash validates component props at construction time. Instantiating
# a layout function exercises every component tree and catches invalid
# kwargs (e.g. Spinner(className=...)) before deployment.

def _render_tree(component) -> dict:
    """Recursively render a Dash component tree — forces prop validation on every node."""
    from dash.development.base_component import Component
    result = component.to_plotly_json()
    children = result.get("props", {}).get("children")
    if isinstance(children, Component):
        _render_tree(children)
    elif isinstance(children, (list, tuple)):
        for child in children:
            if isinstance(child, Component):
                _render_tree(child)
    return result


def test_home_layout_renders():
    """Home page layout must build without prop errors."""
    from pages.home import home_layout
    _render_tree(home_layout())


def test_how_it_works_layout_renders():
    """How It Works layout must build without prop errors."""
    from pages.how_it_works import how_layout
    _render_tree(how_layout())


def test_architecture_layout_renders():
    """Architecture layout must build without prop errors."""
    from pages.architecture import arch_layout
    _render_tree(arch_layout())


def test_corporate_demo_layout_renders():
    """Corporate demo layout must build without prop errors."""
    from pages.corporate_demo import corporate_demo_layout
    _render_tree(corporate_demo_layout())


def test_apply_layout_renders():
    """Apply layout must build without prop errors."""
    from pages.apply import apply_layout
    _render_tree(apply_layout())


# ── Runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_tests = [fn for name, fn in sorted(globals().items())
                 if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    for t in all_tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"  RESULTS: {passed}/{passed + failed} passed, {failed} failed")
    print(f"{'=' * 50}")
    sys.exit(1 if failed else 0)
