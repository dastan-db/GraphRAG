"""Deploy the Enron GraphRAG agent to Databricks Model Serving.

Equivalent to notebook 09_Enron_Build_Agent.py but runs locally.
Only makes API calls — no Spark required.

Usage:
    python scripts/deploy_enron.py                  # full: log + deploy + wait + test
    python scripts/deploy_enron.py log              # just log model
    python scripts/deploy_enron.py deploy           # deploy latest version
    python scripts/deploy_enron.py deploy 42        # deploy specific version
    python scripts/deploy_enron.py test             # test existing endpoint
    python scripts/deploy_enron.py status           # check endpoint status
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mlflow
from mlflow.models.resources import (
    DatabricksServingEndpoint,
    DatabricksGenieSpace,
    DatabricksSQLWarehouse,
    DatabricksTable,
)

CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_enron"
LLM_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"
SMALL_LLM_ENDPOINT = "databricks-meta-llama-3-1-8b-instruct"
REGISTERED_MODEL = f"{CATALOG}.{SCHEMA}.graphrag_enron_agent"
ENDPOINT_NAME = "graphrag-enron-agent"
WAREHOUSE_ID = "399215661843ad19"

GENIE_COMM_SPACE_ID = "01f12b3ef5121d88be4f23d2dfe2d770"
GENIE_ORG_SPACE_ID = "01f12b3ef5521f078ba8438cc94e108b"
GENIE_INVEST_SPACE_ID = "01f12b3ef56e198e828cd8b59f646430"

ENRON_TABLE_NAMES = [
    "entities", "relationships", "emails",
    "entity_analytics", "entity_paths", "entity_mentions",
    "communication_dyads", "participants", "entity_aliases",
    "person_activity", "investigation_timeline",
    "extraction_provenance", "pipeline_lineage", "topic_taxonomy",
    "corpus_coverage",
    "person_role_timeline", "person_identity", "email_classification",
    "data_quality_report", "threads", "org_hierarchy",
]

AGENT_SERVING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "agent", "agent_serving.py"
)
PATTERN_REGISTRY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "agent", "pattern_registry.py"
)


def step_log_model():
    """Log agent_serving.py as a new MLflow model version with all Enron resources."""
    mlflow.set_registry_uri("databricks-uc")

    resources = [
        DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
        DatabricksServingEndpoint(endpoint_name=SMALL_LLM_ENDPOINT),
        *[
            DatabricksTable(table_name=f"{CATALOG}.{SCHEMA}.{t}")
            for t in ENRON_TABLE_NAMES
        ],
        DatabricksSQLWarehouse(warehouse_id=WAREHOUSE_ID),
        DatabricksGenieSpace(genie_space_id=GENIE_COMM_SPACE_ID),
        DatabricksGenieSpace(genie_space_id=GENIE_ORG_SPACE_ID),
        DatabricksGenieSpace(genie_space_id=GENIE_INVEST_SPACE_ID),
    ]

    with mlflow.start_run(run_name="graphrag_enron_agent"):
        model_info = mlflow.pyfunc.log_model(
            name="agent",
            python_model=AGENT_SERVING_PATH,
            code_paths=[PATTERN_REGISTRY_PATH],
            resources=resources,
            pip_requirements=[
                "mlflow>=3.0",
                "databricks-langchain",
                "langgraph>=0.3.4",
                "databricks-agents",
                "databricks-mcp",
                "databricks-sdk",
            ],
            input_example={
                "input": [{"role": "user", "content": "Who communicated most frequently with Kenneth Lay?"}]
            },
            registered_model_name=REGISTERED_MODEL,
        )

    print(f"Model logged: {model_info.model_uri}")
    print(f"Registered version: {model_info.registered_model_version}")
    return model_info


def step_deploy(version):
    """Deploy a model version to Model Serving and wait for READY."""
    from databricks import agents
    from databricks.sdk import WorkspaceClient

    try:
        deployment = agents.deploy(
            REGISTERED_MODEL,
            version,
            endpoint_name=ENDPOINT_NAME,
            environment_vars={
                "GRAPHRAG_CORPUS": "enron",
                "GRAPHRAG_SCHEMA": "graphrag_enron",
                "GENIE_COMM_SPACE_ID": GENIE_COMM_SPACE_ID,
                "GENIE_ORG_SPACE_ID": GENIE_ORG_SPACE_ID,
                "GENIE_INVEST_SPACE_ID": GENIE_INVEST_SPACE_ID,
            },
            tags={"source": "graphrag_solacc", "corpus": "enron"},
        )
        print(f"Deployment initiated: {deployment.endpoint_name}")
    except ValueError as e:
        if "currently updating" in str(e):
            print(f"Endpoint '{ENDPOINT_NAME}' is already updating — waiting...")
        else:
            raise

    w = WorkspaceClient()
    MAX_WAIT = 2400
    POLL = 30
    elapsed = 0

    while elapsed < MAX_WAIT:
        ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
        ready = ep.state.ready if ep.state else None
        config_update = ep.state.config_update if ep.state else None
        print(f"  [{elapsed}s] ready={ready}, config_update={config_update}")
        if "READY" in str(ready) and (config_update is None or "NOT_UPDATING" in str(config_update)):
            print(f"\nEndpoint '{ENDPOINT_NAME}' is READY!")
            return True
        time.sleep(POLL)
        elapsed += POLL

    print(f"\nWARNING: Endpoint did not reach READY within {MAX_WAIT}s.")
    return False


def step_status():
    """Check endpoint status without deploying."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    try:
        ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
        ready = ep.state.ready if ep.state else None
        config_update = ep.state.config_update if ep.state else None
        print(f"Endpoint: {ENDPOINT_NAME}")
        print(f"  ready={ready}, config_update={config_update}")
        if ep.config and ep.config.served_entities:
            for se in ep.config.served_entities:
                print(f"  entity: {se.entity_name} v{se.entity_version}")
        return "READY" in str(ready) and (config_update is None or "NOT_UPDATING" in str(config_update))
    except Exception as e:
        print(f"Endpoint not found or error: {e}")
        return False


