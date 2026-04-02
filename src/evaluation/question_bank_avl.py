from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

from src.evaluation.capability_scoring import build_gap_backlog, question_inventory_frame
from src.evaluation.question_bank import canonicalize_generated_question, load_question_bank
from src.evaluation.question_bank_curation import apply_curation_metadata, build_curation_rows

PROPOSER_ENDPOINT = os.environ.get(
    "GRAPHRAG_PROPOSER_ENDPOINT",
    "databricks-claude-sonnet-4-6",
)
REVIEW_ENDPOINT = os.environ.get(
    "GRAPHRAG_JUDGE_ENDPOINT",
    "databricks-claude-sonnet-4-6",
)

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
GAP_ORDER = {
    "no_questions": 0,
    "missing_holdout": 1,
    "missing_test": 2,
    "missing_train": 3,
    "low_quality": 4,
    "high_variance": 5,
}
VALID_SPLIT_HINTS = {"train", "test", "holdout"}
SINGLE_TURN_TYPES = {"single_turn"}


def _format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"
    headers = list(rows[0].keys())
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row.get(header, ""))))
    header_line = " | ".join(header.ljust(widths[header]) for header in headers)
    divider = "-+-".join("-" * widths[header] for header in headers)
    body = [
        " | ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers)
        for row in rows
    ]
    return "\n".join([header_line, divider, *body])


def _write_output(text: str, output_path: str | None) -> None:
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        print(text)


def _clean_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _call_llm(endpoint: str, prompt: str, *, max_tokens: int = 2048, temperature: float = 0.0) -> str:
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    response = client.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    return response["choices"][0]["message"]["content"].strip()


def _call_llm_json(endpoint: str, prompt: str, *, max_tokens: int = 2048, temperature: float = 0.0) -> Any:
    return json.loads(_clean_json_text(_call_llm(endpoint, prompt, max_tokens=max_tokens, temperature=temperature)))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return [value]


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
    return {}


def _normalize_sources(value: Any) -> list[dict[str, Any]]:
    sources = []
    for item in _coerce_list(value):
        if isinstance(item, dict):
            sources.append(dict(item))
        elif isinstance(item, str):
            sources.append({"type": "source_plan", "ref": item, "supports": ""})
    return sources


def _priority_rank(value: str) -> int:
    return PRIORITY_ORDER.get(value, 99)


def _gap_rank(value: str) -> int:
    return GAP_ORDER.get(value, 99)


def _slice_examples(
    *,
    corpus: str,
    domain_primary: str,
    attorney_category: str,
    architecture_primary: str,
    limit: int = 8,
) -> list[str]:
    examples: list[str] = []
    for record in load_question_bank(corpus=corpus, status=None):
        if record.get("interaction_type") not in SINGLE_TURN_TYPES:
            continue
        if record.get("domain_primary") != domain_primary:
            continue
        if record.get("attorney_category") != attorney_category:
            continue
        if record.get("architecture_primary") != architecture_primary:
            continue
        examples.append(record.get("question_text", ""))
    return examples[:limit]


