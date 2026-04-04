"""Reference external implementer adapter for factual hardening loops.

This module is intentionally lightweight: it reads ``improvement_plan.json`` and
writes ``implementation_log.json`` so the orchestrator can hand off the
implementation stage to a separate process. The adapter can either emit a
structured skip/simulate artifact or apply a narrow set of documentary QA
hardening patches directly to ``src/agent/agent_serving.py``.

Usage:
    python -m src.agent.factual_external_implementer \
        --plan data/improvement_plan.json \
        --output data/implementation_log.json \
        --iteration 1 \
        --adapter-mode skip
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SERVING_PATH = Path("src/agent/agent_serving.py")
ORCHESTRATOR_PATH = Path("src/agent/factual_parallel_orchestrator.py")

LOCAL_BACKEND_DEFAULTS_OLD = """os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")
"""

LOCAL_BACKEND_DEFAULTS_NEW = """LOCAL_ENRON_EXPORT_PATH = Path("data/graphrag_enron.duckdb")
if "GRAPHRAG_BACKEND" not in os.environ:
    os.environ["GRAPHRAG_BACKEND"] = "local" if LOCAL_ENRON_EXPORT_PATH.exists() else "lakebase"
os.environ.setdefault("GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")
"""

SYNTHESIS_GUARDRAIL_OLD = """def _soften_overclaiming_language(text: str, assessment: dict) -> str:
    features = assessment.get("features", {})
    if (
        assessment.get("decision") == "answer"
        and features.get("email_level_hits", 0) > 0
        and features.get("meaningful_count", 0) >= 4
    ):
        return text
    softened = text
    replacements = {
        r"\\bproves\\b": "supports",
        r"\\bproved\\b": "supported",
        r"\\bclearly shows\\b": "is consistent with",
        r"\\bconfirms\\b": "supports",
        r"\\bdemonstrates\\b": "suggests",
    }
    for pattern, replacement in replacements.items():
        softened = re.sub(pattern, replacement, softened, flags=re.IGNORECASE)
    return softened
"""

SYNTHESIS_GUARDRAIL_NEW = """def _soften_overclaiming_language(text: str, assessment: dict) -> str:
    features = assessment.get("features", {})
    if (
        assessment.get("decision") == "answer"
        and features.get("email_level_hits", 0) > 0
        and features.get("meaningful_count", 0) >= 4
    ):
        return text
    softened = text
    replacements = {
        r"\\bproves\\b": "supports",
        r"\\bproved\\b": "supported",
        r"\\bclearly shows\\b": "is consistent with",
        r"\\bconfirms\\b": "supports",
        r"\\bdemonstrates\\b": "suggests",
        r"\\bthe documentary evidence(?: in the local Enron (?:email )?corpus)? shows\\b": "the available documentary evidence suggests",
        r"\\bthe evidence shows\\b": "the available evidence suggests",
        r"\\bthe emails show\\b": "the available emails suggest",
    }
    for pattern, replacement in replacements.items():
        softened = re.sub(pattern, replacement, softened, flags=re.IGNORECASE)
    if assessment.get("decision") == "hedge":
        caution = "The available data provides partial support rather than definitive proof."
        if caution.lower() not in softened.lower():
            softened = re.sub(
                r"(?is)\\A(###\\s+Answer\\s*\\n+)",
                rf"\\1{caution}\\n\\n",
                softened,
                count=1,
            )
            if caution.lower() not in softened.lower():
                softened = f"{caution}\\n\\n{softened.lstrip()}"
    return softened
"""

SUPPORTED_TRIM_HELPER_OLD = """def _normalize_citation_field(value: str) -> str:
    return re.sub(r"\\s+", " ", re.sub(r"[^\\w\\s@.-]", " ", value.lower())).strip()


