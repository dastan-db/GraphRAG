"""Tests for the GraphRAG MCP Server (graph analytics tools).

Validates that the FastMCP server module imports correctly, all expected
tools are registered, and tool functions handle mock data gracefully.

Prerequisites:
    pip install fastmcp psycopg[binary]

Usage:
    pytest tests/test_mcp_server.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "mcp_server", "server")
MCP_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "mcp_server")

EXPECTED_TOOLS = ["bfs_path", "pagerank_ranking", "cross_testament_analysis", "entity_importance"]


def _fastmcp_available():
    try:
        import fastmcp  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.fixture(autouse=True, scope="module")
def _setup_paths():
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    if MCP_DIR not in sys.path:
        sys.path.insert(0, MCP_DIR)


@pytest.fixture
def mcp_module():
    """Import the MCP server module with psycopg mocked if needed."""
    with patch.dict(os.environ, {"LAKEBASE_ENDPOINT": "test-endpoint", "LAKEBASE_HOST": "localhost"}):
        if "psycopg" not in sys.modules:
            sys.modules["psycopg"] = MagicMock()

        import importlib
        import main as mod
        importlib.reload(mod)
        return mod


@pytest.mark.skipif(not _fastmcp_available(), reason="fastmcp not installed")
class TestMcpServerImport:
    def test_fastmcp_instance_exists(self, mcp_module):
        assert hasattr(mcp_module, "mcp"), "Module should export a FastMCP instance named 'mcp'"
        assert mcp_module.mcp is not None

    def test_all_expected_tools_registered(self, mcp_module):
        tool_names = list(mcp_module.mcp._tool_manager._tools.keys())
        for expected in EXPECTED_TOOLS:
            assert expected in tool_names, f"Tool '{expected}' not registered. Found: {tool_names}"

    def test_tool_count(self, mcp_module):
        tool_names = list(mcp_module.mcp._tool_manager._tools.keys())
        assert len(tool_names) >= len(EXPECTED_TOOLS), (
            f"Expected at least {len(EXPECTED_TOOLS)} tools, got {len(tool_names)}"
        )


@pytest.mark.skipif(not _fastmcp_available(), reason="fastmcp not installed")
class TestMcpHelpers:
    def test_slugify(self, mcp_module):
        assert mcp_module._slugify("Moses") == "moses"
        assert mcp_module._slugify("Jesus Christ") == "jesus_christ"
        assert mcp_module._slugify("God (Yahweh)") == "god_yahweh"

    def test_slugify_strips_special_chars(self, mcp_module):
        assert mcp_module._slugify("A--B") == "a_b"
        assert mcp_module._slugify("  spaces  ") == "spaces"


@pytest.mark.skipif(not _fastmcp_available(), reason="fastmcp not installed")
class TestMcpToolFunctions:
    """Test the raw Python functions behind @mcp.tool() wrappers.

    FastMCP wraps functions into FunctionTool objects; access the
    underlying callable via .fn attribute.
    """

    @staticmethod
    def _call(mcp_module, tool_name, **kwargs):
        tool_obj = getattr(mcp_module, tool_name)
        fn = getattr(tool_obj, "fn", tool_obj)
        return fn(**kwargs)

    @patch("main._query")
    def test_bfs_path_no_results(self, mock_query, mcp_module):
        mock_query.return_value = []
        result = self._call(mcp_module, "bfs_path", source="NonExistent1", target="NonExistent2")
        assert "No path found" in result

    @patch("main._query")
    def test_pagerank_ranking_no_results(self, mock_query, mcp_module):
        mock_query.return_value = []
        result = self._call(mcp_module, "pagerank_ranking", entity_type="UnknownType")
        assert "No entities found" in result

    @patch("main._query")
    def test_cross_testament_no_results(self, mock_query, mcp_module):
        mock_query.return_value = []
        result = self._call(mcp_module, "cross_testament_analysis", source_testament="OT")
        assert "No entities found" in result

    @patch("main._query")
    def test_entity_importance_not_found(self, mock_query, mcp_module):
        mock_query.return_value = []
        result = self._call(mcp_module, "entity_importance", entity_name="NonExistent")
        assert "not found" in result

    @patch("main._query")
    def test_pagerank_with_results(self, mock_query, mcp_module):
        mock_query.return_value = [
            {
                "name": "God",
                "entity_type": "Person",
                "testament": "OT",
                "pagerank": 0.05,
                "total_degree": 200,
                "cross_testament_connections": 50,
            }
        ]
        result = self._call(mcp_module, "pagerank_ranking", limit=1)
        assert "God" in result
        assert "PageRank" in result

    @patch("main._query")
    def test_entity_importance_with_results(self, mock_query, mcp_module):
        mock_query.side_effect = [
            [{"name": "Moses", "entity_type": "Person", "testament": "OT",
              "pagerank": 0.03, "in_degree": 50, "out_degree": 60,
              "total_degree": 110, "cross_testament_connections": 10}],
            [{"rank": 3}],
            [{"cnt": 100}],
        ]
        result = self._call(mcp_module, "entity_importance", entity_name="Moses")
        assert "Moses" in result
        assert "Person" in result


class TestAppYaml:
    def test_app_yaml_exists(self):
        app_yaml = os.path.join(MCP_DIR, "app.yaml")
        assert os.path.isfile(app_yaml), "src/mcp_server/app.yaml not found"

    def test_app_yaml_has_command(self):
        import yaml
        app_yaml = os.path.join(MCP_DIR, "app.yaml")
        if not os.path.isfile(app_yaml):
            pytest.skip("app.yaml not found")
        try:
            with open(app_yaml) as f:
                config = yaml.safe_load(f)
            assert "command" in config, "app.yaml must have a 'command' key"
        except ImportError:
            with open(app_yaml) as f:
                content = f.read()
            assert "command:" in content, "app.yaml must contain 'command:'"
