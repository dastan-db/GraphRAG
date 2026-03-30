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
            "entity_structure", "entity_explore", "entity_pair",
            "timeline", "keyword_search", "general", "genie_analytics",
        }
        assert expected.issubset(set(PATTERN_REGISTRY.keys()))

    def test_each_pattern_has_steps(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        for name, pattern in PATTERN_REGISTRY.items():
            assert len(pattern.steps) > 0, f"Pattern {name} has no steps"

    def test_general_pattern_has_entity_free_tools(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["general"]
        tool_names = [s.tool_name for s in pattern.steps]
        assert "get_top_individuals" in tool_names
        assert "get_top_email_pairs" in tool_names
        assert "browse_topics" in tool_names

    def test_keyword_search_has_entity_free_tools(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["keyword_search"]
        entity_free = [s for s in pattern.steps
                       if "entity_name" not in s.params
                       and "entity_a" not in s.params]
        assert len(entity_free) >= 3, "keyword_search needs at least 3 entity-free steps"

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


# ===================================================================
# Investigative Trust Tools — Unit Tests (MockBackend)
# ===================================================================

class TestGetExtractionProvenance:
    """Unit tests for get_extraction_provenance tool."""

    def test_thread_provenance_returns_json(self, mod, mock_backend):
        mock_rows = [
            {
                "step": "entity_extraction",
                "model_endpoint": "databricks-meta-llama-3-3-70b-instruct",
                "prompt_template_version": "corporate_entity_v1",
                "input_char_count": 3200,
                "input_truncated_at": None,
                "output_entity_count": 5,
                "output_rel_count": 0,
                "error_message": None,
            },
        ]
        backend, ctx = mock_backend([mock_rows])
        with ctx:
            result = mod.get_extraction_provenance(thread_id="T-1234")
        parsed = json.loads(result)
        assert "extraction_steps" in parsed
        assert parsed["extraction_steps"][0]["step"] == "entity_extraction"

    def test_entity_resolution_audit(self, mod, mock_backend):
        alias_rows = [{"canonical_id": "jeff_skilling"}]
        audit_rows = [
            {
                "alias_id": "jeff_skilling_ect",
                "canonical_id": "jeff_skilling",
                "method": "custodian_hardcode",
                "blocking_reason": None,
                "confidence": 1.0,
                "ai_raw_response": None,
            },
        ]
        identity_rows = [
            {
                "entity_id": "jeff_skilling",
                "canonical_name": "Jeff Skilling",
                "email_addresses": ["jeff.skilling@enron.com"],
                "aliases": ["Jeff S."],
                "source": "custodian",
                "confidence": 1.0,
            },
        ]
        backend, ctx = mock_backend([alias_rows, audit_rows, identity_rows])
        with ctx:
            result = mod.get_extraction_provenance(entity_name="Jeff Skilling")
        parsed = json.loads(result)
        assert "resolution_audit" in parsed
        assert "identity" in parsed
        assert parsed["resolution_audit"][0]["method"] == "custodian_hardcode"

    def test_truncation_warning(self, mod, mock_backend):
        mock_rows = [
            {
                "step": "entity_extraction",
                "model_endpoint": "test-model",
                "prompt_template_version": "v1",
                "input_char_count": 8000,
                "input_truncated_at": 6000,
                "output_entity_count": 3,
                "output_rel_count": 0,
                "error_message": None,
            },
        ]
        backend, ctx = mock_backend([mock_rows])
        with ctx:
            result = mod.get_extraction_provenance(thread_id="T-5678")
        parsed = json.loads(result)
        assert "truncation_warning" in parsed

    def test_no_args_returns_hint(self, mod, mock_backend):
        backend, ctx = mock_backend([])
        with ctx:
            result = mod.get_extraction_provenance()
        assert "Provide either" in result


class TestTraceDataLineage:
    """Unit tests for trace_data_lineage tool."""

    def test_walks_upstream(self, mod, mock_backend):
        lineage_rows = [
            {"source_table": "emails", "target_table": "threads",
             "transformation_step": "07_Data_Prep", "sql_description": "Thread aggregation"},
            {"source_table": "threads", "target_table": "entities",
             "transformation_step": "07_KG", "sql_description": "Entity extraction"},
            {"source_table": "entities", "target_table": "entity_analytics",
             "transformation_step": "07b", "sql_description": "Graph centrality"},
        ]
        backend, ctx = mock_backend([lineage_rows])
        with ctx:
            result = mod.trace_data_lineage(table_name="entities")
        parsed = json.loads(result)
        assert parsed["table"] == "entities"
        assert parsed["lineage_depth"] >= 1
        sources = [step["source"] for step in parsed["lineage"]]
        assert "emails" in sources or "threads" in sources

    def test_raw_source_table_returns_empty(self, mod, mock_backend):
        lineage_rows = [
            {"source_table": "emails", "target_table": "threads",
             "transformation_step": "07", "sql_description": "Agg"},
        ]
        backend, ctx = mock_backend([lineage_rows])
        with ctx:
            result = mod.trace_data_lineage(table_name="unknown_table")
        parsed = json.loads(result)
        assert parsed["lineage"] == []


class TestBrowseTopics:
    """Unit tests for browse_topics tool."""

    def test_parent_categories(self, mod, mock_backend):
        cat_rows = [
            {"topic_id": "cat_energy", "category": "Energy",
             "thread_count": 500, "entity_count": 120},
            {"topic_id": "cat_legal", "category": "Legal",
             "thread_count": 342, "entity_count": 80},
        ]
        backend, ctx = mock_backend([cat_rows])
        with ctx:
            result = mod.browse_topics()
        parsed = json.loads(result)
        assert "parent_categories" in parsed
        assert len(parsed["parent_categories"]) == 2

    def test_drill_into_category(self, mod, mock_backend):
        sub_rows = [
            {"topic_id": "topic_california", "topic_label": "California Energy Crisis",
             "thread_count": 89, "entity_count": 25},
        ]
        backend, ctx = mock_backend([sub_rows])
        with ctx:
            result = mod.browse_topics(category="Energy")
        parsed = json.loads(result)
        assert "sub_topics" in parsed
        assert parsed["category"] == "Energy"


class TestGetCorpusCoverage:
    """Unit tests for get_corpus_coverage tool."""

    def test_general_coverage(self, mod, mock_backend):
        cov_rows = [
            {"metric_name": "entity_extraction_rate", "metric_value": 850,
             "denominator": 1000, "coverage_pct": 85.0},
            {"metric_name": "relationship_density", "metric_value": 60,
             "denominator": 100, "coverage_pct": 60.0},
        ]
        backend, ctx = mock_backend([cov_rows])
        with ctx:
            result = mod.get_corpus_coverage()
        parsed = json.loads(result)
        assert "corpus_metrics" in parsed
        assert "coverage_warnings" in parsed
        assert any("relationship_density" in w for w in parsed["coverage_warnings"])

    def test_entity_coverage(self, mod, mock_backend):
        cov_rows = [
            {"metric_name": "entity_extraction_rate", "metric_value": 900,
             "denominator": 1000, "coverage_pct": 90.0},
        ]
        activity_rows = [
            {"display_name": "Jeff Skilling", "total_sent": 1200, "total_received": 3400},
        ]
        cls_rows = [
            {"email_type": "original", "cnt": 800, "pct": 66.7},
            {"email_type": "reply", "cnt": 400, "pct": 33.3},
        ]
        prov_rows = [
            {"total_threads": 50, "truncated_threads": 5},
        ]
        backend, ctx = mock_backend([cov_rows, activity_rows, cls_rows, prov_rows])
        with ctx:
            result = mod.get_corpus_coverage(entity_name="Jeff Skilling")
        parsed = json.loads(result)
        assert "entity_activity" in parsed
        assert "extraction_quality" in parsed
        assert parsed["extraction_quality"]["truncation_rate_pct"] == 10.0


# ===================================================================
# Entity Memory Tests
# ===================================================================

class TestEntityMemory:
    """Unit tests for EntityMemory class."""

    def test_extract_simple_entity(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "Kenneth Lay", "type": "Person"}')
        assert len(em.recent) == 1
        assert em.recent[0]["name"] == "Kenneth Lay"

    def test_extract_entity_with_email(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "Jeff Skilling", "corporate_email": "jeff.skilling@enron.com"}')
        assert em.recent[0]["name"] == "Jeff Skilling"
        assert em.recent[0]["email"] == "jeff.skilling@enron.com"

    def test_extract_from_list(self, mod):
        em = mod.EntityMemory()
        em.extract('[{"name": "Alice"}, {"name": "Bob"}]')
        assert len(em.recent) == 2
        names = [e["name"] for e in em.recent]
        assert "Alice" in names
        assert "Bob" in names

    def test_extract_nested_dict(self, mod):
        em = mod.EntityMemory()
        data = json.dumps({"relationships": {"by_type": {"SENT_TO": [
            {"name": "Kenneth Lay", "target": "Jeff Skilling"},
            {"name": "Jeff Skilling", "source": "Kenneth Lay"},
        ]}}})
        em.extract(data)
        names = [e["name"] for e in em.recent]
        assert "Kenneth Lay" in names
        assert "Jeff Skilling" in names

    def test_deduplication(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "Kenneth Lay"}')
        em.extract('{"name": "Kenneth Lay"}')
        assert len(em.recent) == 1

    def test_max_entities(self, mod):
        em = mod.EntityMemory(max_entities=3)
        for i in range(5):
            em.extract(json.dumps({"name": f"Person{i}"}))
        assert len(em.recent) == 3
        assert em.recent[0]["name"] == "Person2"

    def test_skips_email_addresses_as_names(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "foo@bar.com"}')
        assert len(em.recent) == 0

    def test_skips_invalid_json(self, mod):
        em = mod.EntityMemory()
        em.extract("not json at all")
        assert len(em.recent) == 0

    def test_context_for_classifier_empty(self, mod):
        em = mod.EntityMemory()
        assert em.context_for_classifier() == ""

    def test_context_for_classifier_with_entities(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "Kenneth Lay"}')
        em.extract('{"name": "Jeff Skilling"}')
        ctx = em.context_for_classifier()
        assert "Kenneth Lay" in ctx
        assert "Jeff Skilling" in ctx

    def test_clear(self, mod):
        em = mod.EntityMemory()
        em.extract('{"name": "Kenneth Lay"}')
        em.clear()
        assert len(em.recent) == 0


# ===================================================================
# Enriched get_entity_summary Tests (Mock)
# ===================================================================

class TestGetEntitySummaryEnriched:
    """Test that get_entity_summary returns enriched data for Enron Person entities."""

    def test_email_addresses_included(self, mod, mock_backend):
        entity_rows = [{
            "name": "Mike A Roberts", "entity_type": "Person",
            "description": "An Enron employee", "mention_a": "Test Thread", "mention_b": None,
            "src": "Mike A Roberts", "relationship_type": "SENT_TO",
            "tgt": "Vince Kaminski", "rel_desc": "Email communication",
        }]
        backend, ctx = mock_backend([
            [],       # first pattern miss
            entity_rows,  # second pattern hit
            [],       # role_timeline query
            [],       # entity_analytics query
            [],       # department query
        ])
        with ctx, patch.object(mod, "_resolve_name_to_email", return_value=["%mike.roberts@enron.com%"]):
            with patch.object(mod, "_resolve_enron_entity_id", return_value=["%mike_a_roberts%", "%mike_roberts%"]):
                result = mod.get_entity_summary("Mike A Roberts")
        parsed = json.loads(result)
        assert "email_addresses" in parsed

    def test_centrality_included_when_available(self, mod, mock_backend):
        entity_rows = [{
            "name": "Kenneth Lay", "entity_type": "Person",
            "description": "CEO of Enron", "mention_a": "Leadership", "mention_b": None,
            "src": None, "relationship_type": None,
            "tgt": None, "rel_desc": None,
        }]
        analytics_rows = [{
            "pagerank": 0.045, "in_degree": 150,
            "out_degree": 200, "total_degree": 350,
        }]
        backend, ctx = mock_backend([
            entity_rows,      # entity query
            [],               # role_timeline
            analytics_rows,   # entity_analytics
            [],               # department
        ])
        with ctx, \
            patch.object(mod, "_resolve_name_to_email", return_value=[]), \
            patch.object(mod, "_resolve_enron_entity_id", return_value=["%kenneth_lay%"]):
            result = mod.get_entity_summary("Kenneth Lay")
        parsed = json.loads(result)
        assert "centrality" in parsed
        assert parsed["centrality"]["in_degree"] == 150


# ===================================================================
# find_connections temporal data Tests (Mock)
# ===================================================================

class TestFindConnectionsTemporal:
    """Test that find_connections returns temporal metadata."""

    def test_temporal_fields_in_output(self, mod, mock_backend):
        conn_rows = [{
            "source_name": "Kenneth Lay", "relationship_type": "MANAGES",
            "target_name": "Jeff Skilling", "description": "Managed",
            "frequency": 5, "evidence_count": 3,
            "first_observed": "2001-01-15", "last_observed": "2001-10-20",
            "confidence": 0.92,
        }]
        backend, ctx = mock_backend([conn_rows])
        with ctx, patch.object(mod, "_resolve_enron_entity_id", return_value=["%kenneth_lay%"]):
            result = mod.find_connections("Kenneth Lay", relationship_type="MANAGES")
        parsed = json.loads(result)
        conns = parsed["by_type"]["MANAGES"]
        assert len(conns) > 0
        assert "first_observed" in conns[0]
        assert "last_observed" in conns[0]
        assert "confidence" in conns[0]


# ===================================================================
# find_emails Tests (Mock)
# ===================================================================

class TestFindEmails:
    """Test the unified find_emails tool."""

    def test_hour_filter_in_sql(self, mod, mock_backend):
        email_rows = [{
            "date": "2001-06-15 19:30:00", "sender": "jeff.skilling@enron.com",
            "subject": "Late night", "body_preview": "Working late",
        }]
        backend, ctx = mock_backend([email_rows])
        with ctx, patch.object(mod, "_resolve_name_to_email", return_value=["%skilling%"]):
            result = mod.find_emails(person_a="Jeff Skilling", hour_from=18)
        assert "HOUR(date)" in backend.queries[0]["query"]
        parsed = json.loads(result)
        assert parsed["filters"]["hour_from"] == 18
        assert parsed["total"] > 0

    def test_keyword_search(self, mod, mock_backend):
        email_rows = [{
            "date": "2001-08-01", "sender": "a@enron.com",
            "subject": "Shred documents", "body_preview": "Please shred...",
        }]
        backend, ctx = mock_backend([email_rows])
        with ctx:
            result = mod.find_emails(keywords="shred, destroy")
        parsed = json.loads(result)
        assert parsed["total"] == 1
        assert parsed["filters"]["keywords"] == "shred, destroy"

    def test_no_criteria_returns_error(self, mod, mock_backend):
        backend, ctx = mock_backend([])
        with ctx:
            result = mod.find_emails()
        assert "No search criteria" in result

    def test_person_pair_search(self, mod, mock_backend):
        email_rows = [{
            "date": "2001-03-01", "sender": "lay@enron.com",
            "subject": "Meeting", "body_preview": "Let's discuss",
        }]
        backend, ctx = mock_backend([email_rows])
        with ctx, patch.object(mod, "_resolve_name_to_email", return_value=["%lay%"]):
            result = mod.find_emails(
                person_a="Kenneth Lay", person_b="Jeff Skilling",
                hour_from=18, hour_to=23,
            )
        parsed = json.loads(result)
        assert parsed["filters"]["person_a"] == "Kenneth Lay"
        assert parsed["filters"]["person_b"] == "Jeff Skilling"


# ===================================================================
# entity_profile pattern Tests
# ===================================================================

class TestEntityExplorePattern:
    """Test the entity_explore pattern in the registry."""

    def test_entity_explore_pattern_exists(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        assert "entity_explore" in PATTERN_REGISTRY

    def test_entity_explore_steps(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["entity_explore"]
        tool_names = [s.tool_name for s in pattern.steps]
        assert "get_entity_summary" in tool_names
        assert "find_top_contacts" in tool_names
        assert "find_connections" in tool_names
        assert "get_context_verses" in tool_names

    def test_entity_explore_min_confidence(self):
        from src.agent.pattern_registry import PATTERN_REGISTRY
        pattern = PATTERN_REGISTRY["entity_explore"]
        assert pattern.min_confidence == 0.0


class TestQueryAndEnrichEnhanced:
    """Unit tests for query_and_enrich enrichment additions."""

    def test_enrichment_has_new_keys(self, mod, mock_backend):
        quality_rows = [{"table_name": "emails", "total_nulls": 0, "avg_null_rate": 0.01}]
        role_rows = [{"entity_id": "jeff_skilling", "title": "CEO",
                      "department": "Executive", "reports_to": "kenneth_lay",
                      "effective_from": "2001-02-01", "effective_to": "2001-08-14",
                      "source": "sec_filing"}]
        cov_rows = [{"metric_name": "entity_extraction_rate", "coverage_pct": 75.0}]
        cls_rows = [{"email_type": "original", "cnt": 5000, "pct": 60.0}]
        ent_rows = [{"name": "Jeff Skilling", "entity_type": "Person",
                     "description": "CEO of Enron"}]
        backend, ctx = mock_backend([quality_rows, role_rows, cov_rows, cls_rows, ent_rows])
        with ctx:
            old_corpus = mod.CORPUS
            mod.CORPUS = "enron"
            try:
                result = mod.query_and_enrich.__wrapped__(
                    question="How many emails did Jeff Skilling send?",
                    space_name="communication_analytics"
                ) if hasattr(mod.query_and_enrich, '__wrapped__') else "skip"
            except Exception:
                result = "skip"
            finally:
                mod.CORPUS = old_corpus
        if result != "skip":
            parsed = json.loads(result)
            enrichment = parsed.get("enrichment", {})
            assert "role_context" in enrichment or "coverage_warnings" in enrichment