def _review_prompt(record: dict[str, Any]) -> str:
    payload = {
        "question_id": record.get("question_id"),
        "question_text": record.get("question_text"),
        "corpus": record.get("corpus"),
        "attorney_category": record.get("attorney_category"),
        "architecture_primary": record.get("architecture_primary"),
        "domain_primary": record.get("domain_primary"),
        "status": record.get("status"),
        "eval_split": record.get("eval_split"),
        "validation_status": record.get("validation_status"),
        "reference_answer": record.get("reference_answer", ""),
        "ground_truth_sources": record.get("ground_truth_sources", []),
        "rubric_assessment": record.get("rubric_assessment", {}),
        "graph_ground_truth": record.get("graph_ground_truth", ""),
        "historical_ground_truth": record.get("historical_ground_truth", ""),
        "expected_entities": record.get("expected_entities", []),
        "expected_facts": record.get("expected_facts", []),
        "split_rationale": record.get("split_rationale", ""),
        "review_notes": record.get("review_notes", ""),
    }
    return f"""You are reviewing an EXISTING governed evaluation question for the GraphRAG canonical bank.

Existing question package:
{json.dumps(payload, indent=2)}

Your task:
1. Decide whether the question should remain active or return to candidate.
2. Decide the safest bucket recommendation: train, test, holdout, or candidate.
3. Repair or improve the reference answer and source plan if possible.
4. Score the question on this rubric from 1-5: answerability, evidence_sufficiency, specificity, stability, discriminative_power, leakage_risk.

Rules:
- If evidence is weak, ambiguous, or likely stale, prefer `recommended_status="candidate"` or `validation_status="needs_review"`.
- For Bible questions, cite exact verse references when you can.
- For Enron questions, do NOT fabricate message IDs, email counts, or thread IDs. If exact emails are not known from the prompt, provide a coarse source plan instead.
- Holdout requires low leakage risk and a strongly grounded answer package.

Return ONLY JSON with keys:
{{
  "reference_answer": "...",
  "ground_truth_sources": [{{"type": "...", "ref": "...", "supports": "..."}}],
  "rubric_assessment": {{
    "answerability": 1,
    "evidence_sufficiency": 1,
    "specificity": 1,
    "stability": 1,
    "discriminative_power": 1,
    "leakage_risk": 1
  }},
  "validation_status": "validated|provisional|needs_review",
  "recommended_status": "active|candidate",
  "recommended_eval_split": "train|test|holdout|candidate",
  "split_rationale": "...",
  "review_notes": "..."
}}"""


def _proposal_prompt(target: dict[str, Any], *, n_candidates: int) -> str:
    examples = "\n".join(f"- {question}" for question in target.get("existing_questions", [])[:8]) or "- none"
    return f"""You are the proposer in an AVL-style curation loop for the GraphRAG evaluation bank.

Target slice:
- Corpus: {target["corpus"]}
- Domain: {target["domain_primary"]}
- Attorney category: {target["attorney_category"]}
- Architecture capability: {target["architecture_primary"]}
- Gap type: {target["gap_type"]}
- Detail: {target["detail"]}
- Current counts: {json.dumps(target.get("current_counts", {}), sort_keys=True)}

Weak or existing questions in this slice:
{examples}

Generate exactly {n_candidates} NEW candidate questions that are harder, better grounded, and non-duplicative.

Rules:
- Keep `attorney_category="{target["attorney_category"]}"`.
- Keep `architecture_primary="{target["architecture_primary"]}"`.
- Keep `domain_primary="{target["domain_primary"]}"`.
- Questions must require grounded, evidence-backed answers.
- Do not paraphrase or lightly rewrite any existing question.
- Bible corpus: cite exact verse references when possible.
- Enron corpus: never invent exact message IDs, thread IDs, or counts. If exact emails are not known, use source plans like `org_hierarchy`, `investigation_timeline`, `communication_dyads`, or subject/date patterns instead of fabricated specifics.

Return ONLY JSON array of objects with keys:
[
  {{
    "question_text": "...",
    "difficulty": "medium|hard|adversarial",
    "expected_entities": ["..."],
    "expected_facts": ["..."],
    "graph_ground_truth": "...",
    "historical_ground_truth": "...",
    "reference_answer": "...",
    "ground_truth_sources": [{{"type": "...", "ref": "...", "supports": "..."}}],
    "evidence_required": true,
    "architecture_secondary": ["..."],
    "domain_secondary": ["..."],
    "split_hint": "train|test|holdout",
    "review_notes": "why this closes the gap"
  }}
]"""


