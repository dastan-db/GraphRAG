"""Deploy and promote the Enron GraphRAG agent on Model Serving.

This script now uses the shared Enron promotion contract so notebook- and
script-based deploys package the same code paths, set the same runtime env,
and gate live promotion against a narrow deployed quality/latency slice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

import mlflow

from src.agent.enron_promotion import (
    DEFAULT_LLM_ENDPOINT,
    DEFAULT_PROMOTION_MANIFEST,
    DEFAULT_SMALL_LLM_ENDPOINT,
    DEFAULT_SYNTHESIS_ENDPOINT,
    DEFAULT_REACT_ENDPOINT,
    DEFAULT_WAREHOUSE_ID,
    ENRON_ENDPOINT_NAME,
    ENRON_REGISTERED_MODEL,
    assert_enron_lakebase_ready,
    build_default_gate_thresholds,
    build_enron_log_model_kwargs,
    build_enron_serving_environment,
    build_promotion_manifest,
    enron_model_logging_env,
    evaluate_deployed_gate,
    get_live_endpoint_state,
    load_promotion_manifest,
)
from src.evaluation.question_bank import ENRON_CORE_EVAL_DATA
from eval_deployed import run_deployed_evaluation


DEFAULT_GATE_OUTPUT = ROOT_DIR / "data" / "enron_deployed_gate.json"
DEFAULT_LATENCY_OUTPUT = ROOT_DIR / "data" / "enron_deployed_latency.json"
DEFAULT_PROMOTION_OUTPUT = ROOT_DIR / "data" / "enron_promotion_result.json"


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    resolved = Path(path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2))


def _load_manifest(path: str | Path | None, artifact_dir: str | Path) -> dict[str, Any]:
    manifest_path = (
        Path(path).resolve()
        if path is not None
        else (Path(artifact_dir).resolve() / DEFAULT_PROMOTION_MANIFEST)
    )
    if manifest_path.exists():
        manifest = load_promotion_manifest(manifest_path)
        manifest["manifest_path"] = str(manifest_path)
        return manifest

    manifest = build_promotion_manifest(
        artifact_dir=artifact_dir,
        output_path=manifest_path,
    )
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _deployment_contract(manifest: dict[str, Any] | None = None) -> tuple[str, dict[str, str], dict[str, Any]]:
    manifest = manifest or {}
    deploy_target = manifest.get("deploy_target", {})
    contract = manifest.get("promotion_contract", {})
    env = dict(contract.get("environment_vars") or {})
    if not env:
        env = build_enron_serving_environment()
    model_name = deploy_target.get("registered_model_name") or ENRON_REGISTERED_MODEL
    return model_name, env, deploy_target


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(pct / 100 * len(ordered)) - 1
    return round(ordered[max(0, min(index, len(ordered) - 1))], 1)


def _query_endpoint(question: str, endpoint_name: str = ENRON_ENDPOINT_NAME) -> tuple[str, float, str | None]:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    started = time.perf_counter()
    try:
        response = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={"input": [{"role": "user", "content": question}]},
        )
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return "", elapsed_ms, str(exc)

    texts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part["text"])
        elif "text" in item:
            texts.append(item["text"])
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return "\n".join(texts), elapsed_ms, None


def _wait_for_endpoint_ready(
    endpoint_name: str = ENRON_ENDPOINT_NAME,
    *,
    max_wait_s: int = 2400,
    poll_s: int = 30,
) -> dict[str, Any]:
    elapsed = 0
    while elapsed < max_wait_s:
        state = get_live_endpoint_state(endpoint_name)
        print(
            f"  [{elapsed}s] ready={state.get('ready')}, "
            f"config_update={state.get('config_update')}"
        )
        config_update = state.get("config_update")
        config_idle = config_update in (
            None,
            "NOT_UPDATING",
            "EndpointStateConfigUpdate.NOT_UPDATING",
        )
        if "READY" in str(state.get("ready")) and config_idle:
            print(f"\nEndpoint '{endpoint_name}' is READY.")
            return state
        time.sleep(poll_s)
        elapsed += poll_s
    raise TimeoutError(f"Endpoint '{endpoint_name}' did not become READY within {max_wait_s}s.")


def _current_live_version(endpoint_name: str = ENRON_ENDPOINT_NAME) -> str | None:
    try:
        state = get_live_endpoint_state(endpoint_name)
    except Exception:
        return None
    entities = state.get("served_entities") or []
    if not entities:
        return None
    return str(entities[0].get("entity_version") or "") or None


def get_latest_registered_version(
    registered_model_name: str = ENRON_REGISTERED_MODEL,
) -> str | None:
    from mlflow import MlflowClient

    mlflow.set_registry_uri("databricks-uc")
    client = MlflowClient()
    versions = list(client.search_model_versions(f"name='{registered_model_name}'"))
    if not versions:
        return None
    latest = max(versions, key=lambda item: int(item.version))
    return str(latest.version)


def step_log_model(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    model_name, serving_env, _deploy_target = _deployment_contract(manifest)
    catalog, schema, _model = model_name.split(".", 2)
    if serving_env.get("GRAPHRAG_BACKEND") == "lakebase":
        readiness = assert_enron_lakebase_ready(
            endpoint_name=serving_env.get("LAKEBASE_ENDPOINT"),
        )
        print(
            "Lakebase ready:"
            f" endpoint={readiness['endpoint_name']} host={readiness['host']}"
        )

    mlflow.set_registry_uri("databricks-uc")
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID") or DEFAULT_WAREHOUSE_ID
    log_kwargs = build_enron_log_model_kwargs(
        ROOT_DIR,
        catalog=catalog,
        schema=schema,
        llm_endpoint=serving_env.get("GRAPHRAG_LLM_ENDPOINT", DEFAULT_LLM_ENDPOINT),
        small_llm_endpoint=serving_env.get(
            "GRAPHRAG_SMALL_LLM_ENDPOINT",
            DEFAULT_SMALL_LLM_ENDPOINT,
        ),
        warehouse_id=warehouse_id,
    )

    with enron_model_logging_env(
        schema=schema,
        llm_endpoint=serving_env.get("GRAPHRAG_LLM_ENDPOINT", DEFAULT_LLM_ENDPOINT),
        small_llm_endpoint=serving_env.get(
            "GRAPHRAG_SMALL_LLM_ENDPOINT",
            DEFAULT_SMALL_LLM_ENDPOINT,
        ),
        synthesis_endpoint=serving_env.get(
            "GRAPHRAG_SYNTHESIS_ENDPOINT",
            DEFAULT_SYNTHESIS_ENDPOINT,
        ),
        react_endpoint=serving_env.get("GRAPHRAG_REACT_ENDPOINT", DEFAULT_REACT_ENDPOINT),
        backend=serving_env.get("GRAPHRAG_BACKEND", "lakebase"),
        llm_provider=serving_env.get("GRAPHRAG_LLM_PROVIDER", "databricks"),
        lakebase_endpoint=serving_env.get("LAKEBASE_ENDPOINT"),
        lakebase_host=serving_env.get("LAKEBASE_HOST"),
        lakebase_dbname=serving_env.get("LAKEBASE_DBNAME"),
    ):
        with mlflow.start_run(run_name="graphrag_enron_agent"):
            model_info = mlflow.pyfunc.log_model(**log_kwargs)

    payload = {
        "model_uri": model_info.model_uri,
        "registered_model_name": model_name,
        "registered_model_version": str(model_info.registered_model_version),
    }
    print(f"Model logged: {payload['model_uri']}")
    print(f"Registered version: {payload['registered_model_version']}")
    return payload


def step_deploy(
    version: str,
    *,
    manifest: dict[str, Any] | None = None,
    wait_for_ready: bool = True,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    from databricks import agents

    model_name, serving_env, deploy_target = _deployment_contract(manifest)
    endpoint_name = deploy_target.get("endpoint_name") or ENRON_ENDPOINT_NAME
    if serving_env.get("GRAPHRAG_BACKEND") == "lakebase":
        readiness = assert_enron_lakebase_ready(
            endpoint_name=serving_env.get("LAKEBASE_ENDPOINT"),
        )
        print(
            "Lakebase ready:"
            f" endpoint={readiness['endpoint_name']} host={readiness['host']}"
        )

    deployment_tags = {
        "source": "graphrag_solacc",
        "corpus": "enron",
        "backend": serving_env.get("GRAPHRAG_BACKEND", "lakebase"),
    }
    if manifest and manifest.get("manifest_path"):
        deployment_tags["promotion_manifest"] = Path(
            manifest["manifest_path"]
        ).name
    if tags:
        deployment_tags.update(tags)

    try:
        deployment = agents.deploy(
            model_name,
            version,
            endpoint_name=endpoint_name,
            environment_vars=serving_env,
            tags=deployment_tags,
        )
        print(f"Deployment initiated: {deployment.endpoint_name}")
    except ValueError as exc:
        if "currently updating" in str(exc):
            print(f"Endpoint '{endpoint_name}' is already updating. Waiting for it to finish.")
        else:
            raise

    if wait_for_ready:
        return _wait_for_endpoint_ready(endpoint_name)
    return get_live_endpoint_state(endpoint_name)


def step_status(endpoint_name: str = ENRON_ENDPOINT_NAME) -> dict[str, Any]:
    state = get_live_endpoint_state(endpoint_name)
    print(f"Endpoint: {state['endpoint_name']}")
    print(f"  ready={state.get('ready')} config_update={state.get('config_update')}")
    for entity in state.get("served_entities", []):
        print(f"  entity: {entity['entity_name']} v{entity['entity_version']}")
    return state


def step_test(
    *,
    endpoint_name: str = ENRON_ENDPOINT_NAME,
    questions: list[str] | None = None,
) -> dict[str, Any]:
    smoke_questions = questions or [
        "Who communicated most frequently with Kenneth Lay?",
        "Who reported to Jeff Skilling?",
        "How are Kenneth Lay and Tim Belden connected?",
    ]
    results = []
    for question in smoke_questions:
        print(f"\nQ: {question}")
        print("-" * 60)
        text, latency_ms, error = _query_endpoint(question, endpoint_name=endpoint_name)
        if error:
            print(f"ERROR: {error}")
            results.append(
                {
                    "question": question,
                    "latency_ms": latency_ms,
                    "status": "error",
                    "error": error,
                }
            )
            continue
        print(text[:500])
        results.append(
            {
                "question": question,
                "latency_ms": latency_ms,
                "status": "ok",
                "preview": text[:500],
            }
        )

    passed = all(result["status"] == "ok" for result in results)
    return {
        "version": "1.0",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint_name": endpoint_name,
        "passed": passed,
        "questions": results,
    }


def _select_gate_dataset(
    *,
    cases: int | None = None,
    split: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(ENRON_CORE_EVAL_DATA)
    if category:
        rows = [row for row in rows if row["category"] == category]
    if split:
        rows = [row for row in rows if row.get("eval_split") == split]
    if cases:
        rows = rows[:cases]
    return rows


def run_deployed_latency_slice(
    *,
    endpoint_name: str = ENRON_ENDPOINT_NAME,
    cases: int | None = None,
    split: str | None = None,
    category: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    rows = _select_gate_dataset(cases=cases, split=split, category=category)
    started = time.perf_counter()
    question_results = []
    latencies = []
    for row in rows:
        text, latency_ms, error = _query_endpoint(row["question"], endpoint_name=endpoint_name)
        status = "ok" if error is None else "error"
        if error is None:
            latencies.append(latency_ms)
        question_results.append(
            {
                "question_id": row.get("id"),
                "question": row["question"],
                "category": row["category"],
                "eval_split": row.get("eval_split"),
                "latency_ms": latency_ms,
                "status": status,
                "error": error,
                "answer_preview": text[:200] if text else "",
            }
        )

    payload = {
        "version": "1.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint_name": endpoint_name,
        "slice_question_count": len(question_results),
        "successful_question_count": len(latencies),
        "error_question_count": len(question_results) - len(latencies),
        "elapsed_s": round(time.perf_counter() - started, 1),
        "runtime": {
            "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
        },
        "questions": question_results,
    }
    if output_path:
        _write_json(output_path, payload)
    return payload


def run_gate(
    manifest: dict[str, Any],
    *,
    split: str | None = None,
    cases: int | None = None,
    category: str | None = None,
    output_path: str | Path = DEFAULT_GATE_OUTPUT,
    latency_output_path: str | Path = DEFAULT_LATENCY_OUTPUT,
) -> dict[str, Any]:
    thresholds = manifest.get("promotion_contract", {}).get("gate_thresholds") or build_default_gate_thresholds(
        manifest.get("local_candidate", {}).get("quality_summary"),
        manifest.get("local_candidate", {}).get("latency_summary"),
    )
    resolved_split = split or thresholds.get("split")
    resolved_cases = cases or thresholds.get("cases")
    endpoint_name = manifest.get("deploy_target", {}).get("endpoint_name") or ENRON_ENDPOINT_NAME
    quality_output_path = Path(output_path).with_name("enron_deployed_quality.json")

    quality_payload = run_deployed_evaluation(
        cases=resolved_cases,
        category=category,
        split=resolved_split,
        run_name="enron_promotion_deployed_gate",
        endpoint_name=endpoint_name,
        output_json=str(quality_output_path),
    )
    latency_payload = run_deployed_latency_slice(
        endpoint_name=endpoint_name,
        cases=resolved_cases,
        split=resolved_split,
        category=category,
        output_path=latency_output_path,
    )
    payload = evaluate_deployed_gate(manifest, quality_payload, latency_payload)
    payload["quality_payload"] = quality_payload
    payload["latency_payload"] = latency_payload
    payload["cases"] = resolved_cases
    payload["split"] = resolved_split
    payload["category"] = category
    payload["quality_output"] = str(quality_output_path)
    payload["latency_output"] = str(Path(latency_output_path).resolve())
    _write_json(output_path, payload)
    return payload


def promote_in_place(
    *,
    manifest_path: str | Path | None = None,
    artifact_dir: str | Path = ROOT_DIR / "data",
    version: str | None = None,
    skip_log: bool = False,
    no_rollback: bool = False,
    gate_cases: int | None = None,
    gate_split: str | None = None,
    gate_category: str | None = None,
    output_path: str | Path = DEFAULT_PROMOTION_OUTPUT,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path, artifact_dir)
    assessment_verdict = manifest.get("local_candidate", {}).get("assessment_verdict")
    if assessment_verdict and assessment_verdict != "READY":
        raise RuntimeError(
            f"Promotion manifest is not ready for live promotion (verdict={assessment_verdict})."
        )
    previous_version = _current_live_version(
        manifest.get("deploy_target", {}).get("endpoint_name") or ENRON_ENDPOINT_NAME
    )

    log_payload = None
    candidate_version = version
    if candidate_version is None and not skip_log:
        log_payload = step_log_model(manifest)
        candidate_version = log_payload["registered_model_version"]
    if candidate_version is None:
        candidate_version = get_latest_registered_version(
            manifest.get("deploy_target", {}).get("registered_model_name")
            or ENRON_REGISTERED_MODEL
        )
    if candidate_version is None:
        raise RuntimeError("Unable to determine a candidate model version to promote.")

    deploy_payload = step_deploy(
        candidate_version,
        manifest=manifest,
        tags={"promotion_mode": "deploy_in_place"},
    )
    smoke_payload = step_test(
        endpoint_name=manifest.get("deploy_target", {}).get("endpoint_name") or ENRON_ENDPOINT_NAME
    )
    gate_payload = run_gate(
        manifest,
        split=gate_split,
        cases=gate_cases,
        category=gate_category,
    )

    passed = smoke_payload["passed"] and gate_payload["passed"]
    rollback_payload = None
    if not passed and previous_version and previous_version != candidate_version and not no_rollback:
        print(
            f"\nPromotion gate failed. Rolling endpoint back to prior version {previous_version}."
        )
        rollback_payload = step_deploy(
            previous_version,
            manifest=manifest,
            tags={"promotion_mode": "rollback"},
        )

    payload = {
        "version": "1.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "passed": passed,
        "candidate_version": candidate_version,
        "previous_live_version": previous_version,
        "manifest_path": manifest.get("manifest_path"),
        "log_payload": log_payload,
        "deploy_payload": deploy_payload,
        "smoke_payload": smoke_payload,
        "gate_payload": gate_payload,
        "rollback_payload": rollback_payload,
    }
    _write_json(output_path, payload)
    return payload


def rollback_version(
    version: str,
    *,
    manifest_path: str | Path | None = None,
    artifact_dir: str | Path = ROOT_DIR / "data",
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path, artifact_dir)
    return step_deploy(version, manifest=manifest, tags={"promotion_mode": "manual_rollback"})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy and promote the Enron GraphRAG agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show current endpoint readiness and live version.")
    subparsers.add_parser("test", help="Run the live endpoint smoke test.")
    subparsers.add_parser("log", help="Log the current Enron agent artifact to MLflow.")

    deploy_parser = subparsers.add_parser("deploy", help="Deploy a specific or latest registered model version.")
    deploy_parser.add_argument("version", nargs="?", default=None, help="Registered model version to deploy.")

    manifest_parser = subparsers.add_parser("manifest", help="Build or refresh the promotion manifest from local artifacts.")
    manifest_parser.add_argument("--artifact-dir", default=str(ROOT_DIR / "data"))
    manifest_parser.add_argument("--output", default=str(ROOT_DIR / "data" / DEFAULT_PROMOTION_MANIFEST))

    gate_parser = subparsers.add_parser("gate", help="Run the narrow deployed quality/latency promotion gate.")
    gate_parser.add_argument("--manifest", default=None)
    gate_parser.add_argument("--artifact-dir", default=str(ROOT_DIR / "data"))
    gate_parser.add_argument("--cases", type=int, default=None)
    gate_parser.add_argument("--split", choices=["train", "test", "holdout"], default=None)
    gate_parser.add_argument("--category", default=None)

    promote_parser = subparsers.add_parser("promote", help="Deploy in place, gate the live endpoint, and rollback on failure.")
    promote_parser.add_argument("--manifest", default=None)
    promote_parser.add_argument("--artifact-dir", default=str(ROOT_DIR / "data"))
    promote_parser.add_argument("--version", default=None)
    promote_parser.add_argument("--skip-log", action="store_true")
    promote_parser.add_argument("--no-rollback", action="store_true")
    promote_parser.add_argument("--cases", type=int, default=None)
    promote_parser.add_argument("--split", choices=["train", "test", "holdout"], default=None)
    promote_parser.add_argument("--category", default=None)

    rollback_parser = subparsers.add_parser("rollback", help="Redeploy a prior live model version.")
    rollback_parser.add_argument("version", help="Model version to restore.")
    rollback_parser.add_argument("--manifest", default=None)
    rollback_parser.add_argument("--artifact-dir", default=str(ROOT_DIR / "data"))

    all_parser = subparsers.add_parser("all", help="Log, deploy, and smoke-test the latest Enron agent.")
    all_parser.add_argument("--manifest", default=None)
    all_parser.add_argument("--artifact-dir", default=str(ROOT_DIR / "data"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.command == "status":
        payload = step_status()
    elif args.command == "test":
        payload = step_test()
    elif args.command == "log":
        payload = step_log_model()
    elif args.command == "deploy":
        version = args.version or get_latest_registered_version()
        if not version:
            raise RuntimeError("No registered model versions found. Run 'deploy_enron.py log' first.")
        payload = step_deploy(version)
    elif args.command == "manifest":
        payload = build_promotion_manifest(
            artifact_dir=args.artifact_dir,
            output_path=args.output,
        )
    elif args.command == "gate":
        manifest = _load_manifest(args.manifest, args.artifact_dir)
        payload = run_gate(
            manifest,
            split=args.split,
            cases=args.cases,
            category=args.category,
        )
    elif args.command == "promote":
        payload = promote_in_place(
            manifest_path=args.manifest,
            artifact_dir=args.artifact_dir,
            version=args.version,
            skip_log=args.skip_log,
            no_rollback=args.no_rollback,
            gate_cases=args.cases,
            gate_split=args.split,
            gate_category=args.category,
        )
    elif args.command == "rollback":
        payload = rollback_version(
            args.version,
            manifest_path=args.manifest,
            artifact_dir=args.artifact_dir,
        )
    elif args.command == "all":
        manifest = _load_manifest(args.manifest, args.artifact_dir)
        log_payload = step_log_model(manifest)
        deploy_payload = step_deploy(
            log_payload["registered_model_version"],
            manifest=manifest,
        )
        smoke_payload = step_test(
            endpoint_name=manifest.get("deploy_target", {}).get("endpoint_name") or ENRON_ENDPOINT_NAME
        )
        payload = {
            "log_payload": log_payload,
            "deploy_payload": deploy_payload,
            "smoke_payload": smoke_payload,
        }
    else:  # pragma: no cover - argparse enforces commands
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
