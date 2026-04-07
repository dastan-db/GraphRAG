"""Focused tests for factual hardening orchestration helpers."""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
from types import ModuleType

import pytest


def _install_fake_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    mlflow_mod = ModuleType("mlflow")
    mlflow_entities = ModuleType("mlflow.entities")
    mlflow_genai = ModuleType("mlflow.genai")
    mlflow_scorers = ModuleType("mlflow.genai.scorers")

    class _Feedback:
        def __init__(self, value=None, rationale=None):
            self.value = value
            self.rationale = rationale

    mlflow_entities.Feedback = _Feedback
    mlflow_scorers.scorer = lambda fn: fn
    mlflow_genai.scorers = mlflow_scorers
    mlflow_mod.entities = mlflow_entities
    mlflow_mod.genai = mlflow_genai

    pandas_mod = ModuleType("pandas")
    pandas_mod.DataFrame = object

    enron_eval_mod = ModuleType("src.evaluation.enron_evaluation")
    enron_eval_mod.DATA_CONTEXT = ""
    enron_eval_mod.answer_completeness = lambda *args, **kwargs: 0.0
    enron_eval_mod.factual_accuracy = lambda *args, **kwargs: 0.0
    enron_eval_mod.grounding_integrity = lambda *args, **kwargs: 0.0
    enron_eval_mod.hallucination_detection = lambda *args, **kwargs: 0.0

    promotion_mod = ModuleType("src.agent.enron_promotion")
    promotion_mod.build_promotion_manifest = (
        lambda artifact_dir, candidate_label, output_path: {
            "manifest_path": str(output_path),
            "candidate_label": candidate_label,
        }
    )

    question_bank_mod = ModuleType("src.evaluation.question_bank")
    question_bank_mod.export_governed_flat_questions = lambda corpus="enron": []

    fake_modules = {
        "mlflow": mlflow_mod,
        "mlflow.entities": mlflow_entities,
        "mlflow.genai": mlflow_genai,
        "mlflow.genai.scorers": mlflow_scorers,
        "pandas": pandas_mod,
        "src.evaluation.enron_evaluation": enron_eval_mod,
        "src.agent.enron_promotion": promotion_mod,
        "src.evaluation.question_bank": question_bank_mod,
    }
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture(autouse=True)
def _mock_heavy_imports(monkeypatch: pytest.MonkeyPatch):
    _install_fake_modules(monkeypatch)
    yield


@pytest.fixture()
def mod():
    import src.agent.factual_parallel_orchestrator as _mod

    importlib.reload(_mod)
    return _mod


def _quality_payload(score: float = 0.6) -> dict:
    return {
        "version": "1.0",
        "created_at": "2026-04-03T00:00:00Z",
        "overall_metrics": {
            "benchmark_score": score,
            "factual_accuracy": score,
            "grounding_integrity": score,
            "hallucination_detection": score,
            "answer_completeness": score,
            "citation_accuracy": score,
            "evidence_fabrication": score,
            "provenance_structure_compliance": score,
            "provenance_content_quality": min(score, 0.35),
        },
        "reproducibility": {
            "exact_match_rate": 0.5,
            "token_jaccard_mean": 0.5,
        },
        "questions": [
            {
                "question_id": "q_001",
                "question": "Who reported to Ken Lay?",
                "status": "ok",
                "primitive": "entity_structure",
                "runtime_primary_pattern": "entity_structure",
                "benchmark_score": score,
                "factual_accuracy": score,
                "grounding_integrity": score,
                "hallucination_detection": score,
                "answer_completeness": score,
                "citation_accuracy": score,
                "evidence_fabrication": score,
                "provenance_structure_compliance": score,
                "provenance_content_quality": min(score, 0.35),
            }
        ],
    }


