from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.test_enron_agent import (
    _make_resolved,
    _mock_heavy_imports,
    enron_backend,
    mock_backend,
    mod,
    skip_no_enron_db,
)


class TestAnalyticsRouteGateUnit:
    def test_wave1_route_selects_top_individuals(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE1_GENIE_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route("Who sent the most emails at Enron?")

        assert routed is not None
        assert routed["tool_name"] == "get_top_individuals"
        assert routed["params"]["sort_by"] == "sent"

    def test_wave2_route_selects_find_top_contacts(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "Who communicated most with Kenneth Lay?",
                entities=[{"name": "Kenneth Lay"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "find_top_contacts"
        assert routed["params"]["entity_name"] == "Kenneth Lay"
        assert routed["migration_wave"] == "wave2"

    def test_wave2_route_selects_benchmark_top_contact_phrase(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "Who communicated most frequently with Kenneth Lay?",
                entities=[{"name": "Kenneth Lay"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "find_top_contacts"
        assert routed["params"]["entity_name"] == "Kenneth Lay"

    def test_wave2_route_selects_emails_between_for_pairwise_evidence(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "Show me all emails between Kenneth Lay and Leonardo Pacheco.",
                entities=[{"name": "Kenneth Lay"}, {"name": "Leonardo Pacheco"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "get_emails_between"
        assert routed["params"]["entity_a"] == "Kenneth Lay"
        assert routed["params"]["entity_b"] == "Leonardo Pacheco"
        assert routed["migration_wave"] == "wave2"

    def test_wave2_route_selects_external_contacts(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "Who did Kenneth Lay email outside the company?",
                entities=[{"name": "Kenneth Lay"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "get_external_contacts"
        assert routed["params"]["entity_name"] == "Kenneth Lay"

    def test_wave2_route_selects_communication_timeline(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "How did communication between Kenneth Lay and Jeff Skilling change over time?",
                entities=[{"name": "Kenneth Lay"}, {"name": "Jeff Skilling"}],
                metadata={"date_from": "2000-12-04", "date_to": "2000-12-11"},
            )

        assert routed is not None
        assert routed["tool_name"] == "get_communication_timeline"
        assert routed["params"]["entity_name"] == "Kenneth Lay"
        assert routed["params"]["entity_b"] == "Jeff Skilling"
        assert routed["params"]["date_from"] == "2000-12-04"
        assert routed["params"]["date_to"] == "2000-12-11"

    def test_wave2_route_selects_weekly_pair_dyad_timeline(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "How many direct Pacheco-to-Lay messages are recorded for the week beginning June 19, 2000 in the local communication dyad?",
                entities=[{"name": "Leonardo Pacheco"}, {"name": "Kenneth Lay"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "get_communication_timeline"
        assert routed["params"]["entity_name"] == "Leonardo Pacheco"
        assert routed["params"]["entity_b"] == "Kenneth Lay"
        assert routed["params"]["date_from"] == "2000-06-19"
        assert routed["params"]["date_to"] == "2000-06-19"

    def test_wave2_route_selects_pair_summary_count_timeline(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "How many emails were exchanged between Kenneth Lay and Leonardo Pacheco?",
                entities=[{"name": "Kenneth Lay"}, {"name": "Leonardo Pacheco"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "get_communication_timeline"
        assert routed["params"]["entity_name"] == "Kenneth Lay"
        assert routed["params"]["entity_b"] == "Leonardo Pacheco"
        assert routed["params"]["date_from"] == ""
        assert routed["params"]["date_to"] == ""

    def test_wave2_route_selects_dyad_topics_before_single_entity_topics(self, mod):
        with patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False):
            routed = mod._select_gated_analytics_route(
                "What were the top topics between Kenneth Lay and Jeff Skilling?",
                entities=[{"name": "Kenneth Lay"}, {"name": "Jeff Skilling"}],
            )

        assert routed is not None
        assert routed["tool_name"] == "get_dyad_topics"
        assert routed["params"]["entity_a"] == "Kenneth Lay"
        assert routed["params"]["entity_b"] == "Jeff Skilling"


class TestWave2HybridToolsUnit:
    def test_find_top_contacts_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [
                {
                    "contact_email": "karen.denne@enron.com",
                    "sent": "5",
                    "received": "26",
                    "total": "31",
                }
            ],
            [
                {
                    "email_address": "karen.denne@enron.com",
                    "display": "Karen Denne",
                }
            ],
        ]
        backend, patcher = mock_backend(responses)
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Kenneth Lay": [{"title": "Chairman"}]}},
            ),
        ):
            result = mod.find_top_contacts("Kenneth Lay", direction="both", limit=3)

        data = json.loads(result)
        assert data["source"] == "communication_dyads"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "ranking"
        assert data["fallback_used"] is False
        assert data["evidence_ready"] is False
        assert data["enrichment"]["role_context"]["Kenneth Lay"][0]["title"] == "Chairman"
        assert data["top_contacts"][0]["name"] == "Karen Denne"
        assert "GROUP BY contact_email" in backend.queries[0]["query"]

    def test_get_emails_between_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [],
            [
                {
                    "sender": "legal@enron.com",
                    "subject": "Board meeting",
                    "date": "2001-08-22",
                    "body_preview": "Kenneth Lay and Leonardo Pacheco were both referenced.",
                }
            ],
        ]
        backend, patcher = mock_backend(responses)
        resolved_a = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            entity_id_patterns=["%kenneth_lay%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        resolved_b = _make_resolved(
            mod,
            "Leonardo Pacheco",
            email_patterns=["%leonardo.pacheco@enron.com%"],
            entity_id_patterns=["%leonardo_pacheco%"],
            email_addresses=["leonardo.pacheco@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", side_effect=[resolved_a, resolved_b]),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={
                    "role_context": {
                        "Kenneth Lay": [{"title": "Chairman"}],
                        "Leonardo Pacheco": [{"title": "Executive"}],
                    }
                },
            ),
        ):
            result = mod.get_emails_between("Kenneth Lay", "Leonardo Pacheco", limit=5)

        data = json.loads(result)
        assert data["between"] == ["Kenneth Lay", "Leonardo Pacheco"]
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "listing"
        assert data["fallback_used"] is True
        assert data["fallback_reason"] == "body_mention_fallback"
        assert data["evidence_ready"] is True
        assert data["match_type"] == "body_mention"
        assert data["analytics_result"]["match_type"] == "body_mention"
        assert data["enrichment"]["role_context"]["Leonardo Pacheco"][0]["title"] == "Executive"
        assert data["emails"][0]["subject"] == "Board meeting"
        assert "INNER JOIN" in backend.queries[1]["query"]

    def test_find_emails_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [
                {
                    "date": "2001-08-15 21:30:00",
                    "sender": "kenneth.lay@enron.com",
                    "subject": "Shred docs",
                    "body_preview": "Please shred and destroy these drafts.",
                }
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Kenneth Lay": [{"title": "Chairman"}]}},
            ),
        ):
            result = mod.find_emails(
                person_a="Kenneth Lay",
                keywords="shred, destroy",
                hour_from=18,
                limit=5,
            )

        data = json.loads(result)
        assert data["filters"]["person_a"] == "Kenneth Lay"
        assert data["filters"]["keywords"] == "shred, destroy"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "listing"
        assert data["fallback_used"] is False
        assert data["evidence_ready"] is True
        assert data["analytics_result"]["filters"]["hour_from"] == 18
        assert data["emails"][0]["subject"] == "Shred docs"
        assert data["enrichment"]["role_context"]["Kenneth Lay"][0]["title"] == "Chairman"
        assert "LOWER(subject) LIKE" in backend.queries[0]["query"]

    def test_query_and_enrich_returns_hybrid_metadata(self, mod):
        sql_result = {
            "source": "databricks_sql_semantic_layer",
            "space": "communication_analytics",
            "query": "How many emails did Jeff Skilling send?",
            "sql_generated": "SELECT COUNT(*) AS total_sent FROM person_activity WHERE person_id LIKE '%jeff.skilling%'",
            "results": [{"total_sent": 42}],
            "row_count": 1,
            "description": "Email count for Jeff Skilling",
            "analytics_backend": "databricks_sql",
            "semantic_layer": mod.ENRON_COMMUNICATION_METRIC_VIEW,
        }
        resolved = _make_resolved(
            mod,
            "Jeff Skilling",
            email_patterns=["%jeff.skilling@enron.com%"],
            email_addresses=["jeff.skilling@enron.com"],
        )
        with (
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "_genie_sql_fallback", return_value=sql_result),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Jeff Skilling": [{"title": "CEO"}]}},
            ),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
        ):
            result = mod.query_and_enrich(
                question="How many emails did Jeff Skilling send?",
                space_name="communication_analytics",
            )

        data = json.loads(result)
        assert data["genie_result"]["source"] == "databricks_sql_semantic_layer"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "count"
        assert data["fallback_used"] is False
        assert data["resolved_entities"][0]["canonical_name"] == "Jeff Skilling"
        assert data["analytics_result"]["question"] == "How many emails did Jeff Skilling send?"
        assert data["enrichment"]["role_context"]["Jeff Skilling"][0]["title"] == "CEO"

    def test_query_and_enrich_records_genie_failure_fallback(self, mod):
        sql_result = {
            "source": "databricks_sql_semantic_layer",
            "space": "communication_analytics",
            "query": "How many emails did Jeff Skilling send?",
            "sql_generated": "SELECT COUNT(*) AS total_sent FROM person_activity WHERE person_id LIKE '%jeff.skilling%'",
            "results": [{"total_sent": 42}],
            "row_count": 1,
            "description": "Email count for Jeff Skilling",
            "analytics_backend": "databricks_sql",
            "semantic_layer": mod.ENRON_COMMUNICATION_METRIC_VIEW,
        }
        resolved = _make_resolved(
            mod,
            "Jeff Skilling",
            email_patterns=["%jeff.skilling@enron.com%"],
            email_addresses=["jeff.skilling@enron.com"],
        )
        with (
            patch.dict(
                mod.os.environ,
                {
                    "GRAPHRAG_WAVE2_HYBRID_MODE": "gate",
                    "GRAPHRAG_ANALYTICS_TRANSPORT": "local",
                    "GENIE_COMM_SPACE_ID": "space-123",
                },
                clear=False,
            ),
            patch.object(mod, "_genie_sql_fallback", return_value=sql_result),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Jeff Skilling": [{"title": "CEO"}]}},
            ),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch("databricks.sdk.WorkspaceClient", side_effect=RuntimeError("boom")),
        ):
            result = mod.query_and_enrich(
                question="How many emails did Jeff Skilling send?",
                space_name="communication_analytics",
            )

        data = json.loads(result)
        assert data["hybrid_contract_enabled"] is True
        assert data["fallback_used"] is True
        assert data["fallback_reason"] == "genie_error_databricks_sql_fallback"
        assert data["genie_result"]["source"] == "databricks_sql_semantic_layer"
        assert data["analytics_result"]["source"] == "databricks_sql_semantic_layer"

    def test_query_and_enrich_supports_weekly_pair_dyad_breakdown(self, mod, mock_backend):
        responses = [
            [
                {
                    "period": "2000-06-19",
                    "sent_a_to_b": "2",
                    "sent_b_to_a": "0",
                    "to_a_to_b": "2",
                    "to_b_to_a": "0",
                    "cc_a_to_b": "0",
                    "cc_b_to_a": "0",
                    "bcc_a_to_b": "0",
                    "bcc_b_to_a": "0",
                    "total": "2",
                }
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved_a = _make_resolved(
            mod,
            "Leonardo Pacheco",
            email_patterns=["%leonardo.pacheco@enron.com%"],
            email_addresses=["leonardo.pacheco@enron.com"],
        )
        resolved_b = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(
                mod,
                "resolve_entity_cached",
                side_effect=lambda name: resolved_a if "pacheco" in name.lower() else resolved_b,
            ),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={
                    "role_context": {
                        "Leonardo Pacheco": [{"title": "Executive"}],
                        "Kenneth Lay": [{"title": "Chairman"}],
                    }
                },
            ),
        ):
            result = mod.query_and_enrich(
                question="How many direct Pacheco-to-Lay messages are recorded for the week beginning June 19, 2000 in the local communication dyad?",
                space_name="communication_analytics",
            )

        data = json.loads(result)
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_intent"] == "count"
        assert data["genie_result"]["source"] == "databricks_sql_semantic_layer"
        assert data["genie_result"]["results"][0]["to_a_to_b"] == 2
        assert data["analytics_result"]["time_window"]["date_from"] == "2000-06-19"
        assert "COALESCE(d.to_count, 0)" in backend.queries[0]["query"]

    def test_contact_stats_propagates_hybrid_fields(self, mod):
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        hybrid_payload = json.dumps(
            {
                "entity": "Kenneth Lay",
                "source": "communication_dyads",
                "top_contacts": [{"name": "Karen Denne", "email": "karen.denne@enron.com", "total": 31}],
                "resolution": {"canonical_name": "Kenneth Lay"},
                "analytics_backend": "databricks_sql",
                "hybrid_contract_enabled": True,
                "analytics_intent": "ranking",
                "fallback_used": False,
                "evidence_ready": False,
            }
        )
        with (
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(mod.find_top_contacts, "invoke", return_value=hybrid_payload),
        ):
            result = mod.get_communication_stats(
                entity_name="Kenneth Lay",
                group_by="contact",
                limit=5,
            )

        data = json.loads(result)
        assert data["group_by"] == "contact"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["contacts"][0]["name"] == "Karen Denne"

    def test_get_external_contacts_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [
                {"external_email": "jskilling@hotmail.com", "total": "4"},
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Kenneth Lay": [{"title": "Chairman"}]}},
            ),
        ):
            result = mod.get_external_contacts("Kenneth Lay", direction="both", limit=3)

        data = json.loads(result)
        assert data["source"] == "communication_dyads"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "ranking"
        assert data["external_contacts"][0]["email"] == "jskilling@hotmail.com"
        assert data["enrichment"]["role_context"]["Kenneth Lay"][0]["title"] == "Chairman"
        assert "external_email" in backend.queries[0]["query"]

    def test_get_communication_timeline_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [
                {"period": "2000-12-04", "sent": "3", "received": "4", "total": "7"},
                {"period": "2000-12-11", "sent": "6", "received": "2", "total": "8"},
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={"role_context": {"Kenneth Lay": [{"title": "Chairman"}]}},
            ),
        ):
            result = mod.get_communication_timeline(
                entity_name="Kenneth Lay",
                date_from="2000-12-04",
                date_to="2000-12-11",
            )

        data = json.loads(result)
        assert data["source"] == "person_activity"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "timeline"
        assert data["time_series"][0]["sent"] == 3
        assert data["analytics_result"]["time_window"]["date_from"] == "2000-12-04"
        assert "FROM" in backend.queries[0]["query"]

    def test_get_pair_communication_timeline_returns_directional_breakdown(self, mod, mock_backend):
        responses = [
            [
                {
                    "period": "2000-06-19",
                    "sent_a_to_b": "2",
                    "sent_b_to_a": "0",
                    "to_a_to_b": "2",
                    "to_b_to_a": "0",
                    "cc_a_to_b": "0",
                    "cc_b_to_a": "0",
                    "bcc_a_to_b": "0",
                    "bcc_b_to_a": "0",
                    "total": "2",
                }
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved_a = _make_resolved(
            mod,
            "Leonardo Pacheco",
            email_patterns=["%leonardo.pacheco@enron.com%"],
            email_addresses=["leonardo.pacheco@enron.com"],
        )
        resolved_b = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", side_effect=[resolved_a, resolved_b]),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={
                    "role_context": {
                        "Leonardo Pacheco": [{"title": "Executive"}],
                        "Kenneth Lay": [{"title": "Chairman"}],
                    }
                },
            ),
        ):
            result = mod.get_communication_timeline(
                entity_name="Leonardo Pacheco",
                entity_b="Kenneth Lay",
                date_from="2000-06-19",
                date_to="2000-06-19",
            )

        data = json.loads(result)
        assert data["between"] == ["Leonardo Pacheco", "Kenneth Lay"]
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["time_series"][0]["total"] == 2
        assert data["time_series"][0]["sent_a_to_b"] == 2
        assert data["time_series"][0]["to_a_to_b"] == 2
        assert data["time_series"][0]["direct_total"] == 2
        assert data["analytics_result"]["time_window"]["date_from"] == "2000-06-19"
        assert "COALESCE(d.to_count, 0)" in backend.queries[0]["query"]

    def test_get_pair_communication_timeline_returns_summary_fields(self, mod, mock_backend):
        responses = [
            [
                {
                    "period": "2000-06-19",
                    "sent_a_to_b": "2",
                    "sent_b_to_a": "0",
                    "to_a_to_b": "2",
                    "to_b_to_a": "0",
                    "cc_a_to_b": "0",
                    "cc_b_to_a": "0",
                    "bcc_a_to_b": "0",
                    "bcc_b_to_a": "0",
                    "total": "2",
                },
                {
                    "period": "2000-06-26",
                    "sent_a_to_b": "3",
                    "sent_b_to_a": "0",
                    "to_a_to_b": "3",
                    "to_b_to_a": "0",
                    "cc_a_to_b": "0",
                    "cc_b_to_a": "0",
                    "bcc_a_to_b": "0",
                    "bcc_b_to_a": "0",
                    "total": "3",
                },
            ]
        ]
        backend, patcher = mock_backend(responses)
        resolved_a = _make_resolved(
            mod,
            "Leonardo Pacheco",
            email_patterns=["%leonardo.pacheco@enron.com%"],
            email_addresses=["leonardo.pacheco@enron.com"],
        )
        resolved_b = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", side_effect=[resolved_a, resolved_b]),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={
                    "role_context": {
                        "Leonardo Pacheco": [{"title": "Executive"}],
                        "Kenneth Lay": [{"title": "Chairman"}],
                    }
                },
            ),
        ):
            result = mod.get_communication_timeline(
                entity_name="Leonardo Pacheco",
                entity_b="Kenneth Lay",
            )

        data = json.loads(result)
        assert data["total_emails"] == 5
        assert data["sent_a_to_b"] == 5
        assert data["sent_b_to_a"] == 0
        assert data["direction_summary"] == "Leonardo Pacheco → Kenneth Lay"
        assert data["summary"]["weeks_with_traffic"] == 2
        assert data["analytics_result"]["summary"]["total_emails"] == 5
        assert "COALESCE(d.to_count, 0)" in backend.queries[0]["query"]

    def test_month_stats_propagates_timeline_hybrid_fields(self, mod):
        resolved = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        hybrid_payload = json.dumps(
            {
                "entity": "Kenneth Lay",
                "source": "person_activity",
                "time_series": [
                    {"period": "2000-12-04", "sent": 3, "received": 4, "total": 7},
                    {"period": "2000-12-11", "sent": 6, "received": 2, "total": 8},
                ],
                "analytics_backend": "databricks_sql",
                "hybrid_contract_enabled": True,
                "analytics_intent": "timeline",
                "fallback_used": False,
                "evidence_ready": False,
            }
        )
        with (
            patch.object(mod, "resolve_entity_cached", return_value=resolved),
            patch.object(mod.get_communication_timeline, "invoke", return_value=hybrid_payload),
        ):
            result = mod.get_communication_stats(
                entity_name="Kenneth Lay",
                group_by="month",
                limit=5,
            )

        data = json.loads(result)
        assert data["group_by"] == "month"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["monthly_trend"][0]["total_emails"] == 15

    def test_get_dyad_topics_returns_hybrid_metadata(self, mod, mock_backend):
        responses = [
            [
                {"thread_id": "thread-1"},
                {"thread_id": "thread-2"},
            ],
            [
                {
                    "thread_id": "thread-1",
                    "subject": "Board update",
                    "summary": "Discussion about board matters and compensation adjustments.",
                    "key_topics": ["Board Matters", "Compensation"],
                },
                {
                    "thread_id": "thread-2",
                    "subject": "Comp package",
                    "summary": "Follow-up on compensation and retention planning.",
                    "key_topics": '["Compensation"]',
                },
            ],
        ]
        backend, patcher = mock_backend(responses)
        resolved_a = _make_resolved(
            mod,
            "Kenneth Lay",
            email_patterns=["%kenneth.lay@enron.com%"],
            email_addresses=["kenneth.lay@enron.com"],
        )
        resolved_b = _make_resolved(
            mod,
            "Jeff Skilling",
            email_patterns=["%jeff.skilling@enron.com%"],
            email_addresses=["jeff.skilling@enron.com"],
        )
        with (
            patcher,
            patch.dict(mod.os.environ, {"GRAPHRAG_WAVE2_HYBRID_MODE": "gate"}, clear=False),
            patch.object(mod, "resolve_entity_cached", side_effect=[resolved_a, resolved_b]),
            patch.object(
                mod,
                "_collect_analytics_enrichment",
                return_value={
                    "role_context": {
                        "Kenneth Lay": [{"title": "Chairman"}],
                        "Jeff Skilling": [{"title": "CEO"}],
                    }
                },
            ),
        ):
            result = mod.get_dyad_topics("Kenneth Lay", "Jeff Skilling", limit=5)

        data = json.loads(result)
        assert data["between"] == ["Kenneth Lay", "Jeff Skilling"]
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "distribution"
        assert data["threads_scanned"] == 2
        assert data["top_topics"][0]["topic"] == "compensation"
        assert data["threads"][0]["thread_id"] == "thread-1"
        assert data["enrichment"]["role_context"]["Jeff Skilling"][0]["title"] == "CEO"
        assert "thread_discovery" in data["analytics_result"]["sql_generated"]
        assert len(backend.queries) == 2


@skip_no_enron_db
@pytest.mark.integration
class TestWave2HybridToolsIntegration:
    def test_find_top_contacts_returns_hybrid_metadata(self, enron_backend):
        result = enron_backend.find_top_contacts("Kenneth Lay", limit=3)
        data = json.loads(result)

        assert data["source"] == "communication_dyads"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert len(data["top_contacts"]) > 0

    def test_get_external_contacts_returns_hybrid_metadata(self, enron_backend):
        result = enron_backend.get_external_contacts("Kenneth Lay", limit=3)
        data = json.loads(result)

        assert data["source"] == "communication_dyads"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert len(data["external_contacts"]) > 0

    def test_get_communication_timeline_returns_hybrid_metadata(self, enron_backend):
        result = enron_backend.get_communication_timeline("Kenneth Lay")
        data = json.loads(result)

        assert data["source"] == "person_activity"
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert len(data["time_series"]) > 0

    def test_get_emails_between_returns_hybrid_metadata(self, enron_backend):
        result = enron_backend.get_emails_between("Kenneth Lay", "Leonardo Pacheco", limit=3)
        data = json.loads(result)

        assert data["between"] == ["Kenneth Lay", "Leonardo Pacheco"]
        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_backend"] == "databricks_sql"
        assert data["analytics_intent"] == "listing"
        assert len(data["emails"]) > 0

    def test_query_and_enrich_returns_hybrid_metadata(self, enron_backend):
        result = enron_backend.query_and_enrich(
            "How many emails were exchanged between Kenneth Lay and Leonardo Pacheco?",
            space_name="communication_analytics",
        )
        data = json.loads(result)

        assert data["hybrid_contract_enabled"] is True
        assert data["analytics_intent"] == "count"
        assert "genie_result" in data
        assert "analytics_result" in data
        assert data["analytics_backend"] in {"databricks_sql", "genie"}