def build_review_targets(
    *,
    corpus: str | None = None,
    max_targets: int = 12,
    priorities: Iterable[str] = ("high", "medium"),
) -> list[dict[str, Any]]:
    allowed = set(priorities)
    records = [
        record
        for record in load_question_bank(corpus=corpus, status=None)
        if record.get("interaction_type") in SINGLE_TURN_TYPES
    ]
    by_id = {record["question_id"]: record for record in records}
    targets: list[dict[str, Any]] = []
    for row in build_curation_rows(records):
        if row.get("review_priority") not in allowed and not row.get("bucket_mismatch"):
            continue
        record = by_id[row["question_id"]]
        targets.append(
            {
                "target_id": f"review::{record['question_id']}",
                "target_kind": "review",
                "corpus": record.get("corpus"),
                "question_id": record.get("question_id"),
                "question_text": record.get("question_text"),
                "attorney_category": record.get("attorney_category"),
                "architecture_primary": record.get("architecture_primary"),
                "domain_primary": record.get("domain_primary"),
                "status": record.get("status"),
                "eval_split": record.get("eval_split"),
                "validation_status": record.get("validation_status"),
                "review_priority": record.get("review_priority"),
                "bucket_mismatch": record.get("bucket_mismatch"),
                "quality_score": record.get("ground_truth_quality_score"),
                "latency_profile": record.get("validation_latency_profile"),
                "parallel_safe": record.get("parallel_validation_safe"),
                "recommended_workers": record.get("recommended_validation_workers"),
                "prompt": _review_prompt(record),
            }
        )
    targets.sort(
        key=lambda target: (
            _priority_rank(target["review_priority"]),
            not bool(target.get("bucket_mismatch")),
            target.get("corpus", ""),
            target.get("question_id", ""),
        )
    )
    return targets[:max_targets]


def build_gap_targets(
    *,
    corpus: str | None = None,
    max_targets: int = 12,
    severities: Iterable[str] = ("high", "medium"),
) -> list[dict[str, Any]]:
    inventory = question_inventory_frame(
        corpus=corpus,
        include_interaction_types=SINGLE_TURN_TYPES,
    )
    backlog = build_gap_backlog(inventory, corpus=corpus)
    allowed = set(severities)
    targets: list[dict[str, Any]] = []
    for gap in backlog:
        if gap.get("severity") not in allowed:
            continue
        profile_seed = apply_curation_metadata(
            {
                "question_id": f"gap-target::{gap['corpus']}::{gap['domain_primary']}::{gap['attorney_category']}::{gap['architecture_primary']}",
                "question_text": gap["detail"],
                "corpus": gap["corpus"],
                "interaction_type": "single_turn",
                "status": "candidate",
                "source_type": "avl_target",
                "attorney_category": gap["attorney_category"],
                "architecture_primary": gap["architecture_primary"],
                "architecture_secondary": [],
                "domain_primary": gap["domain_primary"],
                "domain_secondary": [],
                "suite_tags": ["avl_curation_target"],
                "expected_entities": [],
                "expected_facts": [],
                "evidence_required": True,
            }
        )
        targets.append(
            {
                "target_id": (
                    f"gap::{gap['corpus']}::{gap['domain_primary']}::{gap['attorney_category']}::"
                    f"{gap['architecture_primary']}::{gap['gap_type']}"
                ),
                "target_kind": "gap",
                "corpus": gap["corpus"],
                "domain_primary": gap["domain_primary"],
                "attorney_category": gap["attorney_category"],
                "architecture_primary": gap["architecture_primary"],
                "gap_type": gap["gap_type"],
                "severity": gap["severity"],
                "detail": gap["detail"],
                "current_counts": gap.get("current_counts", {}),
                "existing_questions": _slice_examples(
                    corpus=gap["corpus"],
                    domain_primary=gap["domain_primary"],
                    attorney_category=gap["attorney_category"],
                    architecture_primary=gap["architecture_primary"],
                ),
                "latency_profile": profile_seed.get("validation_latency_profile"),
                "parallel_safe": profile_seed.get("parallel_validation_safe"),
                "recommended_workers": profile_seed.get("recommended_validation_workers"),
                "prompt": gap["candidate_question_prompt"],
            }
        )
    targets.sort(
        key=lambda target: (
            _priority_rank(target["severity"]),
            _gap_rank(target["gap_type"]),
            target.get("corpus", ""),
            target.get("domain_primary", ""),
            target.get("attorney_category", ""),
            target.get("architecture_primary", ""),
        )
    )
    return targets[:max_targets]