def _latency_payload(mean_ms: float = 1000.0) -> dict:
    return {
        "version": "1.0",
        "created_at": "2026-04-03T00:00:00Z",
        "runtime": {
            "mean_ms": mean_ms,
            "p50_ms": mean_ms,
            "p95_ms": mean_ms,
            "p99_ms": mean_ms,
            "avg_tool_count": 1.0,
            "avg_cache_lookup_count": 0.0,
            "avg_cache_hit_rate": 0.0,
            "sla_ms": 15000,
        },
        "questions": [
            {
                "question_id": "q_001",
                "question": "Who reported to Ken Lay?",
                "status": "ok",
                "primitive": "entity_structure",
                "runtime_primary_pattern": "entity_structure",
                "latency_ms": mean_ms,
                "tool_count": 1,
                "planner_called": False,
                "planner_bypass": True,
                "cache_lookup_count": 0,
                "cache_hit_rate": 0.0,
            }
        ],
    }


def _minimal_agent_serving_fixture() -> str:
    return '''import re

CORPUS = "enron"

def _normalize_citation_field(value: str) -> str:
    return re.sub(r"\\s+", " ", re.sub(r"[^\\w\\s@.-]", " ", value.lower())).strip()


def _citation_is_supported(date: str, sender: str, subject: str, supported: list[dict]) -> bool:
    norm_sender = _normalize_citation_field(sender)
    norm_subject = _normalize_citation_field(subject)
    for record in supported:
        if date and record.get("date") and record["date"] != date:
            continue
        record_sender = _normalize_citation_field(record.get("sender", ""))
        record_subject = _normalize_citation_field(record.get("subject", ""))
        sender_ok = not norm_sender or not record_sender or norm_sender in record_sender or record_sender in norm_sender
        subject_ok = (
            not norm_subject
            or not record_subject
            or norm_subject in record_subject
            or record_subject in norm_subject
        )
        if sender_ok and subject_ok:
            return True
    return False


def _assess_evidence_sufficiency(
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
    consistency_warnings: list[str] | None = None,
    pattern_name: str = "",
) -> dict:
    contract = contract or {}
    features = _collect_evidence_features(tool_entries, question=question, contract=contract)
    timeline_backed_documentary_packet = _has_timeline_backed_documentary_packet(
        question,
        features=features,
        contract=contract,
    )
    decision = "answer"
    reasons: list[str] = []
    answer_type = str(contract.get("answer_type", "unknown") or "unknown")

    if contract.get("requires_evidence"):
        if features["query_relevant_email_hits"] == 0 and not timeline_backed_documentary_packet:
            reasons.append("No query-relevant email evidence was retrieved for the requested documentary claim.")
            if pattern_name in {"keyword_search", "timeline"} or answer_type == "proof_email":
                decision = _escalate_sufficiency_decision(decision, "abstain")
            else:
                decision = _escalate_sufficiency_decision(decision, "hedge")
        elif (
            features["high_signal_query_hits"] == 0
            and features["full_body_hits"] == 0
            and not timeline_backed_documentary_packet
        ):
            reasons.append(
                "The retrieved emails provide only weak topical support and do not directly verify the requested claim."
            )
            decision = _escalate_sufficiency_decision(decision, "hedge")

    if answer_type == "count" and features["meaningful_count"] < EVIDENCE_CONFIG["evidence_sufficiency_threshold"]:
        reasons.append("The count/ranking request has limited supporting rows.")
        decision = _escalate_sufficiency_decision(decision, "hedge")

    return {
        "decision": decision,
        "reasons": list(dict.fromkeys(reasons)),
        "features": features,
        "answer_type": answer_type,
    }


def _soften_overclaiming_language(text: str, assessment: dict) -> str:
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


def _apply_claim_verification(
    response_text: str,
    tool_entries: list[tuple[str, str]],
    evidence_strength: str,
    *,
    question: str = "",
    contract: dict | None = None,
    consistency_warnings: list[str] | None = None,
) -> str:
    assessment = _assess_evidence_sufficiency(
        tool_entries,
        evidence_strength,
        question=question,
        contract=contract,
        consistency_warnings=consistency_warnings,
    )
    supported = _extract_supported_email_records(tool_entries, question=question, contract=contract)
    if contract and contract.get("requires_evidence") and not supported and assessment.get("decision") != "answer":
        return _render_abstention_response(
            tool_entries,
            evidence_strength,
            assessment,
            question=question,
            contract=contract,
        )
    verified = _remove_unsupported_inline_citations(response_text, supported)
    verified = _clean_supporting_evidence_section(verified, supported)
    if contract and contract.get("requires_evidence"):
        support_block = _build_canonical_supporting_evidence_block(
            supported,
            limit=len(supported) or 3,
        )
        if support_block:
            verified = _insert_section_before_provenance(verified, support_block)
    verified = _soften_overclaiming_language(verified, assessment)
    return _ensure_answer_header(verified)


def _execute_fast_path_stream(pattern, contract, tool_results, shortcut_documentary, sufficiency):
    followup_steps = []
        if (
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
    return followup_steps


PROMPT = """
## Response Guidelines
- **Prioritize completeness.** List ALL entities and relationships returned by tools. Name every person found, even if tangentially related.
- **Cite email evidence inline** using this format: [YYYY-MM-DD, From: sender, Subject: topic]. Include at least 2-3 specific email citations when evidence is available.
"""
'''


