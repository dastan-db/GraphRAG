from __future__ import annotations

import json
from unittest.mock import patch

from tests.test_enron_agent import mod


class TestTrustFixes:
    def test_provenance_guardrails_preserve_existing_provenance(self, mod):
        response = (
            "### Answer\n"
            "Grounded answer text.\n\n"
            "### Evidence\n"
            "- [2001-12-03, From: executive.office@enron.com, Subject: Enron Update]\n\n"
            "### Provenance\n"
            "- existing provenance block\n"
        )

        guarded = mod._apply_provenance_guardrails(
            response,
            [],
            "LIMITED",
            question="What happened?",
            contract={},
        )

        assert guarded == response.rstrip()

    def test_remove_unsupported_quote_lines_drops_orphaned_quotes(self, mod):
        response = (
            "### Answer\n"
            '- > "...What does this mean for us?..." —\n'
            '- > "...employees should preserve all records..." — [2001-12-03, From: executive.office@enron.com]\n'
        )
        supported = [
            {
                "date": "2001-12-03",
                "sender": "executive.office@enron.com",
                "subject": "Enron Update",
                "text": "Employees should preserve all records while Enron Corp. files voluntary petitions for relief under Chapter 11.",
            }
        ]

        cleaned = mod._remove_unsupported_quote_lines(response, supported)

        assert "What does this mean for us?" not in cleaned
        assert "employees should preserve all records" in cleaned

    def test_extract_answer_contract_marks_temporal_topic_questions(self, mod):
        contract = mod._extract_answer_contract(
            "What themes dominated the October 19, 2001 Enron Mentions digest?"
        )

        assert contract["answer_type"] == "topic"
        assert contract["force_pattern"] == "keyword_search"
        assert contract["topic_like"] is True

    def test_apply_factual_routing_overrides_keeps_topic_questions_off_timeline(self, mod):
        question = "What themes dominated the October 19, 2001 Enron Mentions digest?"
        contract = mod._extract_answer_contract(question)

        with patch.object(mod, "CORPUS", "enron"):
            routed = mod._apply_factual_routing_overrides(
                question,
                {
                    "pattern": "timeline",
                    "confidence": 0.61,
                    "entities": [],
                    "contract": contract,
                },
            )

        assert routed["pattern"] == "keyword_search"
        assert routed["routing_override"] == "heuristic:keyword_search"

    def test_invoke_llm_with_retry_retries_rate_limits(self, mod):
        attempts = {"count": 0}

        def _flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("REQUEST_LIMIT_EXCEEDED")
            return "ok"

        with patch.object(mod.time, "sleep") as sleep_mock:
            result = mod._invoke_llm_with_retry(_flaky, purpose="unit-test")

        assert result == "ok"
        assert attempts["count"] == 3
        assert sleep_mock.call_count == 2

    def test_assess_evidence_sufficiency_abstains_for_temporal_topic_without_in_window_email_hits(self, mod):
        question = "What themes dominated the October 19, 2001 Enron Mentions digest?"
        contract = mod._extract_answer_contract(question)
        tool_entries = [
            (
                "semantic_search_emails({\"query\": \"October 19, 2001 Enron Mentions digest\"})",
                json.dumps(
                    {
                        "emails": [
                            {
                                "date": "2002-06-18 17:59:41",
                                "sender": "phil.polsky@enron.com",
                                "subject": "centana",
                                "body_preview": "I'm not sure what the attached means, but it has the $416,000 amount in it.",
                            }
                        ]
                    }
                ),
            )
        ]

        assessment = mod._assess_evidence_sufficiency(
            tool_entries,
            "LIMITED",
            question=question,
            contract=contract,
            pattern_name="keyword_search",
        )

        assert assessment["features"]["date_window_email_hits"] == 0
        assert assessment["decision"] == "abstain"
        assert any(
            "requested date window" in reason
            for reason in assessment["reasons"]
        )
