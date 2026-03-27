# Databricks notebook source
# MAGIC %md
# MAGIC # 09 — Build the Enron GraphRAG Agent
# MAGIC
# MAGIC Log and deploy a GraphRAG agent for the **Enron email corpus**.
# MAGIC The same `agent_serving.py` module powers both the Bible and Enron agents —
# MAGIC the `GRAPHRAG_CORPUS` environment variable selects the corpus at runtime.
# MAGIC
# MAGIC **Prerequisites:** Notebooks 06 (Data Prep) and 07 (Build KG) must have run
# MAGIC so that the `graphrag_enron` schema tables exist.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U mlflow>=3.0 databricks-langchain langgraph>=0.3.4 databricks-agents pydantic databricks-mcp --quiet
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
    config['enron_entities_table'],
    config['enron_relationships_table'],
    config['enron_emails_table'],
    config['enron_entity_analytics_table'],
    config['enron_entity_paths_table'],
    config['enron_entity_mentions_table'],
]

for t in enron_tables:
    try:
        count = spark.table(t).count()
        print(f"  {t}: {count:,} rows")
    except Exception as e:
        print(f"  {t}: MISSING — {e}")
        print("  Run notebooks 06 + 07 first!")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Log the Enron Agent to MLflow

# COMMAND ----------

# DBTITLE 1,Log Model
import mlflow
from mlflow.models.resources import DatabricksServingEndpoint, DatabricksTable, DatabricksSQLWarehouse

mlflow.set_registry_uri("databricks-uc")

ENRON_TABLE_NAMES = [
    "entities", "relationships", "emails",
    "entity_analytics", "entity_paths", "entity_mentions",
]

_wh_id = "399215661843ad19"
try:
    _wh_id = spark.conf.get("spark.databricks.warehouse.id")
except Exception:
    pass

resources = [
    DatabricksServingEndpoint(endpoint_name=config['llm_endpoint']),
    DatabricksServingEndpoint(endpoint_name=config['small_llm_endpoint']),
    *[
        DatabricksTable(table_name=f"{config['catalog']}.{config['enron_schema']}.{t}")
        for t in ENRON_TABLE_NAMES
    ],
    DatabricksSQLWarehouse(warehouse_id=_wh_id),
]

with mlflow.start_run(run_name="graphrag_enron_agent"):
    model_info = mlflow.pyfunc.log_model(
        name="agent",
        python_model="../src/agent/agent_serving.py",
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
        registered_model_name=f"{config['catalog']}.{config['enron_schema']}.graphrag_enron_agent",
    )

print(f"Model logged: {model_info.model_uri}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deploy to Model Serving
# MAGIC
# MAGIC Deploy the Enron agent to its own endpoint with `GRAPHRAG_CORPUS=enron`.

# COMMAND ----------

# DBTITLE 1,Deploy Agent and Wait for Endpoint
from databricks import agents
from databricks.sdk import WorkspaceClient
import time

ENDPOINT_NAME = "graphrag-enron-agent"

try:
    deployment = agents.deploy(
        f"{config['catalog']}.{config['enron_schema']}.graphrag_enron_agent",
        model_info.registered_model_version,
        endpoint_name=ENDPOINT_NAME,
        environment_vars={
            "GRAPHRAG_CORPUS": "enron",
            "GRAPHRAG_SCHEMA": "graphrag_enron",
        },
        tags={"source": "graphrag_solacc", "corpus": "enron"},
    )
    print(f"Deployment initiated: {deployment.endpoint_name}")
except ValueError as e:
    if "currently updating" in str(e):
        print(f"Endpoint '{ENDPOINT_NAME}' is already updating — waiting for it to finish...")
    else:
        raise

w = WorkspaceClient()
MAX_WAIT_SECONDS = 1800
POLL_INTERVAL = 30
elapsed = 0

while elapsed < MAX_WAIT_SECONDS:
    ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
    ready = ep.state.ready if ep.state else None
    config_update = ep.state.config_update if ep.state else None
    print(f"  [{elapsed}s] ready={ready}, config_update={config_update}")
    if str(ready) == "READY" and config_update is None:
        print(f"\nEndpoint '{ENDPOINT_NAME}' is READY!")
        break
    time.sleep(POLL_INTERVAL)
    elapsed += POLL_INTERVAL
else:
    print(f"\nWARNING: Endpoint did not reach READY state within {MAX_WAIT_SECONDS}s. Check the Serving UI.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Test the Deployed Endpoint

# COMMAND ----------

# DBTITLE 1,Test: Communication Pattern Query
w = WorkspaceClient()

test_questions = [
    "Who communicated most frequently with Kenneth Lay?",
    "What projects did Jeff Skilling manage between 2000-2001?",
    "How did information flow about the Broadband division?",
]

for q in test_questions:
    print(f"\nQ: {q}")
    print("-" * 60)
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

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Enron agent is built and deployed. The **Corporate Demo** page in the web app
# MAGIC can now query `graphrag-enron-agent` with `USE_MOCK_BACKEND=false`.
