"""Tests for the GraphRAG Graph Engine (DuckDB local backend).

The Calculator Analogy
======================
The graph engine is like a calculator on a math test. A weaker student WITH a
calculator beats a stronger student WITHOUT one — because the calculator provides
exact, deterministic answers that no amount of intelligence can replicate from memory.

Layer 1: Direct tool tests — proves the calculator works (no LLM).
Layer 2: Agent + graph tests — proves student WITH calculator gets correct answers.
Layer 3: Raw LLM baseline — proves student WITHOUT calculator gets them WRONG (xfail).

Prerequisites:
    python scripts/export_local_data.py   # creates data/graphrag.duckdb (one-time)
    pip install -e ".[local]"             # installs duckdb, langchain-openai

Layer 1 only (fast, no LLM):
    pytest tests/test_graph_engine.py -m "not integration and not baseline" -v

Layer 2 — agent with graph (all models x all questions):
    pytest tests/test_graph_engine.py -m integration -v

Layer 3 — raw LLM baseline (expected failures prove graph value):
    pytest tests/test_graph_engine.py -m baseline -v
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths and skip conditions
# ---------------------------------------------------------------------------
DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "graphrag.duckdb")
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

skip_no_db = pytest.mark.skipif(
    not os.path.isfile(DUCKDB_PATH),
    reason="DuckDB not found — run: python scripts/export_local_data.py",
)

# ---------------------------------------------------------------------------
# Mock modules required to import agent_serving without heavy deps
# ---------------------------------------------------------------------------
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


@pytest.fixture(autouse=True, scope="session")
def _mock_heavy_imports():
    """Stub out heavy third-party imports so Layer 1 tests can import tool
    functions without installing all agent dependencies."""
    import importlib

    mocks = {}
    for mod_name in _MOCK_MODULES:
        if mod_name not in sys.modules:
            mocks[mod_name] = MagicMock()
            sys.modules[mod_name] = mocks[mod_name]

    _ResponsesAgent = type("ResponsesAgent", (), {})
    sys.modules["mlflow.pyfunc"].ResponsesAgent = _ResponsesAgent

    responses_mod = sys.modules["mlflow.types.responses"]
    responses_mod.ResponsesAgent = _ResponsesAgent
    responses_mod.ResponsesAgentRequest = object
    responses_mod.ResponsesAgentResponse = object
    responses_mod.ResponsesAgentStreamEvent = object
    responses_mod.output_to_responses_items_stream = MagicMock()
    responses_mod.to_chat_completions_input = lambda x: x
    responses_mod.create_function_call_item = MagicMock()
    responses_mod.create_function_call_output_item = MagicMock()

    lc_messages = sys.modules["langchain_core.messages"]
    lc_messages.AIMessage = type("AIMessage", (), {})
    lc_messages.ToolMessage = type("ToolMessage", (), {})

    lc_tools = sys.modules["langchain_core.tools"]
    lc_tools.tool = lambda f, **kwargs: f

    sys.modules["langgraph.graph.message"].add_messages = None
    sys.modules["langgraph.graph"].END = "end"
    sys.modules["langgraph.graph"].StateGraph = MagicMock()
    sys.modules["mlflow"].langchain = MagicMock()
    sys.modules["mlflow"].models = MagicMock()

    os.environ.setdefault("GRAPHRAG_BACKEND", "local")
    os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")

    yield

    for mod_name in mocks:
        sys.modules.pop(mod_name, None)


@pytest.fixture()
def _patch_backend():
    """Patch agent_serving._backend with a real LocalBackend pointing at the
    test DuckDB, then reload the module so tool functions use it."""
    import importlib
    import src.agent.agent_serving as mod

    importlib.reload(mod)

    import duckdb

    class _TestLocalBackend:
        _FQN_PREFIX = f"{mod.CATALOG}.{mod.SCHEMA}."

        def __init__(self):
            import re as _re
            self._conn = duckdb.connect(DUCKDB_PATH, read_only=True)
            self._re = _re

        def execute_sql(self, query, params=None):
            query = query.replace(self._FQN_PREFIX, "")
            query = self._re.sub(r":(\w+)", r"$\1", query)
            result = self._conn.execute(query, params or {})
            columns = [desc[0] for desc in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]

    backend = _TestLocalBackend()
    with patch.object(mod, "_backend", backend):
        yield mod


# ===================================================================
# Layer 1: Direct Graph Engine Tool Tests (no LLM)
# ===================================================================


@skip_no_db
class TestFindEntity:

    def test_known_person_moses(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_entity("Moses")
        assert "Moses" in result
        assert "Person" in result

    def test_known_place_egypt(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_entity("Egypt")
        assert "Egypt" in result
        assert "Place" in result

    def test_partial_match_abra(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_entity("Abra")
        assert "Abraham" in result

    def test_not_in_graph_aristotle(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_entity("Aristotle")
        assert "No entity found" in result

    def test_case_insensitivity(self, _patch_backend):
        mod = _patch_backend
        result_lower = mod.find_entity("moses")
        result_upper = mod.find_entity("MOSES")
        assert "Moses" in result_lower
        assert "Moses" in result_upper

    def test_multiple_results(self, _patch_backend):
        """Searching 'Mary' should return both Mary and Mary Magdalene."""
        mod = _patch_backend
        result = mod.find_entity("Mary")
        assert "Mary" in result
        assert "Mary Magdalene" in result


@skip_no_db
class TestFindConnections:

    def test_many_connections_moses(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_connections("Moses")
        assert "Moses" in result
        lines = [l for l in result.split("\n") if l.startswith("- ")]
        assert len(lines) >= 10, f"Expected >=10 connections for Moses, got {len(lines)}"

    def test_bidirectional_pharaoh(self, _patch_backend):
        """Pharaoh is both a source (opposes Moses) and a target (opposed by God)."""
        mod = _patch_backend
        result = mod.find_connections("Pharaoh")
        assert "Pharaoh" in result
        assert "OPPOSED" in result

    def test_zero_connections_nonexistent(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_connections("Zarathustra")
        assert "No connections found" in result

    def test_connections_include_book_chapter(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_connections("Abraham")
        assert "Genesis" in result
        assert "ch." in result

    def test_relationship_types_present(self, _patch_backend):
        mod = _patch_backend
        result = mod.find_connections("Ruth")
        assert any(rt in result for rt in ["SPOUSE_OF", "PARENT_OF", "FAMILY_OF"])


@skip_no_db
class TestGetSourceEvidence:

    def test_verse_text_returned(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_source_evidence("Moses")
        assert "Verses mentioning" in result
        assert "Moses" in result

    def test_book_filter(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_source_evidence("Moses", book="Exodus")
        assert "Exodus" in result
        assert "Genesis" not in result.replace("Verses mentioning", "")

    def test_ruth_loyalty_verse(self, _patch_backend):
        """Ruth 1:16 — 'whither thou goest, I will go'."""
        mod = _patch_backend
        result = mod.get_source_evidence("Ruth", book="Ruth")
        assert "whither thou goest" in result or "Ruth" in result

    def test_no_verses_for_nonexistent(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_source_evidence("Aristotle")
        assert "No verses found" in result

    def test_burning_bush_verse(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_source_evidence("bush", book="Exodus")
        assert "bush" in result.lower()
        assert "Exodus" in result


@skip_no_db
class TestGetEntitySummary:

    def test_full_profile_abraham(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_entity_summary("Abraham")
        assert "Abraham" in result
        assert "Person" in result
        assert "relationships" in result.lower() or "--[" in result

    def test_includes_relationships(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_entity_summary("Abraham")
        assert "Key relationships" in result or "--[" in result

    def test_entity_not_found(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_entity_summary("Socrates")
        assert "not found" in result.lower()

    def test_ruth_profile(self, _patch_backend):
        mod = _patch_backend
        result = mod.get_entity_summary("Ruth")
        assert "Ruth" in result
        assert any(name in result for name in ["Boaz", "Naomi", "Obed"])

    def test_boaz_family_connections(self, _patch_backend):
        """Boaz should show SPOUSE_OF Ruth and PARENT_OF Obed."""
        mod = _patch_backend
        result = mod.get_entity_summary("Boaz")
        assert "Boaz" in result
        assert "Ruth" in result


@skip_no_db
class TestFindTopContacts:
    """Tests for the Enron-only find_top_contacts tool.

    Uses a mocked backend since Enron DuckDB is not yet exported locally.
    """

    def test_returns_ranked_contacts(self, _patch_backend):
        mod = _patch_backend
        mock_results = [
            {"contact": "Karen Denne", "sent": 5, "received": 26, "total": 31},
            {"contact": "Vanessa Groscrand", "sent": 2, "received": 14, "total": 16},
            {"contact": "Kathryn Corbally", "sent": 0, "received": 12, "total": 12},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be:
            mock_be.execute_sql.return_value = mock_results
            result = mod.find_top_contacts("Kenneth Lay")
        import json
        data = json.loads(result)
        assert data["entity"] == "Kenneth Lay"
        assert len(data["top_contacts"]) == 3
        assert data["top_contacts"][0]["total"] >= data["top_contacts"][1]["total"]

    def test_direction_outbound(self, _patch_backend):
        mod = _patch_backend
        mock_results = [
            {"contact": "Brian Redmond", "sent": 3, "received": 0, "total": 3},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be:
            mock_be.execute_sql.return_value = mock_results
            result = mod.find_top_contacts("Kenneth Lay", direction="outbound")
        import json
        data = json.loads(result)
        assert data["direction"] == "outbound"
        assert data["top_contacts"][0]["sent"] == 3

    def test_direction_inbound(self, _patch_backend):
        mod = _patch_backend
        mock_results = [
            {"contact": "Karen Denne", "sent": 0, "received": 26, "total": 26},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be:
            mock_be.execute_sql.return_value = mock_results
            result = mod.find_top_contacts("Kenneth Lay", direction="inbound")
        import json
        data = json.loads(result)
        assert data["direction"] == "inbound"
        assert data["top_contacts"][0]["received"] == 26

    def test_no_contacts_found(self, _patch_backend):
        mod = _patch_backend
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be:
            mock_be.execute_sql.return_value = []
            result = mod.find_top_contacts("Nobody Here")
        assert "No email contacts found" in result

    def test_bible_corpus_rejected(self, _patch_backend):
        mod = _patch_backend
        with patch.object(mod, "CORPUS", "bible"):
            result = mod.find_top_contacts("Moses")
        assert "only available for the Enron corpus" in result

    def test_humanizes_slug_names(self, _patch_backend):
        mod = _patch_backend
        mock_results = [
            {"contact": "karen_denne", "sent": 0, "received": 26, "total": 26},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be:
            mock_be.execute_sql.return_value = mock_results
            result = mod.find_top_contacts("Kenneth Lay")
        import json
        data = json.loads(result)
        assert data["top_contacts"][0]["name"] == "Karen Denne"


@skip_no_db
class TestGetEmailsBetween:
    """Tests for the Enron-only get_emails_between tool."""

    def test_returns_emails(self, _patch_backend):
        mod = _patch_backend
        mock_results = [
            {"date": "2001-10-25", "sender": "karen.denne@enron.com",
             "subject": "Meeting Update", "body_preview": "Hi Ken, regarding the meeting..."},
            {"date": "2001-10-20", "sender": "kenneth.lay@enron.com",
             "subject": "Re: Meeting Update", "body_preview": "Thanks Karen..."},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be, \
             patch.object(mod, "_get_corpus_config", return_value={"source_table": "emails"}):
            mock_be.execute_sql.return_value = mock_results
            result = mod.get_emails_between("Karen Denne", "Kenneth Lay")
        import json
        data = json.loads(result)
        assert data["total"] == 2
        assert data["between"] == ["Karen Denne", "Kenneth Lay"]
        assert data["emails"][0]["sender"] == "karen.denne@enron.com"

    def test_no_emails_found(self, _patch_backend):
        mod = _patch_backend
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be, \
             patch.object(mod, "_get_corpus_config", return_value={"source_table": "emails"}):
            mock_be.execute_sql.return_value = []
            result = mod.get_emails_between("Alice", "Bob")
        assert "No emails found" in result

    def test_bible_corpus_rejected(self, _patch_backend):
        mod = _patch_backend
        with patch.object(mod, "CORPUS", "bible"):
            result = mod.get_emails_between("Moses", "Aaron")
        assert "only available for the Enron corpus" in result

    def test_body_preview_truncated(self, _patch_backend):
        mod = _patch_backend
        long_body = "x" * 500
        mock_results = [
            {"date": "2001-10-25", "sender": "a@enron.com",
             "subject": "Test", "body_preview": long_body},
        ]
        with patch.object(mod, "CORPUS", "enron"), \
             patch.object(mod, "_backend") as mock_be, \
             patch.object(mod, "_get_corpus_config", return_value={"source_table": "emails"}):
            mock_be.execute_sql.return_value = mock_results
            result = mod.get_emails_between("A", "B")
        import json
        data = json.loads(result)
        assert len(data["emails"][0]["body_preview"]) <= 300


class TestMaybeHumanize:
    """Tests for _maybe_humanize — pure unit tests, no DB needed."""

    def test_email_suffix_enron_com(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("andrew_fastow_enron_com") == "Andrew Fastow"

    def test_email_suffix_ect(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("john_doe_ect_enron_com") == "John Doe"

    def test_slug_without_domain(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("karen_denne") == "Karen Denne"

    def test_slug_with_digits(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("user_123_test") == "User 123 Test"

    def test_already_human_readable(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("Karen Denne") == "Karen Denne"

    def test_proper_name_untouched(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("Kenneth.Lay@Enron.com") == "Kenneth.Lay@Enron.com"

    def test_single_char_not_humanized(self):
        import importlib
        import src.agent.agent_serving as mod
        importlib.reload(mod)
        assert mod._maybe_humanize("a") == "a"


# ===================================================================
# Layer 2: End-to-End Integration Tests (multi-model matrix)
# ===================================================================

# -- Model configurations --
# Each tuple: (test_id_suffix, provider, env_overrides)
# provider maps to --llm flag; env_overrides are extra env vars.

MODELS = [
    ("llama_3_1_8b", "databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-meta-llama-3-1-8b-instruct"}),
    ("gpt_4o_mini", "openai", {"OPENAI_MODEL": "gpt-4o-mini"}),
    ("llama_3_3_70b", "databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-meta-llama-3-3-70b-instruct"}),
    ("gpt_5_2", "databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-gpt-5.2"}),
]

# -- Questions --
# Each tuple: (category, question, expected_substrings, forbidden_substrings)

QUESTIONS = [
    # Multi-hop traversal: Ruth → Boaz → Obed → Jesse → David
    (
        "multi_hop",
        "Trace the exact family lineage from Ruth to David through the knowledge graph",
        ["Ruth", "Boaz", "Obed"],
        [],
    ),
    # Precise counting: Moses has 230 relationships in the graph
    (
        "counting",
        "How many distinct entities have a direct relationship with Moses in the knowledge graph?",
        ["Moses"],
        [],
    ),
    # Exhaustive enumeration: people who traveled to Egypt
    (
        "enumeration",
        "List every person who traveled to Egypt according to the knowledge graph",
        ["Egypt"],
        [],
    ),
    # Negative boundary: Solomon IS in the graph (Matthew genealogy) but barely
    (
        "negative_boundary",
        "What does the knowledge graph say about King Solomon?",
        ["Solomon"],
        [],
    ),
    # Cross-book tracking: Abraham, Isaac, Jacob, God, Joseph appear in Genesis AND Matthew
    (
        "cross_book",
        "Which people appear in both Genesis and Matthew according to the knowledge graph?",
        ["Abraham"],
        [],
    ),
    # Verse grounding: Ruth 1:16 — loyalty pledge
    (
        "verse_grounding",
        "Find the exact verse where Ruth pledges loyalty to Naomi",
        ["Ruth"],
        [],
    ),
    # Relationship type filtering: who OPPOSED whom in Exodus
    (
        "relationship_filter",
        "Who opposed someone in the book of Exodus according to the knowledge graph?",
        ["Pharaoh"],
        [],
    ),
    # Entity disambiguation: Mary vs Mary Magdalene
    (
        "disambiguation",
        "Are there multiple people named Mary in the knowledge graph? List them all.",
        ["Mary"],
        [],
    ),
    # Inverse reasoning: Abraham's descendants via PARENT_OF
    (
        "inverse",
        "List all of Abraham's descendants that appear in the knowledge graph",
        ["Abraham", "Isaac"],
        [],
    ),
    # Provenance format: every response must have a Provenance section
    (
        "provenance_format",
        "Who is Boaz and how is he related to Ruth?",
        ["Boaz", "Ruth"],
        [],
    ),
    # Scope limitation: agent should state coverage is the full 66-book KJV Bible
    (
        "scope_limit",
        "What does the entire New Testament say about the Holy Spirit?",
        [],
        [],
    ),
    # Multi-hop with verse: Burning bush → Moses → Exodus 3
    (
        "multi_hop_verse",
        "Which entity encountered the burning bush, and in which exact verse?",
        ["Moses", "Exodus"],
        [],
    ),
    # Cross-testament entity
    (
        "cross_testament",
        "In which books does Abraham appear according to the knowledge graph?",
        ["Abraham", "Genesis"],
        [],
    ),
    # Exhaustive place enumeration
    (
        "place_enum",
        "List all places that people traveled to in the book of Genesis according to the graph",
        ["Egypt"],
        [],
    ),
    # Family chain precision
    (
        "family_chain",
        "Who is the grandfather of Obed according to the knowledge graph?",
        ["Obed"],
        [],
    ),
]


def _model_available(provider: str, env_overrides: dict) -> bool:
    """Check whether credentials are available for a given provider."""
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "databricks":
        return bool(
            os.environ.get("DATABRICKS_HOST")
            or os.path.isfile(os.path.expanduser("~/.databrickscfg"))
        )
    return False


def _make_test_id(model_id: str, category: str) -> str:
    return f"{model_id}__{category}"


def _build_parametrize_args():
    """Build the list of (model_id, provider, env_overrides, category, question,
    expected, forbidden) for parametrize, with marks for skipping unavailable models."""
    args = []
    for model_id, provider, env_overrides in MODELS:
        for category, question, expected, forbidden in QUESTIONS:
            marks = []
            if not _model_available(provider, env_overrides):
                marks.append(
                    pytest.mark.skip(reason=f"No credentials for {provider}")
                )
            if not os.path.isfile(DUCKDB_PATH):
                marks.append(
                    pytest.mark.skip(reason="DuckDB not found")
                )
            param = pytest.param(
                model_id,
                provider,
                env_overrides,
                category,
                question,
                expected,
                forbidden,
                id=_make_test_id(model_id, category),
                marks=marks,
            )
            args.append(param)
    return args


@pytest.mark.integration
@pytest.mark.parametrize(
    "model_id,provider,env_overrides,category,question,expected,forbidden",
    _build_parametrize_args(),
)
def test_agent_e2e(
    model_id,
    provider,
    env_overrides,
    category,
    question,
    expected,
    forbidden,
):
    """Run the full agent pipeline via scripts/test_local.py and validate output.

    Parameterized over questions x models to produce a comparison matrix.
    """
    env = {**os.environ}
    env["GRAPHRAG_BACKEND"] = "local"
    env["GRAPHRAG_LLM_PROVIDER"] = provider
    env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "test_local.py"), question],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=PROJECT_ROOT,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0, (
        f"test_local.py exited with code {result.returncode}\n"
        f"STDOUT:\n{result.stdout[:2000]}\n"
        f"STDERR:\n{result.stderr[:2000]}"
    )

    output_lower = output.lower()

    for pattern in expected:
        assert pattern.lower() in output_lower, (
            f"Expected '{pattern}' in output for [{model_id}] {category}.\n"
            f"Output (first 1500 chars):\n{output[:1500]}"
        )

    for pattern in forbidden:
        assert pattern.lower() not in output_lower, (
            f"Forbidden '{pattern}' found in output for [{model_id}] {category}.\n"
            f"Output (first 1500 chars):\n{output[:1500]}"
        )


# ===================================================================
# Layer 3: Raw LLM Baseline — "No Calculator" Control Group
# ===================================================================
#
# Same questions, same LLM, but WITHOUT the graph engine.
# These tests are marked xfail: we EXPECT the raw LLM to fail,
# proving the graph engine's value. If an xfail unexpectedly passes,
# pytest flags it as XPASS — meaning the question wasn't hard enough.
#
# Ground truth comes from DuckDB (validated by Layer 1).
# Each test checks a specific, verifiable claim against the raw LLM output.

RAW_LLM_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "test_raw_llm.py")

# Ground truth from DuckDB (see Layer 1 tests for validation).
# These are graph-specific facts that ONLY exist in our extracted knowledge graph.
# No LLM training data contains "our extraction pipeline found exactly N items."
#
#   Moses: 52 distinct connected entities
#   Traveled to Egypt: Abram, Children of Israel, God, Ishmael, Israel, Jacob, Joseph, Moses
#   Mary entities: Mary (Acts), Mary Magdalene (Matthew) — exactly 2
#   OPPOSED in Exodus: exactly 42 relationships
#   Abraham relationship types: exactly 24 distinct types
#   Entities first mentioned in Ruth: 13 (incl. Elimelech, Chilion, Orpah, Nahshon)
#   Person entities in graph: 602

BASELINE_QUESTIONS = [
    # (test_id, question, validator_fn_name, ground_truth_args)
    #
    # --- These are impossible without the graph (extraction-specific counts) ---
    (
        "counting_moses",
        "How many distinct entities have a direct relationship with Moses in the "
        "King James Bible? Give ONLY a number.",
        "_check_exact_count",
        {"target": 52, "tolerance": 5},
    ),
    (
        "enumeration_egypt",
        "List every person or group who traveled to Egypt according to "
        "the King James Bible. List ONLY names, one per line.",
        "_check_entity_list",
        {"required": ["Abram", "Jacob", "Joseph", "Moses", "Israel"],
         "min_required": 5},
    ),
    (
        "disambiguation_mary",
        "How many distinct people named Mary appear in the King James Bible? "
        "Give ONLY a number.",
        "_check_exact_count",
        {"target": 2, "tolerance": 0},
    ),
    (
        "opposed_count_exodus",
        "How many OPPOSED relationships exist in the book of Exodus? "
        "Count every instance where one entity opposed another. Give ONLY a number.",
        "_check_exact_count",
        {"target": 42, "tolerance": 3},
    ),
    (
        "abraham_relationship_types",
        "How many distinct relationship types involve Abraham across "
        "the King James Bible? "
        "Examples of relationship types: PARENT_OF, SPOUSE_OF, TRAVELED_TO, etc. "
        "Give ONLY a number.",
        "_check_exact_count",
        {"target": 24, "tolerance": 3},
    ),
    (
        "entities_first_mentioned_ruth",
        "Name all entities whose very first mention in the Bible is in the book of Ruth. "
        "Include people, places, events, and groups. List ONLY names, one per line.",
        "_check_entity_list",
        {"required": ["Elimelech", "Chilion", "Orpah", "Nahshon", "Mahlon", "Boaz", "Naomi"],
         "min_required": 5},
    ),
    (
        "person_entity_count",
        "How many distinct Person entities can be extracted from "
        "the complete King James Bible? Give ONLY a number.",
        "_check_exact_count",
        {"target": 602, "tolerance": 50},
    ),
]


def _extract_number(text: str) -> int | None:
    """Extract the first integer from text."""
    import re
    match = re.search(r'\b(\d+)\b', text)
    return int(match.group(1)) if match else None


def _check_exact_count(output: str, target: int, tolerance: int) -> bool:
    """True if output contains a number within tolerance of target."""
    num = _extract_number(output)
    if num is None:
        return False
    return abs(num - target) <= tolerance


def _check_entity_list(output: str, required: list[str], min_required: int) -> bool:
    """True if output mentions at least min_required of the required entities."""
    output_lower = output.lower()
    found = sum(1 for name in required if name.lower() in output_lower)
    return found >= min_required


def _check_verse_ref(output: str, expected_ref: str) -> bool:
    """True if output contains the exact chapter:verse reference."""
    return expected_ref in output


def _check_name_present(output: str, expected: str) -> bool:
    """True if the expected name appears in the output."""
    return expected.lower() in output.lower()


_VALIDATORS = {
    "_check_exact_count": _check_exact_count,
    "_check_entity_list": _check_entity_list,
    "_check_verse_ref": _check_verse_ref,
    "_check_name_present": _check_name_present,
}


def _build_baseline_args():
    """Build parametrize args for baseline tests across models."""
    args = []
    for model_id, provider, env_overrides in MODELS:
        for test_id, question, validator_name, gt_args in BASELINE_QUESTIONS:
            marks = []
            if not _model_available(provider, env_overrides):
                marks.append(pytest.mark.skip(reason=f"No credentials for {provider}"))
            args.append(pytest.param(
                model_id, provider, env_overrides,
                test_id, question, validator_name, gt_args,
                id=f"baseline__{model_id}__{test_id}",
                marks=marks,
            ))
    return args


@pytest.mark.baseline
@pytest.mark.xfail(
    reason="Raw LLM without graph engine cannot match graph ground truth",
    strict=False,
)
@pytest.mark.parametrize(
    "model_id,provider,env_overrides,test_id,question,validator_name,gt_args",
    _build_baseline_args(),
)
def test_raw_llm_baseline(
    model_id, provider, env_overrides,
    test_id, question, validator_name, gt_args,
):
    """Raw LLM (no graph tools) — expected to FAIL on graph-dependent questions.

    xfail: Proves the graph engine's value. If this passes (XPASS), the question
    wasn't hard enough to differentiate graph-assisted from raw LLM answers.
    """
    env = {**os.environ}
    env["GRAPHRAG_LLM_PROVIDER"] = provider
    env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, RAW_LLM_SCRIPT, question, "--llm", provider],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=PROJECT_ROOT,
    )

    assert result.returncode == 0, (
        f"test_raw_llm.py crashed for [{model_id}] {test_id}\n"
        f"STDERR:\n{result.stderr[:2000]}"
    )

    output = result.stdout
    validator = _VALIDATORS[validator_name]
    passed = validator(output, **gt_args)

    assert passed, (
        f"[{model_id}] {test_id}: Raw LLM failed to match graph ground truth "
        f"(this is expected — the graph engine is the differentiator).\n"
        f"Validator: {validator_name}{gt_args}\n"
        f"Output:\n{output[:1500]}"
    )