def _minimal_orchestrator_fixture() -> str:
    return '''import os
from pathlib import Path

os.environ.setdefault("GRAPHRAG_BACKEND", "lakebase")
os.environ.setdefault("GRAPHRAG_CORPUS", "enron")
os.environ.setdefault("GRAPHRAG_SCHEMA", "graphrag_enron")
'''


class TestOrchestrateMeasure:
    def test_runs_quality_and_latency_in_parallel(self, mod, monkeypatch, tmp_path):
        quality_started = threading.Event()
        latency_started = threading.Event()
        observations: dict[str, object] = {}

        def fake_build(output_path, limit=None):
            payload = {
                "version": "1.0",
                "questions": [],
                "composition_summary": {},
            }
            mod._write_json(output_path, payload)
            return payload

        def fake_quality(
            benchmark_path,
            output_path,
            *,
            max_concurrent_questions,
            max_concurrent_judge_calls,
            limit,
        ):
            quality_started.set()
            observations["quality_saw_latency"] = latency_started.wait(0.5)
            payload = _quality_payload()
            mod._write_json(output_path, payload)
            return payload

        def fake_latency(
            benchmark_path,
            output_path,
            *,
            mode,
            max_concurrent_questions,
            limit,
            sla_ms,
            precomputed_rows,
        ):
            latency_started.set()
            observations["latency_saw_quality"] = quality_started.wait(0.5)
            observations["latency_precomputed_rows"] = precomputed_rows
            payload = _latency_payload()
            mod._write_json(output_path, payload)
            return payload

        monkeypatch.setattr(mod, "build_benchmark_definition", fake_build)
        monkeypatch.setattr(mod, "run_quality_evaluation", fake_quality)
        monkeypatch.setattr(mod, "run_latency_evaluation", fake_latency)

        payload = mod.orchestrate_measure(
            artifact_dir=tmp_path,
            iteration=1,
            label="baseline",
            parallel_subagents=True,
        )

        assert observations["quality_saw_latency"] is True
        assert observations["latency_saw_quality"] is True
        assert observations["latency_precomputed_rows"] is None
        assert payload["quality"].endswith("factual_baseline_quality.json")
        assert payload["latency"].endswith("factual_baseline_latency.json")


