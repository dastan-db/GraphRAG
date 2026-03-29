"""Unit tests for query entity pre-linking in agent_serving."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_MOCK_MODULES = [
    "mlflow",
    "mlflow.pyfunc",
    "mlflow.types",
    "mlflow.types.responses",
    "mlflow.langchain",
    "mlflow.models",
    "databricks_langchain",
    "langchain_core",
    "langchain_core.messages",
    "langchain_core.tools",
    "langchain_core.runnables",
    "langchain_openai",
    "langchain_ollama",
    "langgraph",
    "langgraph.graph",
    "langgraph.graph.message",
    "langgraph.prebuilt",
    "langgraph.prebuilt.tool_node",
]


@pytest.fixture(autouse=True)
def _mock_imports(monkeypatch):
    """Stub out heavy third-party imports so we can test pure logic."""
    import sys

    mocks = {}
    for mod_name in _MOCK_MODULES:
        if mod_name not in sys.modules:
            mocks[mod_name] = MagicMock()
            monkeypatch.setitem(sys.modules, mod_name, mocks[mod_name])

    _ResponsesAgent = type("ResponsesAgent", (), {})

    pyfunc_mod = sys.modules["mlflow.pyfunc"]
    pyfunc_mod.ResponsesAgent = _ResponsesAgent

    responses_mod = sys.modules["mlflow.types.responses"]
    responses_mod.ResponsesAgent = _ResponsesAgent
    responses_mod.ResponsesAgentRequest = object
    responses_mod.ResponsesAgentResponse = object
    responses_mod.ResponsesAgentStreamEvent = object
    responses_mod.output_to_responses_items_stream = MagicMock()
    responses_mod.to_chat_completions_input = lambda x: x

    lc_messages = sys.modules["langchain_core.messages"]
    lc_messages.AIMessage = type("AIMessage", (), {})

    lc_tools = sys.modules["langchain_core.tools"]
    lc_tools.tool = lambda f: f

    typing_mod = sys.modules["langgraph.graph.message"]
    typing_mod.add_messages = None

    sys.modules["mlflow"].langchain = MagicMock()
    sys.modules["mlflow"].models = MagicMock()

    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_mod():
    import importlib
    import src.agent.agent_serving as mod
    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests — _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_simple_name(self):
        from src.agent.agent_serving import _slugify
        assert _slugify("Abraham") == "abraham"

    def test_multi_word(self):
        from src.agent.agent_serving import _slugify
        assert _slugify("Holy Spirit") == "holy_spirit"

    def test_special_chars(self):
        from src.agent.agent_serving import _slugify
        assert _slugify("God's Covenant!") == "god_s_covenant"


# ---------------------------------------------------------------------------
# Tests — extract_query_entities
# ---------------------------------------------------------------------------

class TestExtractQueryEntities:
    def test_parses_json_array(self):
        fake_response = SimpleNamespace(
            content=json.dumps([
                {"name": "Arabs", "entity_type": "Group"},
                {"name": "Abraham", "entity_type": "Person"},
            ])
        )
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response
        with patch("src.agent.agent_serving._get_llm", return_value=mock_llm):
            from src.agent.agent_serving import extract_query_entities
            result = extract_query_entities("Who is the father of all Arabs?")

        assert len(result) == 2
        assert result[0]["name"] == "Arabs"
        assert result[1]["name"] == "Abraham"

    def test_strips_markdown_fences(self):
        fake_response = SimpleNamespace(
            content='```json\n[{"name": "Moses", "entity_type": "Person"}]\n```'
        )
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response
        with patch("src.agent.agent_serving._get_llm", return_value=mock_llm):
            from src.agent.agent_serving import extract_query_entities
            result = extract_query_entities("Tell me about Moses")

        assert len(result) == 1
        assert result[0]["name"] == "Moses"

    def test_returns_empty_on_bad_json(self):
        fake_response = SimpleNamespace(content="I cannot extract entities.")
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response
        with patch("src.agent.agent_serving._get_llm", return_value=mock_llm):
            from src.agent.agent_serving import extract_query_entities
            result = extract_query_entities("some question")

        assert result == []


# ---------------------------------------------------------------------------
# Tests — pre_lookup_entities (returns tuple)
# ---------------------------------------------------------------------------

class TestPreLookupEntities:
    def test_all_not_found(self):
        mod = _reload_mod()
        mock_backend = MagicMock()
        mock_backend.execute_sql.return_value = []
        with patch.object(mod, "_backend", mock_backend):
            found, not_found = mod.pre_lookup_entities(["Arabs", "father"])

        assert found == []
        assert not_found == ["Arabs", "father"]

    def test_all_found(self):
        mod = _reload_mod()
        rows = [{"entity_id": "abraham", "name": "Abraham", "entity_type": "Person"}]
        mock_backend = MagicMock()
        mock_backend.execute_sql.return_value = rows
        with patch.object(mod, "_backend", mock_backend):
            found, not_found = mod.pre_lookup_entities(["Abraham"])

        assert len(found) == 1
        assert "Abraham" in found[0]
        assert not_found == []

    def test_mixed(self):
        mod = _reload_mod()

        def sql_side_effect(query, params=None):
            if params:
                for v in params.values():
                    if "ishmael" in v:
                        return [{"entity_id": "ishmael", "name": "Ishmael", "entity_type": "Person"}]
            return []

        mock_backend = MagicMock()
        mock_backend.execute_sql.side_effect = sql_side_effect
        with patch.object(mod, "_backend", mock_backend):
            found, not_found = mod.pre_lookup_entities(["Ishmael", "Arabs"])

        assert len(found) == 1
        assert "Ishmael" in found[0]
        assert not_found == ["Arabs"]


# ---------------------------------------------------------------------------
# Tests — build_prelookup_context
# ---------------------------------------------------------------------------

class TestBuildPrelookupContext:
    def test_returns_constraint_block_with_not_found(self):
        mod = _reload_mod()
        entities = [{"name": "Arabs", "entity_type": "Group"}]
        with patch.object(mod, "extract_query_entities", return_value=entities), \
             patch.object(mod, "pre_lookup_entities", return_value=([], ["Arabs"])):
            ctx = mod.build_prelookup_context("Who is the father of all Arabs?")

        assert "PRE-LOOKUP RESULTS (DEFINITIVE" in ctx
        assert "NOT IN GRAPH: Arabs" in ctx
        assert "FOUND IN GRAPH: (none)" in ctx
        assert "WRONG" in ctx

    def test_returns_constraint_block_with_found(self):
        mod = _reload_mod()
        entities = [{"name": "Moses", "entity_type": "Person"}]
        with patch.object(mod, "extract_query_entities", return_value=entities), \
             patch.object(mod, "pre_lookup_entities",
                          return_value=(["Moses -> Moses (Person)"], [])):
            ctx = mod.build_prelookup_context("Who is Moses?")

        assert "FOUND IN GRAPH: Moses -> Moses (Person)" in ctx
        assert "NOT IN GRAPH: (none)" in ctx

    def test_returns_empty_when_no_entities_extracted(self):
        mod = _reload_mod()
        with patch.object(mod, "extract_query_entities", return_value=[]):
            ctx = mod.build_prelookup_context("Hello")

        assert ctx == ""

    def test_returns_empty_on_exception(self):
        mod = _reload_mod()
        with patch.object(mod, "extract_query_entities",
                          side_effect=RuntimeError("LLM down")):
            ctx = mod.build_prelookup_context("test")

        assert ctx == ""


# ---------------------------------------------------------------------------
# Tests — dynamic system prompt in _build_graph
# ---------------------------------------------------------------------------

class TestDynamicSystemPrompt:
    def test_build_graph_appends_prelookup_to_system_prompt(self):
        mod = _reload_mod()
        agent = mod.GraphRAGAgent.__new__(mod.GraphRAGAgent)
        agent.llm = MagicMock()
        agent.tools = []
        agent.llm_with_tools = MagicMock()

        prelookup_ctx = "\n\n---\nPRE-LOOKUP RESULTS (DEFINITIVE):\n  NOT IN GRAPH: Arabs\n---"

        compiled = agent._build_graph(prelookup_context=prelookup_ctx)
        assert compiled is not None

    def test_build_graph_works_with_empty_prelookup(self):
        mod = _reload_mod()
        agent = mod.GraphRAGAgent.__new__(mod.GraphRAGAgent)
        agent.llm = MagicMock()
        agent.tools = []
        agent.llm_with_tools = MagicMock()

        compiled = agent._build_graph(prelookup_context="")
        assert compiled is not None
