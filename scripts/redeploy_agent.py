"""Re-log and redeploy the GraphRAG agent to Model Serving.

Equivalent to the log / deploy / gateway steps for the active Enron agent.
Run locally — only makes API calls (no Spark needed).

Step 0: (optional) Run local validation gate before deploying
Step 3: Log agent_serving.py as a new MLflow model version
Step 3.5: Smoke-test the logged model locally via mlflow.pyfunc.load_model
Step 4: Deploy to Model Serving and wait for READY
Step 5: Configure AI Gateway (usage tracking, inference tables, rate limits, guardrails)

Usage:
    python scripts/redeploy_agent.py              # validate + deploy
    python scripts/redeploy_agent.py --no-validate # skip validation
    python scripts/redeploy_agent.py 3             # just log model
"""
import argparse
import os
import subprocess
import sys
import time

import mlflow
from mlflow.models.resources import DatabricksServingEndpoint

CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_enron"
LLM_ENDPOINT = "databricks-llama-4-maverick"
SMALL_LLM_ENDPOINT = "databricks-meta-llama-3-1-8b-instruct"
REGISTERED_MODEL = f"{CATALOG}.{SCHEMA}.graphrag_enron_agent"
ENDPOINT_NAME = "graphrag-enron-agent"
INFERENCE_TABLE_PREFIX = "graphrag_gw"

AGENT_SERVING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "agent", "agent_serving.py"
)

VALIDATE_SCRIPT = os.path.join(os.path.dirname(__file__), "validate_local.py")


def step0_validate() -> bool:
    """Step 0: Run local validation gate. Returns True if all gates pass."""
    print("\n" + "=" * 60)
    print("  STEP 0: Local Validation Gate")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, VALIDATE_SCRIPT, "--backend", "local"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )
    if result.returncode != 0:
        print("\nERROR: Local validation failed. Fix issues before deploying.")
        return False
    print("\nLocal validation passed.")
    return True


def step3_log_model():
    """Step 3: Log agent_serving.py as a new MLflow model version."""
    mlflow.set_registry_uri("databricks-uc")

    resources = [
        DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
        DatabricksServingEndpoint(endpoint_name=SMALL_LLM_ENDPOINT),
    ]

    with mlflow.start_run(run_name="graphrag_enron_agent"):
        model_info = mlflow.pyfunc.log_model(
            name="agent",
            python_model=AGENT_SERVING_PATH,
            resources=resources,
            pip_requirements=[
                "mlflow>=3.0",
                "databricks-langchain",
                "langgraph>=0.3.4",
                "databricks-agents",
                "databricks-mcp",
                "databricks-connect",
            ],
            input_example={
                "input": [{"role": "user", "content": "Who communicated most frequently with Kenneth Lay?"}]
            },
            registered_model_name=REGISTERED_MODEL,
        )

    print(f"Model logged: {model_info.model_uri}")
    print(f"Registered version: {model_info.registered_model_version}")
    return model_info


def step3_5_smoke_test(model_info) -> bool:
    """Step 3.5: Load the logged model locally and run a smoke query.

    Catches packaging/dependency issues before the 15-40 min deploy.
    """
    print("\n" + "=" * 60)
    print("  STEP 3.5: Post-Log Smoke Test")
    print("=" * 60)

    try:
        loaded = mlflow.pyfunc.load_model(model_info.model_uri)
        result = loaded.predict(
            {"input": [{"role": "user", "content": "Who communicated most frequently with Kenneth Lay?"}]}
        )
        has_output = bool(result.get("output")) if isinstance(result, dict) else bool(result)
        if has_output:
            print("  Smoke test PASSED — model loads and produces output.")
            return True
        print("  WARNING: Model loaded but returned empty output.")
        return True
    except Exception as e:
        print(f"  Smoke test FAILED: {e}")
        print("  The logged model has packaging issues. Fix before deploying.")
        return False


def step4_deploy(model_info):
    """Step 4: Deploy the new version to the serving endpoint and wait."""
    from databricks import agents
    from databricks.sdk import WorkspaceClient

    try:
        deployment = agents.deploy(
            REGISTERED_MODEL,
            model_info.registered_model_version,
            endpoint_name=ENDPOINT_NAME,
            tags={"source": "graphrag_solacc"},
        )
        print(f"Deployment initiated: {deployment.endpoint_name}")
    except ValueError as e:
        if "currently updating" in str(e):
            print(f"Endpoint '{ENDPOINT_NAME}' is already updating — waiting...")
        else:
            raise

    w = WorkspaceClient()
    MAX_WAIT = 1800
    POLL = 30
    elapsed = 0

    while elapsed < MAX_WAIT:
        ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
        ready = ep.state.ready if ep.state else None
        config_update = ep.state.config_update if ep.state else None
        print(f"  [{elapsed}s] ready={ready}, config_update={config_update}")
        if str(ready) == "READY" and config_update is None:
            print(f"\nEndpoint '{ENDPOINT_NAME}' is READY!")
            return True
        time.sleep(POLL)
        elapsed += POLL

    print(f"\nWARNING: Endpoint did not reach READY within {MAX_WAIT}s.")
    return False


