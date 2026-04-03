from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


_DEFAULT_ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _default_asset_path(corpus: str) -> Path:
    return _DEFAULT_ASSET_DIR / f"{corpus}_router_cases_train.json"


def _normalize_case(row: dict) -> dict:
    return {
        "question_id": row.get("question_id", ""),
        "question_text": row.get("question_text") or row.get("question", ""),
        "primitive": row.get("primitive", ""),
        "expected_entities": list(row.get("expected_entities", []) or []),
        "graph_ground_truth": row.get("graph_ground_truth", ""),
        "attorney_category": row.get("attorney_category", ""),
        "architecture_primary": row.get("architecture_primary", ""),
        "eval_split": row.get("eval_split", ""),
        "suite_tags": list(row.get("suite_tags", []) or []),
    }


def build_router_case_asset(
    *,
    output_path: str | Path | None = None,
    corpus: str = "enron",
    eval_splits: Iterable[str] = ("train",),
) -> dict:
    from src.evaluation.question_bank import export_governed_flat_questions

    rows: list[dict] = []
    normalized_splits = tuple(dict.fromkeys(split.strip() for split in eval_splits if split.strip()))
    for split in normalized_splits or ("train",):
        rows.extend(export_governed_flat_questions(corpus=corpus, eval_split=split))

    payload = {
        "corpus": corpus,
        "eval_splits": list(normalized_splits or ("train",)),
        "case_count": len(rows),
        "cases": [_normalize_case(row) for row in rows],
    }

    path = Path(output_path) if output_path else _default_asset_path(corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return payload


def load_router_case_asset(
    *,
    corpus: str = "enron",
    case_limit: int | None = None,
) -> list[dict]:
    env_path = os.environ.get("GRAPHRAG_ROUTER_CASE_ASSET", "").strip()
    path = Path(env_path) if env_path else _default_asset_path(corpus)

    payload: dict | None = None
    if path.exists():
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = None

    if payload is None:
        try:
            payload = build_router_case_asset(corpus=corpus)
        except OSError:
            from src.evaluation.question_bank import export_governed_flat_questions

            rows = export_governed_flat_questions(corpus=corpus, eval_split="train")
            payload = {
                "corpus": corpus,
                "eval_splits": ["train"],
                "case_count": len(rows),
                "cases": [_normalize_case(row) for row in rows],
            }

    cases = list(payload.get("cases", []) or [])
    if case_limit is not None:
        return cases[:case_limit]
    return cases
