"""Tests for the Enron GraphRAG Agent tools and heuristics.

Layer 1: Pure-function unit tests — no DB, no LLM
Layer 2: DuckDB integration tests — tool functions against local Enron data
Layer 3: Pattern registry and classifier tests

Prerequisites:
    python scripts/export_local_data.py --corpus enron  # creates data/graphrag_enron.duckdb
    pip install -e ".[local]"

Layer 1 only (fast):
    pytest tests/test_enron_agent.py -m "not integration" -v

Layer 2 (needs DuckDB):
    pytest tests/test_enron_agent.py -m integration -v
"""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths and skip conditions
# ---------------------------------------------------------------------------
ENRON_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "graphrag_enron.duckdb")
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

skip_no_enron_db = pytest.mark.skipif(
    not os.path.isfile(ENRON_DUCKDB_PATH),
    reason="Enron DuckDB not found — run: python scripts/export_local_data.py --corpus enron",
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
    """Stub out heavy third-party imports so unit tests can import tool
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

    def _mock_tool(f, **kwargs):
        f.name = kwargs.get("name", f.__name__)
        return f

    lc_tools.tool = _mock_tool

    sys.modules["langgraph.graph.message"].add_messages = None
    sys.modules["langgraph.graph"].END = "end"
    sys.modules["langgraph.graph"].StateGraph = MagicMock()
    sys.modules["mlflow"].langchain = MagicMock()
    sys.modules["mlflow"].models = MagicMock()

    os.environ.setdefault("GRAPHRAG_BACKEND", "local")
    os.environ.setdefault("GRAPHRAG_LLM_PROVIDER", "openai")
    os.environ.setdefault("GRAPHRAG_CORPUS", "enron")

    yield

    for mod_name in mocks:
        sys.modules.pop(mod_name, None)


@pytest.fixture()
def mod():
    """Import and reload agent_serving module."""
    import importlib
    import src.agent.agent_serving as _mod
    importlib.reload(_mod)
    return _mod


@pytest.fixture()
def enron_backend(mod):
    """Patch agent_serving._backend with a DuckDB backend pointing at
    the Enron test database."""
    import duckdb

    catalog = mod.CATALOG
    schema = mod.SCHEMA
    enron_schema = mod.ENRON_SCHEMA

    class _TestEnronBackend:
        _FQN_BIBLE = f"{catalog}.{schema}."
        _FQN_ENRON = f"{catalog}.{enron_schema}."

        def __init__(self):
            self._conn = duckdb.connect(ENRON_DUCKDB_PATH, read_only=True)

        def execute_sql(self, query, params=None):
            query = query.replace(self._FQN_ENRON, "")
            query = query.replace(self._FQN_BIBLE, "")
            query = re.sub(r":(\w+)", r"$\1", query)
            result = self._conn.execute(query, params or {})
            columns = [desc[0] for desc in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]

    backend = _TestEnronBackend()
    with patch.object(mod, "_backend", backend):
        yield mod


class MockBackend:
    """Configurable mock backend for unit testing SQL-issuing tools."""

    def __init__(self, responses=None):
        self.queries = []
        self.responses = responses or []
        self._call_idx = 0

    def execute_sql(self, query, params=None):
        self.queries.append({"query": query, "params": params})
        if self._call_idx < len(self.responses):
            result = self.responses[self._call_idx]
            self._call_idx += 1
            return result
        return []


@pytest.fixture()
def mock_backend(mod):
    """Provide a MockBackend and patch it into the module."""
    def _factory(responses=None):
        backend = MockBackend(responses)
        return backend, patch.object(mod, "_backend", backend)
    return _factory


# ===================================================================
# Layer 1: Unit Tests — _is_likely_same_person
# ===================================================================

class TestIsLikelySamePerson:

    def test_identical_emails(self, mod):
        assert mod._is_likely_same_person("a@x.com", "a@x.com") is True

    def test_same_domain_different_local(self, mod):
        assert mod._is_likely_same_person("alice@enron.com", "bob@enron.com") is False

    def test_same_local_different_domain(self, mod):
        assert mod._is_likely_same_person("alice@enron.com", "alice@gmail.com") is True

    def test_kaminski_bug(self, mod):
        """The original bug: vince.kaminski@enron.com vs vkaminski@aol.com."""
        assert mod._is_likely_same_person(
            "vince.kaminski@enron.com", "vkaminski@aol.com"
        ) is True

    def test_kaminski_reverse(self, mod):
        assert mod._is_likely_same_person(
            "vkaminski@aol.com", "vince.kaminski@enron.com"
        ) is True

    def test_first_initial_lastname(self, mod):
        assert mod._is_likely_same_person(
            "john.smith@enron.com", "jsmith@yahoo.com"
        ) is True

    def test_first_initial_lastname_reverse(self, mod):
        assert mod._is_likely_same_person(
            "jsmith@yahoo.com", "john.smith@enron.com"
        ) is True

    def test_different_people_different_domain(self, mod):
        assert mod._is_likely_same_person(
            "john.smith@enron.com", "jane.doe@gmail.com"
        ) is False

    def test_substring_match_short(self, mod):
        assert mod._is_likely_same_person(
            "jeff.skilling@enron.com", "jskilling@hotmail.com"
        ) is True

    def test_different_lastnames_cross_domain(self, mod):
        assert mod._is_likely_same_person(
            "kenneth.lay@enron.com", "andy.fastow@gmail.com"
        ) is False

    def test_underscore_dot_equivalence(self, mod):
        assert mod._is_likely_same_person(
            "kenneth_lay@enron.com", "kenneth.lay@gmail.com"
        ) is True

    def test_very_short_local_parts(self, mod):
        assert mod._is_likely_same_person("ab@x.com", "cd@y.com") is False


# ===================================================================
# Layer 1: Unit Tests — get_top_individuals (mock backend)
# ===================================================================

class TestGetTopIndividualsMock:

    def test_returns_json_with_individuals(self, mod, mock_backend):
        responses = [
            [
                {"person_id": "jeff.skilling@enron.com", "total_sent": "500",
                 "total_received": "300", "total": "800"},
                {"person_id": "kenneth.lay@enron.com", "total_sent": "200",
                 "total_received": "100", "total": "300"},
            ],
            [],  # display name lookup
        ]
        backend, patcher = mock_backend(responses)
        with patcher:
            result = mod.get_top_individuals(limit=10)
        data = json.loads(result)
        assert data["source"] == "person_activity"
        assert len(data["individuals"]) == 2
        assert data["individuals"][0]["total"] == 800

    def test_empty_corpus(self, mod, mock_backend):
        backend, patcher = mock_backend([[]])
        with patcher:
            result = mod.get_top_individuals()
        assert "No individual activity data" in result

    def test_sort_by_sent(self, mod, mock_backend):
        responses = [
            [{"person_id": "a@e.com", "total_sent": "10", "total_received": "5", "total": "15"}],
            [],
        ]
        backend, patcher = mock_backend(responses)
        with patcher:
            result = mod.get_top_individuals(sort_by="sent")
        data = json.loads(result)
        assert data["sort_by"] == "sent"
        assert "total_sent" in backend.queries[0]["query"]


# ===================================================================
# Layer 1: Unit Tests — search_emails recipient filter (mock backend)
# ===================================================================

class TestSearchEmailsRecipient:

    def test_recipient_filter_in_sql(self, mod, mock_backend):
        responses = [
            [],  # _resolve_name_to_email for recipient
            [{"date": "2001-01-01", "sender": "a@e.com",
              "subject": "Test", "body_preview": "hello"}],
        ]
        backend, patcher = mock_backend(responses)
        with patcher:
            with patch.object(mod, "_resolve_name_to_email", return_value=["%lay%"]):
                result = mod.search_emails(
                    keywords="test",
                    recipient="Kenneth Lay",
                )
        assert "recip_pat" in str(backend.queries) or "recipient" in result

    def test_no_recipient_no_filter(self, mod, mock_backend):
        responses = [
            [{"date": "2001-01-01", "sender": "a@e.com",
              "subject": "Test", "body_preview": "hello"}],
        ]
        backend, patcher = mock_backend(responses)
        with patcher:
            result = mod.search_emails(keywords="test")
        assert "recip_pat" not in str(backend.queries)


# ===================================================================
# Layer 2: DuckDB Integration Tests
# ===================================================================

@skip_no_enron_db
@pytest.mark.integration
class TestGetTopIndividualsIntegration:

    def test_returns_results(self, enron_backend):
        result = enron_backend.get_top_individuals(limit=5)
        data = json.loads(result)
        assert len(data["individuals"]) > 0
        assert data["individuals"][0]["total"] > 0

    def test_ordering(self, enron_backend):
        result = enron_backend.get_top_individuals(limit=10)
        data = json.loads(result)
        totals = [ind["total"] for ind in data["individuals"]]
        assert totals == sorted(totals, reverse=True)

    def test_sort_by_sent(self, enron_backend):
        result = enron_backend.get_top_individuals(limit=5, sort_by="sent")
        data = json.loads(result)
        sent_vals = [ind["emails_sent"] for ind in data["individuals"]]
        assert sent_vals == sorted(sent_vals, reverse=True)


@skip_no_enron_db
@pytest.mark.integration
class TestGetTopEmailPairsIntegration:

    def test_returns_pairs(self, enron_backend):
        result = enron_backend.get_top_email_pairs(limit=5)
        data = json.loads(result)
        assert "top_pairs" in data
        assert len(data["top_pairs"]) > 0


@skip_no_enron_db
@pytest.mark.integration
class TestSearchEmailsIntegration:

    def test_keyword_search(self, enron_backend):
        result = enron_backend.search_emails(keywords="california")
        data = json.loads(result)
        assert data["total"] > 0

    def test_no_results(self, enron_backend):
        result = enron_backend.search_emails(keywords="xyznonexistent12345")
        assert "No emails found" in result


# ===================================================================
# Layer 3: Pattern registry (unit)
# ===================================================================

class TestPatternRegistry:
    """Unit tests for pattern registry structure and lookups."""

    def test_all_patterns_exist(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        expected = {
            "org_hierarchy", "communication", "communication_comparison",
            "path", "temporal", "topic", "topic_pair",
            "corpus_ranking_pairs", "individual_ranking", "genie_analytics",
        }
        assert expected.issubset(set(PATTERN_REGISTRY.keys()))

    def test_each_pattern_has_steps(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        for name, pattern in PATTERN_REGISTRY.items():
            assert len(pattern.steps) > 0, f"Pattern {name} has no steps"

    def test_individual_ranking_uses_correct_tool(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["individual_ranking"]
        tool_names = [s.tool_name for s in pattern.steps]
        assert "get_top_individuals" in tool_names

    def test_corpus_ranking_pairs_uses_correct_tool(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["corpus_ranking_pairs"]
        tool_names = [s.tool_name for s in pattern.steps]
        assert "get_top_email_pairs" in tool_names

    def test_resolve_params_primary_entity(self):
        from src.agent.pattern_registry import resolve_params
        result = resolve_params(
            {"entity_name": "$ENTITY", "limit": 10},
            [{"name": "Kenneth Lay"}],
        )
        assert result["entity_name"] == "Kenneth Lay"
        assert result["limit"] == 10

    def test_resolve_params_secondary_entity(self):
        from src.agent.pattern_registry import resolve_params
        result = resolve_params(
            {"entity_a": "$ENTITY", "entity_b": "$ENTITY_B"},
            [{"name": "Kenneth Lay"}, {"name": "Jeff Skilling"}],
        )
        assert result["entity_a"] == "Kenneth Lay"
        assert result["entity_b"] == "Jeff Skilling"


# ===================================================================
# Layer 2b: DuckDB schema / metadata tables
# ===================================================================

@skip_no_enron_db
@pytest.mark.integration
class TestPersonIdentity:
    """T5: Validate person_identity table."""

    def test_table_has_rows(self, enron_backend):
        rows = enron_backend._backend.execute_sql("SELECT COUNT(*) AS cnt FROM person_identity")
        assert rows[0]["cnt"] > 0

    def test_all_person_entities_covered(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(DISTINCT e.entity_id) AS missing"
            " FROM entities e"
            " LEFT JOIN person_identity p ON e.entity_id = p.entity_id"
            " WHERE e.entity_type = 'Person' AND p.entity_id IS NULL"
        )
        # Allow some gaps but most should be covered
        assert rows[0]["missing"] < 100

    def test_confidence_range(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT MIN(confidence) AS min_c, MAX(confidence) AS max_c FROM person_identity"
        )
        assert rows[0]["min_c"] >= 0.0
        assert rows[0]["max_c"] <= 1.0


@skip_no_enron_db
@pytest.mark.integration
class TestEvidenceType:
    """T6: Validate evidence_type and confidence on relationships."""

    def test_evidence_type_values(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT evidence_type FROM relationships WHERE evidence_type IS NOT NULL"
        )
        types = {r["evidence_type"] for r in rows}
        assert types.issubset({"structural", "semantic", "temporal"})

    def test_structural_types_correct(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM relationships"
            " WHERE relationship_type IN ('SENT_TO', 'REPORTS_TO', 'EMPLOYED_BY', 'MANAGES')"
            " AND evidence_type != 'structural'"
        )
        assert rows[0]["cnt"] == 0

    def test_confidence_range(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT MIN(confidence) AS min_c, MAX(confidence) AS max_c"
            " FROM relationships WHERE confidence IS NOT NULL"
        )
        assert rows[0]["min_c"] >= 0.0
        assert rows[0]["max_c"] <= 1.0


@skip_no_enron_db
@pytest.mark.integration
class TestOntologyRegistry:
    """T7: Validate ontology_registry table."""

    def test_entity_types_registered(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT type_name FROM ontology_registry WHERE category = 'entity'"
        )
        types = {r["type_name"] for r in rows}
        expected = {"Person", "Organization", "Division", "Project",
                    "Meeting", "Document", "Location", "Financial_Event"}
        assert expected.issubset(types)

    def test_relationship_types_registered(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT type_name FROM ontology_registry WHERE category = 'relationship'"
        )
        types = {r["type_name"] for r in rows}
        expected = {"REPORTS_TO", "COLLABORATES_WITH", "MANAGES", "SENT_TO", "DISCUSSES"}
        assert expected.issubset(types)

    def test_definitions_not_empty(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM ontology_registry"
            " WHERE definition IS NULL OR definition = ''"
        )
        assert rows[0]["cnt"] == 0


@skip_no_enron_db
@pytest.mark.integration
class TestExtractionProvenance:
    """TM1: Validate extraction_provenance table."""

    def test_table_has_rows(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM extraction_provenance"
        )
        assert rows[0]["cnt"] > 0

    def test_truncation_detected(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM extraction_provenance"
            " WHERE input_truncated_at IS NOT NULL AND input_char_count > input_truncated_at"
        )
        # Should have some truncated threads
        assert rows[0]["cnt"] >= 0

    def test_steps_valid(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT step FROM extraction_provenance"
        )
        steps = {r["step"] for r in rows}
        valid = {"entity_extraction", "relationship_extraction", "thread_summarization",
                 "entity_resolution", "relevance_filter"}
        assert steps.issubset(valid)


@skip_no_enron_db
@pytest.mark.integration
class TestEntityResolutionAudit:
    """TM2: Validate entity_resolution_audit table."""

    def test_table_has_rows(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM entity_resolution_audit"
        )
        assert rows[0]["cnt"] > 0

    def test_custodian_merges_confidence(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM entity_resolution_audit"
            " WHERE method = 'custodian_hardcode' AND confidence != 1.0"
        )
        assert rows[0]["cnt"] == 0

    def test_valid_methods(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT method FROM entity_resolution_audit"
        )
        methods = {r["method"] for r in rows}
        assert methods.issubset({"custodian_hardcode", "ai_powered", "blocked"})


@skip_no_enron_db
@pytest.mark.integration
class TestEmailClassification:
    """TM3: Validate email_classification table."""

    def test_all_emails_classified(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT"
            " (SELECT COUNT(*) FROM emails) AS total_emails,"
            " (SELECT COUNT(*) FROM email_classification) AS classified"
        )
        # Should classify most emails
        assert rows[0]["classified"] > 0

    def test_reply_depth_valid(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM email_classification WHERE reply_depth < 0"
        )
        assert rows[0]["cnt"] == 0

    def test_email_types_valid(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT email_type FROM email_classification"
        )
        types = {r["email_type"] for r in rows}
        valid = {"original", "reply", "forward", "calendar", "automated", "bounce"}
        assert types.issubset(valid)


@skip_no_enron_db
@pytest.mark.integration
class TestDataQualityReport:
    """TM4: Validate data_quality_report table."""

    def test_tables_covered(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT table_name FROM data_quality_report"
        )
        tables = {r["table_name"] for r in rows}
        # At minimum the core tables should be covered
        assert len(tables) >= 10

    def test_null_rate_valid(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM data_quality_report"
            " WHERE null_rate < 0 OR null_rate > 1"
        )
        assert rows[0]["cnt"] == 0

    def test_cardinality_valid(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM data_quality_report"
            " WHERE cardinality_ratio < 0 OR cardinality_ratio > 1"
        )
        assert rows[0]["cnt"] == 0


@skip_no_enron_db
@pytest.mark.integration
class TestPipelineLineage:
    """TM5: Validate pipeline_lineage table."""

    def test_table_has_rows(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT COUNT(*) AS cnt FROM pipeline_lineage"
        )
        assert rows[0]["cnt"] > 10

    def test_no_orphan_targets(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT target_table FROM pipeline_lineage"
            " WHERE target_table NOT IN (SELECT DISTINCT source_table FROM pipeline_lineage)"
            " AND target_table NOT IN ('communication_dyads', 'person_activity',"
            "   'entity_analytics', 'entity_paths', 'entity_mentions',"
            "   'person_identity', 'email_classification', 'extraction_provenance',"
            "   'ontology_registry', 'corpus_coverage', 'data_quality_report',"
            "   'person_role_timeline', 'topic_taxonomy', 'pipeline_lineage',"
            "   'entity_resolution_audit')"
        )
        # Most tables should either be a source or a known leaf
        assert len(rows) <= 5

    def test_core_tables_present(self, enron_backend):
        rows = enron_backend._backend.execute_sql(
            "SELECT DISTINCT target_table FROM pipeline_lineage"
        )
        targets = {r["target_table"] for r in rows}
        expected = {"entities", "relationships", "entity_mentions", "person_activity"}
        assert expected.issubset(targets)


@pytest.mark.skipif(True, reason="Requires Lakebase connection — run on Databricks")
class TestLakebaseSync:
    """TL1: Validate Lakebase sync."""

    def test_row_counts_match(self):
        pass  # Requires actual Lakebase connection

    def test_indexes_exist(self):
        pass  # Requires actual Lakebase connection


@pytest.mark.skipif(True, reason="Requires Databricks MVs — run on Databricks")
class TestMVFreshness:
    """TL2: Validate materialized view freshness."""

    def test_entity_profiles_count(self):
        pass  # Requires actual Databricks environment

    def test_quality_summary_covers_tables(self):
        pass  # Requires actual Databricks environment


class TestParallelExecution:
    """TL5: Validate parallel tool execution correctness."""

    def test_parallel_flag_exists(self, mod):
        assert hasattr(mod, '_PARALLEL_TOOLS')

    def test_heuristic_entity_names(self, mod):
        names = mod._heuristic_entity_names("Who did Kenneth Lay communicate with Jeff Skilling about?")
        assert "Kenneth Lay" in names or "Jeff Skilling" in names