def step5_configure_ai_gateway():
    """Step 5: Configure AI Gateway (usage tracking, inference tables, rate limits, guardrails)."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import (
        AiGatewayGuardrailParameters,
        AiGatewayGuardrailPiiBehavior,
        AiGatewayGuardrailPiiBehaviorBehavior,
        AiGatewayGuardrails,
        AiGatewayInferenceTableConfig,
        AiGatewayRateLimit,
        AiGatewayRateLimitKey,
        AiGatewayRateLimitRenewalPeriod,
        AiGatewayUsageTrackingConfig,
    )

    w = WorkspaceClient()
    print(f"\nConfiguring AI Gateway on '{ENDPOINT_NAME}'...")

    configs = [
        (
            "full features",
            dict(
                usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=True),
                inference_table_config=AiGatewayInferenceTableConfig(
                    enabled=True,
                    catalog_name=CATALOG,
                    schema_name=SCHEMA,
                    table_name_prefix=INFERENCE_TABLE_PREFIX,
                ),
                rate_limits=[
                    AiGatewayRateLimit(
                        key=AiGatewayRateLimitKey.ENDPOINT,
                        renewal_period=AiGatewayRateLimitRenewalPeriod.MINUTE,
                        calls=60,
                    ),
                    AiGatewayRateLimit(
                        key=AiGatewayRateLimitKey.USER,
                        renewal_period=AiGatewayRateLimitRenewalPeriod.MINUTE,
                        calls=20,
                    ),
                ],
                guardrails=AiGatewayGuardrails(
                    input=AiGatewayGuardrailParameters(
                        safety=True,
                        pii=AiGatewayGuardrailPiiBehavior(
                            behavior=AiGatewayGuardrailPiiBehaviorBehavior.MASK,
                        ),
                    ),
                    output=AiGatewayGuardrailParameters(
                        safety=True,
                        pii=AiGatewayGuardrailPiiBehavior(
                            behavior=AiGatewayGuardrailPiiBehaviorBehavior.MASK,
                        ),
                    ),
                ),
            ),
        ),
        (
            "inference tables + usage tracking",
            dict(
                usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=True),
                inference_table_config=AiGatewayInferenceTableConfig(
                    enabled=True,
                    catalog_name=CATALOG,
                    schema_name=SCHEMA,
                    table_name_prefix=INFERENCE_TABLE_PREFIX,
                ),
            ),
        ),
        (
            "inference tables only",
            dict(
                inference_table_config=AiGatewayInferenceTableConfig(
                    enabled=True,
                    catalog_name=CATALOG,
                    schema_name=SCHEMA,
                    table_name_prefix=INFERENCE_TABLE_PREFIX,
                ),
            ),
        ),
    ]

    for label, cfg in configs:
        try:
            print(f"  Trying: {label}...")
            w.serving_endpoints.put_ai_gateway(name=ENDPOINT_NAME, **cfg)
            print(f"  AI Gateway configured ({label}).")
            return True
        except Exception as e:
            print(f"  Failed: {e}")

    print("WARNING: Could not configure AI Gateway on this endpoint type.")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redeploy GraphRAG agent to Model Serving")
    parser.add_argument("step", nargs="?", default="all",
                        choices=["all", "3", "log", "3.5", "smoke", "4", "deploy", "5", "gateway"],
                        help="Which step(s) to run (default: all)")
    parser.add_argument("--validate", dest="validate", action="store_true", default=True,
                        help="Run local validation before deploying (default)")
    parser.add_argument("--no-validate", dest="validate", action="store_false",
                        help="Skip local validation")
    parser.add_argument("version", nargs="?", default=None,
                        help="Model version (for step 4 standalone)")
    args = parser.parse_args()

    step = args.step

    if step == "all" and args.validate:
        if not step0_validate():
            sys.exit(1)

    model_info = None
    if step in ("3", "log", "all"):
        model_info = step3_log_model()

    if step in ("3.5", "smoke", "all"):
        if model_info:
            if not step3_5_smoke_test(model_info):
                print("\nAborting deployment due to smoke test failure.")
                sys.exit(1)
        else:
            print("No model_info available for smoke test. Run step 3 first.")

    if step in ("4", "deploy", "all"):
        if model_info:
            step4_deploy(model_info)
        else:
            version = args.version
            if not version:
                print("Usage: redeploy_agent.py 4 <version>")
                sys.exit(1)

            class _Info:
                registered_model_version = version
            step4_deploy(_Info())

    if step in ("5", "gateway", "all"):
        step5_configure_ai_gateway()