def step_test():
    """Test the deployed endpoint with sample queries."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()

    test_questions = [
        "Who communicated most frequently with Kenneth Lay?",
        "Who reported to Jeff Skilling?",
        "How are Kenneth Lay and Tim Belden connected?",
    ]

    for q in test_questions:
        print(f"\nQ: {q}")
        print("-" * 60)
        try:
            resp = w.api_client.do(
                "POST",
                f"/serving-endpoints/{ENDPOINT_NAME}/invocations",
                body={"input": [{"role": "user", "content": q}]},
            )
            for item in resp.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            print(part["text"][:500])
            print()
        except Exception as e:
            print(f"ERROR: {e}\n")


def get_latest_version():
    """Get the latest registered model version."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    try:
        ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
        if ep.config and ep.config.served_entities:
            return ep.config.served_entities[0].entity_version
    except Exception:
        pass
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy Enron GraphRAG agent to Model Serving")
    parser.add_argument("step", nargs="?", default="all",
                        choices=["all", "log", "deploy", "test", "status"],
                        help="Which step to run (default: all = log + deploy + wait + test)")
    parser.add_argument("version", nargs="?", default=None,
                        help="Model version (for 'deploy' step standalone)")
    args = parser.parse_args()

    if args.step == "status":
        step_status()
    elif args.step == "test":
        step_test()
    elif args.step == "log":
        step_log_model()
    elif args.step == "deploy":
        v = args.version
        if not v:
            v = get_latest_version()
            if not v:
                print("No version specified and could not detect latest. Usage: deploy_enron.py deploy <version>")
                sys.exit(1)
            print(f"Using latest version: {v}")
        step_deploy(v)
    elif args.step == "all":
        model_info = step_log_model()
        if step_deploy(model_info.registered_model_version):
            step_test()
        else:
            print("\nEndpoint not ready. Run 'deploy_enron.py test' later to verify.")