def build_avl_curation_queue(
    *,
    corpus: str | None = None,
    max_review_targets: int = 12,
    max_gap_targets: int = 12,
) -> dict[str, Any]:
    review_targets = build_review_targets(corpus=corpus, max_targets=max_review_targets)
    gap_targets = build_gap_targets(corpus=corpus, max_targets=max_gap_targets)
    combined_queue = sorted(
        [*review_targets, *gap_targets],
        key=lambda target: (
            _priority_rank(target.get("review_priority", target.get("severity", "low"))),
            0 if target["target_kind"] == "gap" else 1,
            target.get("corpus", ""),
            target.get("target_id", ""),
        ),
    )

    summary_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "corpus": "",
            "review_targets": 0,
            "gap_targets": 0,
            "high_priority_reviews": 0,
            "high_severity_gaps": 0,
            "parallel_safe_targets": 0,
        }
    )
    for target in review_targets:
        row = summary_rows[target["corpus"]]
        row["corpus"] = target["corpus"]
        row["review_targets"] += 1
        row["parallel_safe_targets"] += int(bool(target.get("parallel_safe")))
        if target.get("review_priority") == "high":
            row["high_priority_reviews"] += 1
    for target in gap_targets:
        row = summary_rows[target["corpus"]]
        row["corpus"] = target["corpus"]
        row["gap_targets"] += 1
        row["parallel_safe_targets"] += int(bool(target.get("parallel_safe")))
        if target.get("severity") == "high":
            row["high_severity_gaps"] += 1

    return {
        "summary": [summary_rows[key] for key in sorted(summary_rows)],
        "review_targets": review_targets,
        "gap_targets": gap_targets,
        "combined_queue": combined_queue,
    }


def _fallback_gap_candidates(target: dict[str, Any], *, n_candidates: int) -> list[dict[str, Any]]:
    domain_label = target["domain_primary"].replace("_", " ")
    attorney = target["attorney_category"]
    templates = {
        "documentary_evidence": f"Which source evidence best supports {domain_label}?",
        "timeline_reconstruction": f"What timeline best reconstructs key events in {domain_label}?",
        "topic_investigation": f"What themes or topics define {domain_label}?",
        "corroboration_challenge": f"What evidence corroborates a disputed claim in {domain_label}?",
        "case_synthesis": f"What is the most evidence-backed synthesis of {domain_label}?",
        "person_profile": f"Which person best represents {domain_label}, and why?",
        "relationship_analysis": f"Which relationship path best explains {domain_label}?",
        "quantitative_analysis": f"What quantitative pattern is most revealing in {domain_label}?",
    }
    question_text = templates.get(attorney, f"What governed question would best test {domain_label}?")
    questions = []
    for idx in range(n_candidates):
        questions.append(
            {
                "question_text": question_text if idx == 0 else f"{question_text} (variant {idx + 1})",
                "difficulty": "hard",
                "expected_entities": [],
                "expected_facts": [target["detail"]],
                "graph_ground_truth": target["detail"],
                "historical_ground_truth": "",
                "reference_answer": target["detail"],
                "ground_truth_sources": [{"type": "source_plan", "ref": target["architecture_primary"], "supports": target["detail"]}],
                "evidence_required": True,
                "architecture_secondary": ["synthesis_provenance"],
                "domain_secondary": [],
                "split_hint": "train",
                "review_notes": "Fallback template proposal because LLM generation failed.",
            }
        )
    return questions