class TestOrchestrateImplement:
    def test_noop_mode_writes_skipped_log(self, mod, tmp_path):
        mod._write_json(
            tmp_path / "improvement_plan.json",
            {
                "version": "1.0",
                "iteration": 1,
                "changes": [
                    {
                        "id": "fix_001",
                        "description": "Tighten documentary routing",
                        "files_to_modify": ["src/agent/agent_serving.py"],
                    }
                ],
                "plan_empty": False,
            },
        )
        mod._write_json(
            tmp_path / "loop_state.json",
            {
                "version": "1.0",
                "iteration": 1,
                "phase": "PLAN_COMPLETE",
                "history": [
                    {
                        "iteration": 1,
                        "label": "baseline",
                        "phase": "PLAN_COMPLETE",
                        "artifacts": {},
                    }
                ],
            },
        )

        payload = mod.orchestrate_implement(
            artifact_dir=tmp_path,
            iteration=1,
            implementer_mode="noop",
        )

        implementation_log = mod._read_json(tmp_path / "implementation_log.json")
        loop_state = mod._read_json(tmp_path / "loop_state.json")

        assert payload["status"] == "skipped"
        assert implementation_log["changes_implemented"] == []
        assert implementation_log["changes_skipped"][0]["fix_id"] == "fix_001"
        assert loop_state["phase"] == "IMPLEMENT_COMPLETE"
        assert (
            loop_state["history"][0]["artifacts"]["implementation_log"]
            == str(tmp_path / "implementation_log.json")
        )

    def test_command_mode_runs_external_adapter(self, mod, monkeypatch, tmp_path):
        mod._write_json(
            tmp_path / "improvement_plan.json",
            {
                "version": "1.0",
                "iteration": 1,
                "changes": [
                    {
                        "id": "fix_001",
                        "description": "Tighten documentary routing",
                        "files_to_modify": ["src/agent/agent_serving.py"],
                    }
                ],
                "plan_empty": False,
            },
        )
        mod._write_json(
            tmp_path / "loop_state.json",
            {
                "version": "1.0",
                "iteration": 1,
                "phase": "PLAN_COMPLETE",
                "history": [
                    {
                        "iteration": 1,
                        "label": "baseline",
                        "phase": "PLAN_COMPLETE",
                        "artifacts": {},
                    }
                ],
            },
        )

        def fake_run(cmd, **kwargs):
            mod._write_json(
                tmp_path / "implementation_log.json",
                {
                    "version": "1.0",
                    "iteration": 1,
                    "changes_implemented": [],
                    "changes_skipped": [
                        {
                            "fix_id": "fix_001",
                            "description": "Tighten documentary routing",
                            "reason": "adapter wrote the artifact",
                        }
                    ],
                    "total_files_modified": 0,
                    "total_lines_changed": 0,
                },
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        payload = mod.orchestrate_implement(
            artifact_dir=tmp_path,
            iteration=1,
            implementer_mode="command",
            implementer_command=(
                "python -m src.agent.factual_external_implementer "
                "--plan {improvement_plan} --output {implementation_log} "
                "--iteration {iteration}"
            ),
        )

        implementation_log = mod._read_json(tmp_path / "implementation_log.json")

        assert payload["status"] == "skipped"
        assert implementation_log["implementer_mode"] == "command"
        assert implementation_log["implementer_command"].startswith("python -m")


class TestExternalImplementerAdapter:
    def test_build_implementation_log_skip_mode(self, tmp_path):
        from src.agent.factual_external_implementer import build_implementation_log

        payload = build_implementation_log(
            {
                "version": "1.0",
                "changes": [
                    {
                        "id": "fix_001",
                        "description": "Tighten documentary routing",
                        "files_to_modify": ["src/agent/agent_serving.py"],
                    }
                ],
                "plan_empty": False,
            },
            iteration=2,
            plan_path=tmp_path / "improvement_plan.json",
            adapter_mode="skip",
        )

        assert payload["status"] == "skipped"
        assert payload["changes_skipped"][0]["fix_id"] == "fix_001"
        assert payload["changes_implemented"] == []

    def test_build_implementation_log_simulate_mode(self, tmp_path):
        from src.agent.factual_external_implementer import build_implementation_log

        payload = build_implementation_log(
            {
                "version": "1.0",
                "changes": [
                    {
                        "id": "fix_002",
                        "description": "Add evidence sufficiency guard",
                        "files_to_modify": [
                            "src/agent/agent_serving.py",
                            "src/agent/pattern_registry.py",
                        ],
                    }
                ],
                "plan_empty": False,
            },
            iteration=3,
            plan_path=tmp_path / "improvement_plan.json",
            adapter_mode="simulate",
        )

        assert payload["status"] == "simulated"
        assert payload["changes_implemented"][0]["fix_id"] == "fix_002"
        assert payload["total_files_modified"] == 2

    def test_build_implementation_log_apply_mode_mutates_agent_file(self, tmp_path):
        from src.agent.factual_external_implementer import build_implementation_log

        agent_path = tmp_path / "src" / "agent" / "_agent_core.py"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(_minimal_agent_serving_fixture())
        orchestrator_path = (
            tmp_path / "src" / "_internal" / "agent" / "factual_parallel_orchestrator.py"
        )
        orchestrator_path.parent.mkdir(parents=True, exist_ok=True)
        orchestrator_path.write_text(_minimal_orchestrator_fixture())

        payload = build_implementation_log(
            {
                "version": "1.0",
                "changes": [
                    {
                        "id": "fix_001",
                        "description": "Tighten synthesis prompts so unsupported narrative is replaced with explicit uncertainty or abstention when evidence is weak.",
                        "target_failure_class": "unsupported_synthesis_hallucination",
                        "change_type": "synthesis_guardrail",
                    },
                    {
                        "id": "fix_002",
                        "description": "Limit provenance to claim-supporting sources and downgrade grounding when only broad context or partial evidence was retrieved.",
                        "target_failure_class": "provenance_grounding_failure",
                        "change_type": "provenance_guardrail",
                    },
                    {
                        "id": "fix_003",
                        "description": "Trim duplicate follow-up evidence calls once the answer contract has already been satisfied or abstention is determined.",
                        "target_failure_class": "redundant_tool_calls",
                        "change_type": "latency_optimization",
                    },
                    {
                        "id": "fix_004",
                        "description": "Differentiate healthy documentary abstention from over-abstention by adding one more targeted evidence pass before final refusal.",
                        "target_failure_class": "abstention_failure",
                        "change_type": "abstention_policy",
                    },
                    {
                        "id": "fix_005",
                        "description": "Default factual benchmark runs to the local backend when the required local export is present and reserve remote mode for parity checks.",
                        "target_failure_class": "remote_when_local_possible",
                        "change_type": "execution_config",
                    },
                ],
                "plan_empty": False,
            },
            iteration=4,
            plan_path=tmp_path / "improvement_plan.json",
            adapter_mode="apply",
            repo_root=tmp_path,
        )

        mutated_agent = agent_path.read_text()
        mutated_orchestrator = orchestrator_path.read_text()

        assert payload["status"] == "implemented"
        assert len(payload["changes_implemented"]) == 5
        assert payload["changes_skipped"] == []
        assert "partial support rather than definitive proof" in mutated_agent
        assert "def _trim_supported_email_records(" in mutated_agent
        assert 'drill_limit = 2 if contract.get("requires_evidence") else 1' in mutated_agent
        assert 'if sufficiency["decision"] == "hedge" and contract.get("requires_evidence"):' in mutated_agent
        assert 'drill_params["limit"] = 1' in mutated_agent
        assert "At least one claim-relevant email was retrieved" in mutated_agent
        assert "LOCAL_ENRON_EXPORT_PATH = Path(\"data/graphrag_enron.duckdb\")" in mutated_orchestrator
        assert "os.environ.setdefault(\"GRAPHRAG_LOCAL_DB\", \"data/graphrag_enron.duckdb\")" in mutated_orchestrator


class TestOrchestrateLoop:
    def test_plan_empty_stops_loop_and_emits_final_report(
        self,
        mod,
        monkeypatch,
        tmp_path,
    ):
        measure_calls: list[tuple[int, str]] = []

        def fake_measure(
            *,
            artifact_dir,
            iteration,
            label,
            limit,
            parallel_subagents,
            max_concurrent_questions,
            max_concurrent_judge_calls,
            latency_mode,
            latency_sla_ms,
            subagent_timeout_seconds,
        ):
            artifact_root = tmp_path
            measure_calls.append((iteration, label))
            benchmark_path = artifact_root / "factual_benchmark_definition.json"
            quality_path = artifact_root / f"factual_{label}_quality.json"
            latency_path = artifact_root / f"factual_{label}_latency.json"
            if not benchmark_path.exists():
                mod._write_json(
                    benchmark_path,
                    {
                        "version": "1.0",
                        "questions": [],
                        "composition_summary": {},
                    },
                )
            mod._write_json(quality_path, _quality_payload())
            mod._write_json(latency_path, _latency_payload())
            loop_state_path = artifact_root / "loop_state.json"
            loop_state = mod._load_or_init_loop_state(loop_state_path)
            loop_state["iteration"] = iteration
            loop_state["phase"] = "MEASURE_COMPLETE"
            entry = mod._ensure_history_entry(
                loop_state,
                iteration=iteration,
                label=label,
                phase="MEASURE_COMPLETE",
            )
            entry["artifacts"]["benchmark_definition"] = str(benchmark_path)
            entry["artifacts"][f"{label}_quality"] = str(quality_path)
            entry["artifacts"][f"{label}_latency"] = str(latency_path)
            mod._write_json(loop_state_path, loop_state)
            return {
                "benchmark_definition": str(benchmark_path),
                "quality": str(quality_path),
                "latency": str(latency_path),
                "loop_state": str(loop_state_path),
                "quality_summary": _quality_payload()["overall_metrics"],
                "latency_summary": _latency_payload()["runtime"],
            }

        def fake_analyze(
            *,
            artifact_dir,
            quality_label,
            iteration,
            prior_assessment_path,
        ):
            artifact_root = tmp_path
            mod._write_json(
                artifact_root / "failure_taxonomy.json",
                {
                    "version": "1.0",
                    "iteration": iteration,
                    "quality_failures": [
                        {
                            "category": "routing_classification_failure",
                            "count": 1,
                        }
                    ],
                    "latency_failures": [],
                    "ranked_by_impact": ["routing_classification_failure"],
                },
            )
            mod._write_json(
                artifact_root / "root_cause_report.json",
                {
                    "version": "1.0",
                    "investigated_failure_classes": [],
                },
            )
            mod._write_json(
                artifact_root / "improvement_plan.json",
                {
                    "version": "1.0",
                    "iteration": iteration,
                    "changes": [],
                    "plan_empty": True,
                },
            )
            loop_state_path = artifact_root / "loop_state.json"
            loop_state = mod._load_or_init_loop_state(loop_state_path)
            loop_state["iteration"] = iteration
            loop_state["phase"] = "PLAN_COMPLETE"
            entry = mod._ensure_history_entry(
                loop_state,
                iteration=iteration,
                label=quality_label,
                phase="PLAN_COMPLETE",
            )
            entry["artifacts"]["failure_taxonomy"] = str(artifact_root / "failure_taxonomy.json")
            entry["artifacts"]["root_cause_report"] = str(artifact_root / "root_cause_report.json")
            entry["artifacts"]["improvement_plan"] = str(artifact_root / "improvement_plan.json")
            entry["improvement_plan"] = {"changes": [], "plan_empty": True}
            mod._write_json(loop_state_path, loop_state)
            return {
                "failure_taxonomy": str(artifact_root / "failure_taxonomy.json"),
                "root_cause_report": str(artifact_root / "root_cause_report.json"),
                "improvement_plan": str(artifact_root / "improvement_plan.json"),
                "loop_state": str(loop_state_path),
                "ranked_failures": ["routing_classification_failure"],
                "planned_changes": 0,
            }

        monkeypatch.setattr(mod, "orchestrate_measure", fake_measure)
        monkeypatch.setattr(mod, "orchestrate_analyze", fake_analyze)

        payload = mod.orchestrate_loop(
            artifact_dir=tmp_path,
            max_iterations=3,
            implementer_mode="noop",
        )

        final_report = mod._read_json(tmp_path / "final_report.json")

        assert measure_calls == [(1, "baseline")]
        assert payload["termination_reason"] == "EXHAUSTED"
        assert final_report["termination_reason"] == "EXHAUSTED"
        assert (tmp_path / "factual_postchange_quality.json").exists()
        assert (tmp_path / "factual_postchange_latency.json").exists()
        assert (tmp_path / "iterations" / "iter_1" / "archive_manifest.json").exists()