def _citation_is_supported(date: str, sender: str, subject: str, supported: list[dict]) -> bool:
"""

SUPPORTED_TRIM_HELPER_NEW = """def _normalize_citation_field(value: str) -> str:
    return re.sub(r"\\s+", " ", re.sub(r"[^\\w\\s@.-]", " ", value.lower())).strip()


def _trim_supported_email_records(
    supported: list[dict],
    *,
    question: str = "",
    limit: int = 3,
) -> list[dict]:
    if not supported or limit <= 0:
        return []
    normalized_question = _normalize_citation_field(question)
    question_tokens = {
        token
        for token in normalized_question.split()
        if len(token) >= 4 and token not in {"from", "with", "that", "this", "their", "about"}
    }
    if not question_tokens:
        return supported[:limit]

    def _score(record: dict) -> tuple[int, int, str, str]:
        combined = _normalize_citation_field(
            " ".join(
                [
                    str(record.get("subject", "") or ""),
                    str(record.get("text", "") or ""),
                ]
            )
        )
        overlap = sum(1 for token in question_tokens if token in combined)
        full_body_bonus = 1 if len(str(record.get("text", "") or "")) >= 120 else 0
        return (
            overlap,
            full_body_bonus,
            str(record.get("date", "") or ""),
            str(record.get("message_id", "") or ""),
        )

    ranked = sorted(supported, key=_score, reverse=True)
    return ranked[:limit]


def _citation_is_supported(date: str, sender: str, subject: str, supported: list[dict]) -> bool:
"""

CLAIM_VERIFICATION_TRIM_OLD = """    supported = _extract_supported_email_records(tool_entries, question=question, contract=contract)
    if contract and contract.get("requires_evidence") and not supported and assessment.get("decision") != "answer":
"""

CLAIM_VERIFICATION_TRIM_NEW = """    supported = _extract_supported_email_records(tool_entries, question=question, contract=contract)
    if contract and contract.get("requires_evidence"):
        supported = _trim_supported_email_records(
            supported,
            question=question,
            limit=3,
        )
    if contract and contract.get("requires_evidence") and not supported and assessment.get("decision") != "answer":
"""

ABSTENTION_SOFTEN_OLD = """        elif (
            features["high_signal_query_hits"] == 0
            and features["full_body_hits"] == 0
            and not timeline_backed_documentary_packet
        ):
            reasons.append(
                "The retrieved emails provide only weak topical support and do not directly verify the requested claim."
            )
            decision = _escalate_sufficiency_decision(decision, "hedge")

    if answer_type == "count" and features["meaningful_count"] < EVIDENCE_CONFIG["evidence_sufficiency_threshold"]:
"""

ABSTENTION_SOFTEN_NEW = """        elif (
            features["high_signal_query_hits"] == 0
            and features["full_body_hits"] == 0
            and not timeline_backed_documentary_packet
        ):
            reasons.append(
                "The retrieved emails provide only weak topical support and do not directly verify the requested claim."
            )
            decision = _escalate_sufficiency_decision(decision, "hedge")
        elif (
            contract.get("documentary_evidence_like")
            and answer_type != "proof_email"
            and decision == "abstain"
            and features["query_relevant_email_hits"] > 0
            and (
                features["high_signal_query_hits"] > 0
                or features["full_body_hits"] > 0
            )
        ):
            reasons.append(
                "At least one claim-relevant email was retrieved, so answer narrowly with explicit caveats instead of a full refusal."
            )
            decision = "hedge"

    if answer_type == "count" and features["meaningful_count"] < EVIDENCE_CONFIG["evidence_sufficiency_threshold"]:
"""

FAST_PATH_DRILLDOWN_OLD = """        if (
            not shortcut_documentary
            and CORPUS == "enron"
            and pattern.name in (
            "entity_structure", "entity_pair", "entity_explore",
            "keyword_search", "timeline",
            )
        ):
            drill_limit = 4 if contract.get("requires_evidence") else 2
            drill_ids = _extract_evidence_ids_for_drilldown(tool_results, limit=drill_limit)
            for mid, tid in drill_ids:
                drill_params = {}
                if mid:
                    drill_params["message_id"] = mid
                elif tid:
                    drill_params["thread_id"] = tid
                drill_params["limit"] = 2 if contract.get("requires_evidence") else 1
                followup_steps.append(ExecutionStep("get_email_full_body", drill_params))