def _estimate_leakage_risk(candidate: dict[str, Any]) -> int:
    text = _normalize_text(candidate.get("question_text", ""))
    corpus = candidate.get("corpus")
    if corpus == "bible":
        if any(token in text for token in ["which passages", "cite at least", "trace", "step by step", "compare"]):
            return 2
        if any(token in text for token in ["who was", "what happened", "what covenant", "how did joseph", "road to damascus"]):
            return 5
        return 4
    if corpus == "enron":
        if any(token in text for token in ["how many", "percentage", "top ", "most ", "highest", "lowest"]):
            return 3
        if any(token in text for token in ["show me", "what evidence", "which emails", "source evidence", "reporting chain"]):
            return 2
        return 2
    return 3


def _clip_score(value: int) -> int:
    return max(1, min(5, int(value)))


def _provisional_rubric(candidate: dict[str, Any]) -> dict[str, int]:
    question_text = candidate.get("question_text", "")
    expected_entities = _coerce_list(candidate.get("expected_entities"))
    expected_facts = _coerce_list(candidate.get("expected_facts"))
    source_count = len(_normalize_sources(candidate.get("ground_truth_sources")))
    has_reference = bool(candidate.get("reference_answer"))

    answerability = 5 if has_reference and expected_facts else 4 if has_reference else 2
    evidence_sufficiency = 5 if not candidate.get("evidence_required", True) else _clip_score(2 + source_count)
    specificity = 2
    if len(question_text.split()) >= 10:
        specificity += 1
    if expected_entities:
        specificity += 1
    if len(expected_facts) >= 2:
        specificity += 1
    stability = 4
    if any(token in _normalize_text(question_text) for token in ["how many", "percentage", "top ", "most ", "highest", "lowest", "current"]):
        stability -= 1
    if candidate.get("corpus") == "enron" and any(token in _normalize_text(question_text) for token in ["how many", "percentage", "top ", "most "]):
        stability -= 1
    discriminative_power = 3
    if candidate.get("evidence_required", True):
        discriminative_power += 1
    if candidate.get("architecture_primary") in {"evidence_drilldown", "entity_resolution", "synthesis_provenance", "timeline_retrieval"}:
        discriminative_power += 1

    return {
        "answerability": _clip_score(answerability),
        "evidence_sufficiency": _clip_score(evidence_sufficiency),
        "specificity": _clip_score(specificity),
        "stability": _clip_score(stability),
        "discriminative_power": _clip_score(discriminative_power),
        "leakage_risk": _clip_score(_estimate_leakage_risk(candidate)),
    }


def _candidate_validation_status(candidate: dict[str, Any]) -> str:
    rubric = candidate.get("rubric_assessment", {})
    source_count = len(_normalize_sources(candidate.get("ground_truth_sources")))
    corpus = candidate.get("corpus")
    min_sources = 1 if corpus == "bible" else 2
    if (
        candidate.get("reference_answer")
        and source_count >= min_sources
        and rubric.get("answerability", 0) >= 4
        and rubric.get("evidence_sufficiency", 0) >= 4
        and rubric.get("specificity", 0) >= 4
    ):
        return "provisional"
    return "needs_review"


