# Databricks notebook source
# MAGIC %md
# MAGIC # 09 — Build the Enron GraphRAG Agent (Staged `src`)
# MAGIC
# MAGIC Re-log and deploy the Enron agent using a locally staged copy of the repo
# MAGIC `src` tree so MLflow packages `src.runtime` and related modules as plain files.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U mlflow>=3.0 databricks-langchain langgraph>=0.3.4 databricks-agents pydantic databricks-mcp "psycopg[binary,pool]>=3.0" --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Verify Enron Tables Exist

# COMMAND ----------

# DBTITLE 1,Check Enron KG Tables
enron_tables = [
    config["enron_entities_table"],
    config["enron_relationships_table"],
    config["enron_emails_table"],
    config["enron_entity_analytics_table"],
    config["enron_entity_paths_table"],
    config["enron_entity_mentions_table"],
]

for table_name in enron_tables:
    try:
        count = spark.table(table_name).count()
        print(f"  {table_name}: {count:,} rows")
    except Exception as exc:
        print(f"  {table_name}: MISSING — {exc}")
        print("  Run notebooks 06 + 07 first!")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Stage `src`, log the model, and verify packaged artifacts

# COMMAND ----------

# DBTITLE 1,Log Model With Staged Code Paths
import os
import shutil
import sys
import tempfile
from pathlib import Path

import mlflow
from mlflow.models.resources import (
    DatabricksGenieSpace,
    DatabricksServingEndpoint,
    DatabricksSQLWarehouse,
    DatabricksTable,
)
from mlflow.tracking import MlflowClient

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from src.agent.enron_promotion import (
    ENRON_INPUT_EXAMPLE,
    ENRON_PIP_REQUIREMENTS,
    ENRON_TABLE_NAMES,
    GENIE_SPACE_IDS,
    assert_enron_lakebase_ready,
    build_enron_serving_environment,
    enron_model_logging_env,
)

mlflow.set_registry_uri("databricks-uc")

REPO_ROOT = Path(os.path.abspath(os.path.join(os.getcwd(), ".."))).resolve()
MODEL_NAME = f"{config['catalog']}.{config['enron_schema']}.graphrag_enron_agent"


def stage_src_tree(repo_root: Path) -> Path:
    """Copy only serving-required source files to a plain local directory."""

    staging_root = Path(tempfile.mkdtemp(prefix="graphrag_mlflow_code_")).resolve()
    staged_src = staging_root / "src"
    copy_plan = [
        ("agent/agent_serving.py", "agent/agent_serving.py"),
        ("agent/enron_promotion.py", "agent/enron_promotion.py"),
        ("agent/pattern_registry.py", "agent/pattern_registry.py"),
        (
            "evaluation/baselines/genie_iteration0_baseline.json",
            "evaluation/baselines/genie_iteration0_baseline.json",
        ),
        ("runtime/__init__.py", "runtime/__init__.py"),
        ("runtime/analytics_sql.py", "runtime/analytics_sql.py"),
        ("runtime/config.py", "runtime/config.py"),
        ("runtime/contracts.py", "runtime/contracts.py"),
        ("runtime/modules.py", "runtime/modules.py"),
        ("runtime/orchestrator.py", "runtime/orchestrator.py"),
        ("runtime/responses.py", "runtime/responses.py"),
        ("runtime/router_assets.py", "runtime/router_assets.py"),
        (
            "runtime/assets/enron_router_cases_train.json",
            "runtime/assets/enron_router_cases_train.json",
        ),
    ]

    for source_rel, target_rel in copy_plan:
        source_path = (repo_root / "src" / source_rel).resolve()
        target_path = (staged_src / target_rel).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    required_paths = [
        staged_src / "agent" / "agent_serving.py",
        staged_src / "agent" / "enron_promotion.py",
        staged_src / "agent" / "pattern_registry.py",
        staged_src / "evaluation" / "baselines" / "genie_iteration0_baseline.json",
        staged_src / "runtime" / "__init__.py",
        staged_src / "runtime" / "analytics_sql.py",
        staged_src / "runtime" / "router_assets.py",
        staged_src / "runtime" / "assets" / "enron_router_cases_train.json",
    ]
    missing = [str(path.relative_to(staging_root)) for path in required_paths if not path.exists()]
    if missing:
        raise RuntimeError(f"Staged src bundle is missing required files: {missing}")

    print("Staged MLflow code root:", staging_root)
    print("Staged python model:", staged_src / "agent" / "agent_serving.py")
    return staging_root


def build_log_model_kwargs(
    *,
    staged_root: Path,
    warehouse_id: str,
    llm_endpoint: str,
    small_llm_endpoint: str,
) -> dict:
    resources = [
        DatabricksServingEndpoint(endpoint_name=llm_endpoint),
        DatabricksServingEndpoint(endpoint_name=small_llm_endpoint),
        *[
            DatabricksTable(table_name=f"{config['catalog']}.{config['enron_schema']}.{table_name}")
            for table_name in ENRON_TABLE_NAMES
        ],
        DatabricksSQLWarehouse(warehouse_id=warehouse_id),
        *[
            DatabricksGenieSpace(genie_space_id=space_id)
            for space_id in GENIE_SPACE_IDS.values()
        ],
    ]
    return {
        "name": "agent",
        "python_model": str((staged_root / "src" / "agent" / "agent_serving.py").resolve()),
        "code_paths": [str((staged_root / "src").resolve())],
        "resources": resources,
        "pip_requirements": list(ENRON_PIP_REQUIREMENTS),
        "input_example": dict(ENRON_INPUT_EXAMPLE),
        "registered_model_name": MODEL_NAME,
    }


_warehouse_id = "399215661843ad19"
try:
    _warehouse_id = spark.conf.get("spark.databricks.warehouse.id")