"""

FAST_PATH_DRILLDOWN_NEW = """        if (
            not shortcut_documentary
            and CORPUS == "enron"
            and pattern.name in (
            "entity_structure", "entity_pair", "entity_explore",
            "keyword_search", "timeline",
            )
        ):
            drill_limit = 2 if contract.get("requires_evidence") else 1
            if sufficiency["decision"] == "hedge" and contract.get("requires_evidence"):
                drill_limit = 1
            drill_ids = _extract_evidence_ids_for_drilldown(tool_results, limit=drill_limit)
            for mid, tid in drill_ids:
                drill_params = {}
                if mid:
                    drill_params["message_id"] = mid
                elif tid:
                    drill_params["thread_id"] = tid
                drill_params["limit"] = 1
                followup_steps.append(ExecutionStep("get_email_full_body", drill_params))
"""


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _replace_once(
    text: str,
    old: str,
    new: str,
    *,
    patch_name: str,
    summary: str,
) -> tuple[str, dict[str, Any]]:
    if new in text:
        return text, {
            "patch_name": patch_name,
            "summary": summary,
            "applied": False,
            "status": "already_present",
            "lines_changed": None,
            "before_excerpt": None,
            "after_excerpt": None,
        }
    idx = text.find(old)
    if idx == -1:
        raise ValueError(f"Anchor for patch '{patch_name}' not found.")
    start_line = _line_number(text, idx)
    end_line = start_line + old.count("\n")
    updated = text.replace(old, new, 1)
    return updated, {
        "patch_name": patch_name,
        "summary": summary,
        "applied": True,
        "status": "implemented",
        "lines_changed": f"{start_line}-{end_line}",
        "before_excerpt": old.strip(),
        "after_excerpt": new.strip(),
    }


def _apply_patch_group(
    text: str,
    patches: list[tuple[str, str, str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    patch_logs: list[dict[str, Any]] = []
    updated = text
    for old, new, patch_name, summary in patches:
        updated, patch_log = _replace_once(
            updated,
            old,
            new,
            patch_name=patch_name,
            summary=summary,
        )
        patch_logs.append(patch_log)
    return updated, patch_logs


def _supported_patch_spec(
    change: dict[str, Any],
) -> tuple[Path, list[tuple[str, str, str, str]]] | None:
    change_type = str(change.get("change_type", "") or "")
    failure_class = str(change.get("target_failure_class", "") or "")

    if change_type in {"evidence_selection", "provenance_guardrail"} or failure_class in {
        "evidence_selection_failure",
        "provenance_grounding_failure",
    }:
        return (
            AGENT_SERVING_PATH,
            [
                (
                    SUPPORTED_TRIM_HELPER_OLD,
                    SUPPORTED_TRIM_HELPER_NEW,
                    "documentary_evidence_trim_helper",
                    "Added claim-relevance ranking for supported email records.",
                ),
                (
                    CLAIM_VERIFICATION_TRIM_OLD,
                    CLAIM_VERIFICATION_TRIM_NEW,
                    "claim_verification_trim",
                    "Trimmed documentary evidence blocks to the top claim-relevant email records.",
                ),
            ],
        )
    if change_type == "synthesis_guardrail" or failure_class == "unsupported_synthesis_hallucination":
        return (
            AGENT_SERVING_PATH,
            [
                (
                    SYNTHESIS_GUARDRAIL_OLD,
                    SYNTHESIS_GUARDRAIL_NEW,
                    "documentary_synthesis_guardrail",
                    "Softened documentary synthesis when the evidence sufficiency decision is a hedge.",
                ),
            ],
        )
    if change_type == "latency_optimization" or failure_class == "redundant_tool_calls":
        return (
            AGENT_SERVING_PATH,
            [
                (
                    FAST_PATH_DRILLDOWN_OLD,
                    FAST_PATH_DRILLDOWN_NEW,
                    "fast_path_drilldown_budget",
                    "Reduced redundant fast-path full-body drill-down calls once evidence is already thin.",
                ),
            ],
        )
    if change_type == "abstention_policy" or failure_class == "abstention_failure":
        return (
            AGENT_SERVING_PATH,
            [
                (
                    ABSTENTION_SOFTEN_OLD,
                    ABSTENTION_SOFTEN_NEW,
                    "documentary_abstention_softening",
                    "Relaxed full abstention to a narrow hedge when claim-relevant documentary evidence exists.",
                ),
            ],
        )
    if change_type == "execution_config" or failure_class == "remote_when_local_possible":
        return (
            ORCHESTRATOR_PATH,
            [
                (
                    LOCAL_BACKEND_DEFAULTS_OLD,
                    LOCAL_BACKEND_DEFAULTS_NEW,
                    "local_backend_default",
                    "Prefer local DuckDB for factual evals when the exported Enron snapshot is present.",
                ),
            ],
        )
    return None


def _sum_line_spans(spans: list[str]) -> int:
    touched: set[int] = set()
    for span in spans:
        match = re.fullmatch(r"(\d+)-(\d+)", span)
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        touched.update(range(start, end + 1))
    return len(touched)


def apply_supported_changes(
    plan_payload: dict[str, Any],
    *,
    repo_root: str | Path = REPO_ROOT,
    agent_serving_path: str | Path | None = None,
    orchestrator_path: str | Path | None = None,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    target_paths: dict[Path, Path] = {
        AGENT_SERVING_PATH: Path(agent_serving_path)
        if agent_serving_path
        else repo_root / AGENT_SERVING_PATH,
        ORCHESTRATOR_PATH: Path(orchestrator_path)
        if orchestrator_path
        else repo_root / ORCHESTRATOR_PATH,
    }
    original_texts: dict[Path, str] = {}
    updated_texts: dict[Path, str] = {}
    changes_implemented: list[dict[str, Any]] = []
    changes_skipped: list[dict[str, Any]] = []
    touched_spans: list[str] = []
    changed_targets: set[Path] = set()

    for change in plan_payload.get("changes", []):
        patch_spec = _supported_patch_spec(change)
        if patch_spec is None:
            changes_skipped.append(
                {
                    "fix_id": change.get("id"),
                    "description": change.get("description"),
                    "reason": "No supported auto-apply patch exists for this change type.",
                }
            )
            continue
        target_rel_path, patches = patch_spec
        target_path = target_paths[target_rel_path]
        relative_path = str(target_rel_path)
        if target_rel_path not in updated_texts:
            original_texts[target_rel_path] = target_path.read_text()
            updated_texts[target_rel_path] = original_texts[target_rel_path]
        before = updated_texts[target_rel_path]
        updated, patch_logs = _apply_patch_group(before, patches)
        applied_spans = [
            log["lines_changed"]
            for log in patch_logs
            if log.get("applied") and log.get("lines_changed")
        ]
        if updated == before:
            changes_skipped.append(
                {
                    "fix_id": change.get("id"),
                    "description": change.get("description"),
                    "reason": f"The target guardrail is already present in {relative_path}.",
                }
            )
            continue
        updated_texts[target_rel_path] = updated
        changed_targets.add(target_rel_path)
        touched_spans.extend(applied_spans)
        changes_implemented.append(
            {
                "fix_id": change.get("id"),
                "files_modified": [relative_path],
                "lines_changed": ", ".join(applied_spans) if applied_spans else "unknown",
                "description": change.get("description"),
                "status": "implemented",
                "patches": patch_logs,
            }
        )

    for target_rel_path, updated_text in updated_texts.items():
        if updated_text != original_texts[target_rel_path]:
            target_paths[target_rel_path].write_text(updated_text)

    return {
        "changes_implemented": changes_implemented,
        "changes_skipped": changes_skipped,
        "total_files_modified": len(changed_targets),
        "total_lines_changed": _sum_line_spans([span for span in touched_spans if span]),
    }


def build_implementation_log(
    plan_payload: dict[str, Any],
    *,
    iteration: int,
    plan_path: str | Path,
    adapter_mode: str = "skip",
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, Any]:
    changes = plan_payload.get("changes", [])
    if plan_payload.get("plan_empty"):
        return {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": "command",
            "adapter_mode": adapter_mode,
            "status": "no_changes_required",
            "plan_empty": True,
            "changes_implemented": [],
            "changes_skipped": [],
            "total_files_modified": 0,
            "total_lines_changed": 0,
        }

    if adapter_mode == "skip":
        changes_skipped = [
            {
                "fix_id": change.get("id"),
                "description": change.get("description"),
                "reason": "Reference external adapter skip mode does not mutate code.",
            }
            for change in changes
        ]
        return {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": "command",
            "adapter_mode": adapter_mode,
            "status": "skipped",
            "plan_empty": False,
            "changes_implemented": [],
            "changes_skipped": changes_skipped,
            "total_files_modified": 0,
            "total_lines_changed": 0,
        }

    if adapter_mode == "simulate":
        changes_implemented = [
            {
                "fix_id": change.get("id"),
                "files_modified": change.get("files_to_modify", []),
                "lines_changed": "simulation",
                "description": (
                    "Simulated external implementer handoff for: "
                    f"{change.get('description')}"
                ),
                "status": "simulated",
            }
            for change in changes
        ]
        return {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": "command",
            "adapter_mode": adapter_mode,
            "status": "simulated",
            "plan_empty": False,
            "changes_implemented": changes_implemented,
            "changes_skipped": [],
            "total_files_modified": len(
                {
                    file_path
                    for change in changes_implemented
                    for file_path in change.get("files_modified", [])
                }
            ),
            "total_lines_changed": 0,
        }

    if adapter_mode == "apply":
        apply_result = apply_supported_changes(plan_payload, repo_root=repo_root)
        return {
            "version": "1.0",
            "created_at": _utc_now(),
            "iteration": iteration,
            "inputs": {
                "improvement_plan": str(plan_path),
            },
            "implementer_mode": "command",
            "adapter_mode": adapter_mode,
            "status": "implemented" if apply_result["changes_implemented"] else "skipped",
            "plan_empty": False,
            "changes_implemented": apply_result["changes_implemented"],
            "changes_skipped": apply_result["changes_skipped"],
            "total_files_modified": apply_result["total_files_modified"],
            "total_lines_changed": apply_result["total_lines_changed"],
        }

    raise ValueError(
        f"Unsupported adapter mode: {adapter_mode!r}. Use 'skip', 'simulate', or 'apply'."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reference external implementer adapter for factual hardening loops.",
    )
    parser.add_argument(
        "--plan",
        required=True,
        help="Path to improvement_plan.json.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to implementation_log.json.",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Loop iteration number for the emitted implementation log.",
    )
    parser.add_argument(
        "--adapter-mode",
        choices=["skip", "simulate", "apply"],
        default="skip",
        help="How the reference adapter should materialize the implementation log.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root used to locate src/agent/agent_serving.py for apply mode.",
    )
    args = parser.parse_args()

    plan_payload = _read_json(args.plan)
    payload = build_implementation_log(
        plan_payload,
        iteration=args.iteration,
        plan_path=args.plan,
        adapter_mode=args.adapter_mode,
        repo_root=args.repo_root,
    )
    _write_json(args.output, payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
