"""Lightweight curation helpers for the Enron evaluation bank.

The earlier implementation lived under ``src._internal`` and carried a much
larger review workflow. After the core-value refactor we only need a small
surface:

- no additional generated questions are shipped by default
- records flow through unchanged unless fields are missing
- reporting helpers can still materialize a compact review table when needed
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

ADDITIONAL_CURATED_QUESTIONS: list[dict[str, Any]] = []


def apply_curation_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of a question-bank record.

    The governed Enron bank already carries the review metadata it needs, so the
    only work left here is to ensure the common optional fields always exist.
    """

    normalized = deepcopy(record)
    normalized.setdefault("validation_status", "validated")
    normalized.setdefault("review_notes", "")
    normalized.setdefault("review_priority", None)
    normalized.setdefault("curation_origin", normalized.get("source_type", "canonical"))
    return normalized


def build_curation_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a compact tabular view for review/reporting workflows."""

    rows: list[dict[str, Any]] = []
    for record in records:
        normalized = apply_curation_metadata(record)
        rows.append(
            {
                "question_id": normalized.get("question_id", ""),
                "question_text": normalized.get("question_text", ""),
                "corpus": normalized.get("corpus", ""),
                "source_type": normalized.get("source_type", ""),
                "validation_status": normalized.get("validation_status", "validated"),
                "review_priority": normalized.get("review_priority"),
                "review_notes": normalized.get("review_notes", ""),
            }
        )
    return rows


__all__ = [
    "ADDITIONAL_CURATED_QUESTIONS",
    "apply_curation_metadata",
    "build_curation_rows",
]