except Exception:
    pass

serving_env = build_enron_serving_environment(
    schema=config["enron_schema"],
    llm_endpoint=config["llm_endpoint"],
    small_llm_endpoint=config["small_llm_endpoint"],
    synthesis_endpoint=config["llm_endpoint"],
    react_endpoint=config["llm_endpoint"],
)
lakebase_readiness = assert_enron_lakebase_ready(
    endpoint_name=serving_env.get("LAKEBASE_ENDPOINT"),
)
print(
    "Lakebase ready:",
    lakebase_readiness["endpoint_name"],
    lakebase_readiness["host"],
)
print(
    "Runtime transports:",
    {
        "router": serving_env.get("GRAPHRAG_ROUTER_TRANSPORT"),
        "planner": serving_env.get("GRAPHRAG_PLANNER_TRANSPORT"),
        "graph": serving_env.get("GRAPHRAG_GRAPH_TRANSPORT"),
        "evidence": serving_env.get("GRAPHRAG_EVIDENCE_TRANSPORT"),
        "analytics": serving_env.get("GRAPHRAG_ANALYTICS_TRANSPORT"),
    },
)

staged_root = stage_src_tree(REPO_ROOT)
log_model_kwargs = build_log_model_kwargs(
    staged_root=staged_root,
    warehouse_id=_warehouse_id,
    llm_endpoint=config["llm_endpoint"],
    small_llm_endpoint=config["small_llm_endpoint"],
)

with enron_model_logging_env(
    schema=config["enron_schema"],
    llm_endpoint=config["llm_endpoint"],
    small_llm_endpoint=config["small_llm_endpoint"],
    synthesis_endpoint=config["llm_endpoint"],
    react_endpoint=config["llm_endpoint"],
    lakebase_endpoint=serving_env.get("LAKEBASE_ENDPOINT"),
):
    with mlflow.start_run(run_name="graphrag_enron_agent_staged_src") as run:
        model_info = mlflow.pyfunc.log_model(**log_model_kwargs)
        artifact_client = MlflowClient()
        code_entries = [
            item.path for item in artifact_client.list_artifacts(run.info.run_id, "agent/code")
        ]
        src_entries = [
            item.path for item in artifact_client.list_artifacts(run.info.run_id, "agent/code/src")
        ]
        runtime_entries = [
            item.path
            for item in artifact_client.list_artifacts(run.info.run_id, "agent/code/src/runtime")
        ]
        print("Code artifact listing:", code_entries)
        print("src artifact listing:", src_entries)
        print("Runtime artifact listing:", runtime_entries)
        if "agent/code/src" not in code_entries:
            raise RuntimeError(
                "MLflow logged the model without a top-level 'src' code artifact."
            )
        if "agent/code/src/runtime" not in src_entries:
            raise RuntimeError(
                "MLflow logged the model without 'src/runtime' in the packaged artifacts."
            )

print(f"Model logged: {model_info.model_uri}")
print(f"Registered version: {model_info.registered_model_version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deploy to Model Serving and wait for READY

# COMMAND ----------

# DBTITLE 1,Deploy Agent and Wait for Endpoint
import time

from databricks import agents
from databricks.sdk import WorkspaceClient

ENDPOINT_NAME = "graphrag-enron-agent"

try:
    lakebase_readiness = assert_enron_lakebase_ready(
        endpoint_name=serving_env.get("LAKEBASE_ENDPOINT"),
    )
    print(
        "Lakebase ready:",
        lakebase_readiness["endpoint_name"],
        lakebase_readiness["host"],
    )
    deployment = agents.deploy(
        MODEL_NAME,
        model_info.registered_model_version,
        endpoint_name=ENDPOINT_NAME,
        environment_vars=serving_env,
        tags={"source": "graphrag_solacc", "corpus": "enron", "packaging": "staged_src"},
    )
    print(f"Deployment initiated: {deployment.endpoint_name}")
except ValueError as exc:
    if "currently updating" in str(exc):
        print(f"Endpoint '{ENDPOINT_NAME}' is already updating — waiting for it to finish...")
    else:
        raise

workspace_client = WorkspaceClient()
MAX_WAIT_SECONDS = 1800
POLL_INTERVAL = 30
elapsed = 0

while elapsed < MAX_WAIT_SECONDS:
    endpoint = workspace_client.serving_endpoints.get(name=ENDPOINT_NAME)
    ready = endpoint.state.ready if endpoint.state else None
    config_update = endpoint.state.config_update if endpoint.state else None
    print(f"  [{elapsed}s] ready={ready}, config_update={config_update}")
    if "READY" in str(ready) and str(config_update) in {
        "None",
        "NOT_UPDATING",
        "EndpointStateConfigUpdate.NOT_UPDATING",
    }:
        print(f"\nEndpoint '{ENDPOINT_NAME}' is READY!")
        break
    time.sleep(POLL_INTERVAL)
    elapsed += POLL_INTERVAL
else:
    print(
        f"\nWARNING: Endpoint did not reach READY state within {MAX_WAIT_SECONDS}s. Check the Serving UI."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Smoke test the deployed endpoint

# COMMAND ----------

# DBTITLE 1,Test: Communication Pattern Query
workspace_client = WorkspaceClient()

test_questions = [
    "Who communicated most frequently with Kenneth Lay?",
    "What projects did Jeff Skilling manage between 2000-2001?",
]

for question in test_questions:
    print(f"\nQ: {question}")
    print("-" * 60)
    response = workspace_client.api_client.do(
        "POST",
        f"/serving-endpoints/{ENDPOINT_NAME}/invocations",
        body={"input": [{"role": "user", "content": question}]},
    )
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    print(part["text"][:500])
    print()