def _normalize_proposal(target: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    split_hint = str(proposal.get("split_hint", "")).strip().lower()
    base_question = {
        "question_text": proposal.get("question_text") or proposal.get("question") or target["detail"],
        "difficulty": proposal.get("difficulty", "hard"),
        "expected_entities": _coerce_list(proposal.get("expected_entities")),
        "expected_facts": _coerce_list(proposal.get("expected_facts")),
        "graph_ground_truth": proposal.get("graph_ground_truth", ""),
        "historical_ground_truth": proposal.get("historical_ground_truth", ""),
        "evidence_required": proposal.get("evidence_required", True),
        "attorney_category": target["attorney_category"],
        "architecture_primary": target["architecture_primary"],
        "architecture_secondary": _coerce_list(proposal.get("architecture_secondary")) or ["synthesis_provenance"],
        "domain_primary": target["domain_primary"],
        "domain_secondary": _coerce_list(proposal.get("domain_secondary")),
        "eval_split": split_hint if split_hint in VALID_SPLIT_HINTS else None,
        "status": "candidate",
        "suite_tags": ["avl_curation_candidate"],
    }
    candidate = canonicalize_generated_question(
        base_question,
        corpus=target["corpus"],
        source_type="avl_curation_generated",
        suite_tag="avl_curation_candidate",
    )
    candidate["status"] = "candidate"
    candidate["reference_answer"] = proposal.get("reference_answer", "")
    candidate["ground_truth_sources"] = _normalize_sources(proposal.get("ground_truth_sources"))
    candidate["review_notes"] = proposal.get("review_notes", "")
    candidate["recommended_status"] = "candidate"
    if split_hint in VALID_SPLIT_HINTS:
        candidate["recommended_eval_split"] = split_hint
    candidate["rubric_assessment"] = _provisional_rubric({**candidate, "ground_truth_sources": candidate["ground_truth_sources"]})
    candidate["validation_status"] = _candidate_validation_status({**candidate, "rubric_assessment": candidate["rubric_assessment"]})
    candidate["split_rationale"] = (
        f"AVL proposer suggested `{split_hint}` for this candidate."
        if split_hint in VALID_SPLIT_HINTS
        else "AVL candidate needs manual split confirmation after evidence validation."
    )
    candidate["review_priority"] = "high"
    candidate.setdefault("metadata", {})
    candidate["metadata"] = {
        **candidate.get("metadata", {}),
        "avl_target_id": target["target_id"],
        "gap_type": target["gap_type"],
        "proposal_mode": "gap_generation",
    }
    return apply_curation_metadata(candidate)


def _run_parallel(
    targets: list[dict[str, Any]],
    *,
    worker_fn: Callable[[dict[str, Any]], Any],
    max_workers: int,
) -> list[Any]:
    serial_targets = [target for target in targets if not target.get("parallel_safe", True)]
    parallel_targets = [target for target in targets if target.get("parallel_safe", True)]
    results: list[Any] = []
    for target in serial_targets:
        results.append(worker_fn(target))
    if parallel_targets:
        worker_count = max(1, min(max_workers, len(parallel_targets)))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(worker_fn, target): target for target in parallel_targets}
            for future in as_completed(futures):
                results.append(future.result())
    return results


