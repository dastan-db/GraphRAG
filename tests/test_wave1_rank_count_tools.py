from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.test_enron_agent import (
    _mock_heavy_imports,
    enron_backend,
    mock_backend,
    mod,
    skip_no_enron_db,
)


class TestWave1RankCountUnit:
    def test_gate_enabled_adds_semantic_metadata(self, mod, mock_backend):
        responses = [
            [
                {
                    "person_id": "jeff.skilling@enron.com",
                    "total_sent": "500",
                    "total_received": "300",
                    "total": "800",
                }
            ],
            [
                {
                    "email_address": "jeff.skilling@enron.com",
                    "display": "Jeff Skilling",
                }
            ],
        ]
        backend, patcher = mock_backend(responses)
        with patcher, patch.dict(mod.os.environ, {"GRAPHRAG_WAVE1_GENIE_MODE": "gate"}, clear=False):
            result = mod.get_top_individuals(limit=1, sort_by="sent")

        data = json.loads(result)
        assert data["source"] == "person_activity"
        assert data["analytics_backend"] == "databricks_sql"
        assert data["wave1_gate_enabled"] is True
        assert data["wave1_gate_bundle_path"].endswith("genie_iteration0_baseline.json")
        assert data["individuals"][0]["name"] == "Jeff Skilling"
        assert "total_sent" in backend.queries[0]["query"]

    def test_gate_off_rolls_back_to_legacy_payload(self, mod, mock_backend):
        responses = [
            [
                {
                    "person_id": "a@e.com",
                    "total_sent": "10",
                    "total_received": "5",
                    "total": "15",
                }
            ],
            [],
        ]
        backend, patcher = mock_backend(responses)
        with patcher, patch.dict(mod.os.environ, {"GRAPHRAG_WAVE1_GENIE_MODE": "off"}, clear=False):
            result = mod.get_top_individuals(limit=1)

        data = json.loads(result)
        assert data["source"] == "person_activity"
        assert "analytics_backend" not in data
        assert "wave1_gate_enabled" not in data
        assert "GROUP BY person_id" in backend.queries[0]["query"]

    def test_detect_self_emails_uses_gate_and_filters_pairs(self, mod, mock_backend):
        responses = [
            [
                {
                    "person_a": "jeff.skilling@enron.com",
                    "person_b": "jskilling@hotmail.com",
                    "total": "4",
                    "peak_week": "2",
                    "first_seen": "2001-01-01",
                    "last_seen": "2001-01-08",
                    "active_weeks": "2",
                },
                {
                    "person_a": "kenneth.lay@enron.com",
                    "person_b": "andy.fastow@gmail.com",
                    "total": "9",
                    "peak_week": "5",
                    "first_seen": "2001-02-01",
                    "last_seen": "2001-02-15",
                    "active_weeks": "3",
                },
            ],
            [
                {
                    "email_address": "jeff.skilling@enron.com",
                    "display": "Jeff Skilling",
                }
            ],
        ]
        backend, patcher = mock_backend(responses)
        with patcher, patch.dict(mod.os.environ, {"GRAPHRAG_WAVE1_GENIE_MODE": "gate"}, clear=False):
            result = mod.detect_self_emails(limit=5)

        data = json.loads(result)
        assert data["analytics_backend"] == "databricks_sql"
        assert data["wave1_gate_enabled"] is True
        assert data["total_found"] == 1
        assert data["self_email_pairs"][0]["person"] == "Jeff Skilling"
        assert data["self_email_pairs"][0]["corporate_email"] == "jeff.skilling@enron.com"
        assert "HAVING SUM(d.total_count) >= 3" in backend.queries[0]["query"]


@skip_no_enron_db
@pytest.mark.integration
class TestWave1RankCountIntegration:
    def test_top_individuals_returns_gate_metadata(self, enron_backend):
        result = enron_backend.get_top_individuals(limit=3)
        data = json.loads(result)

        assert data["source"] == "person_activity"
        assert data["analytics_backend"] == "databricks_sql"
        assert data["wave1_gate_enabled"] is True
        assert data["wave1_gate_bundle_path"].endswith("genie_iteration0_baseline.json")
        assert len(data["individuals"]) == 3

    def test_top_email_pairs_returns_gate_metadata(self, enron_backend):
        result = enron_backend.get_top_email_pairs(limit=3)
        data = json.loads(result)

        assert data["source"] == "communication_dyads"
        assert data["analytics_backend"] == "databricks_sql"
        assert data["wave1_gate_enabled"] is True
        assert data["wave1_gate_bundle_path"].endswith("genie_iteration0_baseline.json")
        assert len(data["top_pairs"]) == 3
