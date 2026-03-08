"""Configure AI Gateway on the GraphRAG agent serving endpoint.

Enables: usage tracking, payload logging (inference tables), rate limits,
and AI guardrails via the Databricks Python SDK `put_ai_gateway` API.

NOTE: Agent endpoints (deployed via `databricks.agents.deploy()`) currently
only support inference tables. Rate limits, guardrails, and fallbacks are
fully supported on external-model, provisioned-throughput, and pay-per-token
endpoints. This script configures ALL features so the setup is ready if the
endpoint type changes or Databricks extends agent-endpoint support.

Usage:
    python scripts/configure_ai_gateway.py                 # apply full config
    python scripts/configure_ai_gateway.py --show          # show current config
    python scripts/configure_ai_gateway.py --disable       # disable all features
"""
import argparse
import json
import sys

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

# ---------------------------------------------------------------------------
# Configuration — edit these to match your environment
# ---------------------------------------------------------------------------
ENDPOINT_NAME = "graphrag-bible-agent"
CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_bible"
INFERENCE_TABLE_PREFIX = "graphrag_gw"


def build_ai_gateway_config() -> dict:
    """Build the full AI Gateway configuration dict for put_ai_gateway."""
    return dict(
        # 1. Usage tracking — populates system.serving.endpoint_usage
        usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=True),

        # 2. Payload logging — logs request/response pairs to inference tables
        inference_table_config=AiGatewayInferenceTableConfig(
            enabled=True,
            catalog_name=CATALOG,
            schema_name=SCHEMA,
            table_name_prefix=INFERENCE_TABLE_PREFIX,
        ),

        # 3. Rate limits — protect against runaway costs
        #    Endpoint-level: 60 QPM global cap
        #    Per-user default: 20 QPM
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

        # 4. AI Guardrails — safety + PII masking on both input and output
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
    )


def build_disable_config() -> dict:
    """Build a config that disables all AI Gateway features."""
    return dict(
        usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=False),
        inference_table_config=AiGatewayInferenceTableConfig(enabled=False),
        rate_limits=[],
        guardrails=AiGatewayGuardrails(input=None, output=None),
    )


def show_current_config(w: WorkspaceClient) -> None:
    """Print the current AI Gateway configuration for the endpoint."""
    ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
    gw = ep.ai_gateway
    if gw is None:
        print(f"Endpoint '{ENDPOINT_NAME}' has no AI Gateway configured.")
        return

    print(f"AI Gateway configuration for '{ENDPOINT_NAME}':")
    print(json.dumps(gw.as_dict(), indent=2, default=str))


def apply_config(w: WorkspaceClient, config: dict, label: str) -> None:
    """Apply an AI Gateway configuration, with progressive fallback.

    Tries: full config → inference tables + usage tracking → inference tables only.
    Agent endpoints may only support inference tables.
    """
    print(f"\n{'='*60}")
    print(f"Applying AI Gateway config ({label}) to '{ENDPOINT_NAME}'...")
    print(f"{'='*60}\n")

    try:
        result = w.serving_endpoints.put_ai_gateway(
            name=ENDPOINT_NAME,
            **config,
        )
        print("AI Gateway updated successfully.\n")
        print("Applied configuration:")
        print(json.dumps(result.as_dict(), indent=2, default=str))
    except Exception as e:
        error_msg = str(e)
        print(f"Full config failed: {error_msg}\n")
        _apply_with_fallback(w)


def _apply_with_fallback(w: WorkspaceClient) -> None:
    """Progressive fallback: try inference tables + usage tracking, then just inference tables."""
    # Attempt: inference tables + usage tracking
    try:
        print("Attempting: inference tables + usage tracking...")
        result = w.serving_endpoints.put_ai_gateway(
            name=ENDPOINT_NAME,
            usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=True),
            inference_table_config=AiGatewayInferenceTableConfig(
                enabled=True,
                catalog_name=CATALOG,
                schema_name=SCHEMA,
                table_name_prefix=INFERENCE_TABLE_PREFIX,
            ),
        )
        print("Inference tables + usage tracking configured.\n")
        print("Applied configuration:")
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return
    except Exception as e2:
        print(f"  Failed: {e2}\n")

    # Attempt: inference tables only
    try:
        print("Attempting: inference tables only...")
        result = w.serving_endpoints.put_ai_gateway(
            name=ENDPOINT_NAME,
            inference_table_config=AiGatewayInferenceTableConfig(
                enabled=True,
                catalog_name=CATALOG,
                schema_name=SCHEMA,
                table_name_prefix=INFERENCE_TABLE_PREFIX,
            ),
        )
        print("Inference tables configured.\n")
        print("Applied configuration:")
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return
    except Exception as e3:
        print(f"  Failed: {e3}\n")
        print(
            "ERROR: Could not configure any AI Gateway features.\n"
            "This endpoint type may not support AI Gateway at all.\n"
            "Check: https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Configure AI Gateway on the GraphRAG serving endpoint"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="Show current config")
    group.add_argument("--disable", action="store_true", help="Disable all features")
    args = parser.parse_args()

    w = WorkspaceClient()

    ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
    ready = ep.state.ready if ep.state else None
    is_ready = ready and "READY" in str(ready)
    print(f"Endpoint '{ENDPOINT_NAME}' state: ready={ready}")
    if not is_ready:
        print("WARNING: Endpoint is not in READY state. Config may fail.")

    if args.show:
        show_current_config(w)
    elif args.disable:
        apply_config(w, build_disable_config(), "disable all")
    else:
        apply_config(w, build_ai_gateway_config(), "full AI Gateway")

        print(f"\nTo verify, run: python scripts/configure_ai_gateway.py --show")
        print(f"To query logs:  python scripts/query_ai_gateway_tables.py")


if __name__ == "__main__":
    main()