def generate_gap_candidates(
    gap_targets: list[dict[str, Any]],
    *,
    candidates_per_gap: int = 2,
    max_workers: int = 4,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    existing_texts = {
        _normalize_text(record.get("question_text", ""))
        for record in load_question_bank(status=None)
        if record.get("interaction_type") in SINGLE_TURN_TYPES
    }

    def worker(target: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = _proposal_prompt(target, n_candidates=candidates_per_gap)
        if dry_run:
            raw_candidates = _fallback_gap_candidates(target, n_candidates=candidates_per_gap)
        else:
            try:
                raw_candidates = _call_llm_json(PROPOSER_ENDPOINT, prompt, max_tokens=3072)
                if not isinstance(raw_candidates, list):
                    raw_candidates = _fallback_gap_candidates(target, n_candidates=candidates_per_gap)
            except Exception:
                raw_candidates = _fallback_gap_candidates(target, n_candidates=candidates_per_gap)

        proposals: list[dict[str, Any]] = []
        seen_here: set[str] = set()
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            proposal = _normalize_proposal(target, raw)
            text_key = _normalize_text(proposal.get("question_text", ""))
            if not text_key or text_key in existing_texts or text_key in seen_here:
                continue
            seen_here.add(text_key)
            proposals.append(proposal)
        return proposals

    batches = _run_parallel(gap_targets, worker_fn=worker, max_workers=max_workers)
    return [proposal for batch in batches for proposal in batch]


def review_existing_questions(
    review_targets: list[dict[str, Any]],
    *,
    max_workers: int = 4,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    records_by_id = {
        record["question_id"]: record
        for record in load_question_bank(status=None)
        if record.get("interaction_type") in SINGLE_TURN_TYPES
    }

    def worker(target: dict[str, Any]) -> dict[str, Any]:
        record = records_by_id[target["question_id"]]
        if dry_run:
            payload = {
                "reference_answer": record.get("reference_answer", ""),
                "ground_truth_sources": record.get("ground_truth_sources", []),
                "rubric_assessment": record.get("rubric_assessment", {}),
                "validation_status": record.get("validation_status", "needs_review"),
                "recommended_status": record.get("recommended_status", "candidate"),
                "recommended_eval_split": record.get("recommended_eval_split", record.get("eval_split", "candidate")),
                "split_rationale": record.get("split_rationale", ""),
                "review_notes": f"Dry-run review packet for {record['question_id']}.",
            }
        else:
            try:
                payload = _call_llm_json(REVIEW_ENDPOINT, target["prompt"], max_tokens=2048)
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:
                payload = {"review_notes": f"Review generation failed: {exc}"}
        return {
            "question_id": record["question_id"],
            "corpus": record.get("corpus"),
            "question_text": record.get("question_text"),
            "target_kind": "review",
            "review_priority": target.get("review_priority"),
            "bucket_mismatch": target.get("bucket_mismatch"),
            "proposed_update": {
                "reference_answer": payload.get("reference_answer", record.get("reference_answer", "")),
                "ground_truth_sources": _normalize_sources(payload.get("ground_truth_sources", record.get("ground_truth_sources", []))),
                "rubric_assessment": _coerce_mapping(payload.get("rubric_assessment")) or record.get("rubric_assessment", {}),
                "validation_status": payload.get("validation_status", record.get("validation_status")),
                "recommended_status": payload.get("recommended_status", record.get("recommended_status")),
                "recommended_eval_split": payload.get("recommended_eval_split", record.get("recommended_eval_split", record.get("eval_split"))),
                "split_rationale": payload.get("split_rationale", record.get("split_rationale", "")),
                "review_notes": payload.get("review_notes", ""),
            },
        }

    return _run_parallel(review_targets, worker_fn=worker, max_workers=max_workers)


def build_candidate_summary(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "corpus": "",
            "candidate_proposals": 0,
            "provisional_candidates": 0,
            "needs_review_candidates": 0,
            "train_hints": 0,
            "test_hints": 0,
            "holdout_hints": 0,
        }
    )
    for candidate in candidates:
        corpus = candidate.get("corpus", "unknown")
        row = rows[corpus]
        row["corpus"] = corpus
        row["candidate_proposals"] += 1
        if candidate.get("validation_status") == "provisional":
            row["provisional_candidates"] += 1
        else:
            row["needs_review_candidates"] += 1
        split = candidate.get("recommended_eval_split")
        if split in {"train", "test", "holdout"}:
            row[f"{split}_hints"] += 1
    return [rows[key] for key in sorted(rows)]


def run_avl_curation(
    *,
    corpus: str | None = None,
    max_review_targets: int = 12,
    max_gap_targets: int = 12,
    candidates_per_gap: int = 2,
    max_workers: int = 4,
    dry_run: bool = False,
    run_reviews: bool = False,
) -> dict[str, Any]:
    queue = build_avl_curation_queue(
        corpus=corpus,
        max_review_targets=max_review_targets,
        max_gap_targets=max_gap_targets,
    )
    candidate_proposals = generate_gap_candidates(
        queue["gap_targets"],
        candidates_per_gap=candidates_per_gap,
        max_workers=max_workers,
        dry_run=dry_run,
    )
    review_packets = (
        review_existing_questions(queue["review_targets"], max_workers=max_workers, dry_run=dry_run)
        if run_reviews
        else []
    )
    return {
        **queue,
        "candidate_summary": build_candidate_summary(candidate_proposals),
        "candidate_proposals": candidate_proposals,
        "review_packets": review_packets,
    }


def _render_queue_report(payload: dict[str, Any]) -> str:
    parts = ["## AVL Queue Summary", _format_table(payload.get("summary", []))]
    parts.extend(
        [
            "\n## Gap Targets",
            _format_table(
                [
                    {
                        "target_id": row.get("target_id"),
                        "corpus": row.get("corpus"),
                        "domain_primary": row.get("domain_primary"),
                        "attorney_category": row.get("attorney_category"),
                        "architecture_primary": row.get("architecture_primary"),
                        "gap_type": row.get("gap_type"),
                        "severity": row.get("severity"),
                        "latency_profile": row.get("latency_profile"),
                        "detail": row.get("detail"),
                    }
                    for row in payload.get("gap_targets", [])
                ]
            ),
            "\n## Review Targets",
            _format_table(
                [
                    {
                        "target_id": row.get("target_id"),
                        "question_id": row.get("question_id"),
                        "corpus": row.get("corpus"),
                        "validation_status": row.get("validation_status"),
                        "review_priority": row.get("review_priority"),
                        "bucket_mismatch": row.get("bucket_mismatch"),
                        "latency_profile": row.get("latency_profile"),
                        "question_text": row.get("question_text"),
                    }
                    for row in payload.get("review_targets", [])
                ]
            ),
        ]
    )
    return "\n".join(parts)


def _render_proposal_report(payload: dict[str, Any]) -> str:
    parts = [_render_queue_report(payload)]
    parts.extend(
        [
            "\n## Candidate Summary",
            _format_table(payload.get("candidate_summary", [])),
            "\n## Candidate Proposals",
            _format_table(
                [
                    {
                        "question_id": row.get("question_id"),
                        "corpus": row.get("corpus"),
                        "validation_status": row.get("validation_status"),
                        "recommended_eval_split": row.get("recommended_eval_split"),
                        "domain_primary": row.get("domain_primary"),
                        "attorney_category": row.get("attorney_category"),
                        "architecture_primary": row.get("architecture_primary"),
                        "latency_profile": row.get("validation_latency_profile"),
                        "question_text": row.get("question_text"),
                    }
                    for row in payload.get("candidate_proposals", [])
                ]
            ),
        ]
    )
    if payload.get("review_packets"):
        parts.extend(
            [
                "\n## Review Packets",
                _format_table(
                    [
                        {
                            "question_id": row.get("question_id"),
                            "corpus": row.get("corpus"),
                            "review_priority": row.get("review_priority"),
                            "recommended_status": row.get("proposed_update", {}).get("recommended_status"),
                            "recommended_eval_split": row.get("proposed_update", {}).get("recommended_eval_split"),
                            "validation_status": row.get("proposed_update", {}).get("validation_status"),
                            "review_notes": row.get("proposed_update", {}).get("review_notes"),
                        }
                        for row in payload.get("review_packets", [])
                    ]
                ),
            ]
        )
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="AVL-style question-bank curation loop")
    parser.add_argument("--mode", choices=["queue", "propose"], default="queue")
    parser.add_argument("--corpus", choices=["enron", "bible"], default=None)
    parser.add_argument("--max-review-targets", type=int, default=12)
    parser.add_argument("--max-gap-targets", type=int, default=12)
    parser.add_argument("--candidates-per-gap", type=int, default=2)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Skip live LLM calls and use fallback generation")
    parser.add_argument("--run-reviews", action="store_true", help="Also generate review packets for existing questions")
    args = parser.parse_args()

    if args.mode == "queue":
        payload = build_avl_curation_queue(
            corpus=args.corpus,
            max_review_targets=args.max_review_targets,
            max_gap_targets=args.max_gap_targets,
        )
        if args.format == "json":
            _write_output(json.dumps(payload, indent=2), args.output)
            return
        _write_output(_render_queue_report(payload), args.output)
        return

    payload = run_avl_curation(
        corpus=args.corpus,
        max_review_targets=args.max_review_targets,
        max_gap_targets=args.max_gap_targets,
        candidates_per_gap=args.candidates_per_gap,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        run_reviews=args.run_reviews,
    )
    if args.format == "json":
        _write_output(json.dumps(payload, indent=2), args.output)
        return
    _write_output(_render_proposal_report(payload), args.output)


if __name__ == "__main__":
    main()
