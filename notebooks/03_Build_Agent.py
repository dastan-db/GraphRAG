# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Build the GraphRAG Agent
# MAGIC
# MAGIC Define graph traversal tools, build a LangGraph agent, test it, and log it to MLflow for deployment.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U mlflow>=3.0 databricks-langchain langgraph>=0.3.4 databricks-agents pydantic --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration and Utilities
# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %run ../src/agent/tools

# COMMAND ----------

# MAGIC %run ../src/agent/agent

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Test the Agent
# MAGIC
# MAGIC Before logging, let's verify the agent works with some sample questions.

# COMMAND ----------

# DBTITLE 1,Test: Simple Entity Lookup
from mlflow.types.responses import ResponsesAgentRequest

test_request = ResponsesAgentRequest(
    input=[{"role": "user", "content": "Who is Moses?"}]
)
result = AGENT.predict(test_request)
for item in result.output:
    if hasattr(item, 'text'):
        print(item.text)

# COMMAND ----------

# DBTITLE 1,Test: Multi-hop Question
test_request = ResponsesAgentRequest(
    input=[{"role": "user", "content": "How is Ruth connected to Jesus?"}]
)
result = AGENT.predict(test_request)
for item in result.output:
    if hasattr(item, 'text'):
        print(item.text)

# COMMAND ----------

# DBTITLE 1,Test: Cross-book Question
test_request = ResponsesAgentRequest(
    input=[{"role": "user", "content": "Which people appear in both Genesis and the New Testament?"}]
)
result = AGENT.predict(test_request)
for item in result.output:
    if hasattr(item, 'text'):
        print(item.text)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Log the Agent to MLflow
# MAGIC
# MAGIC Register the agent in Unity Catalog so it can be deployed to Model Serving.

# COMMAND ----------

# DBTITLE 1,Log Model
import mlflow
from mlflow.models.resources import DatabricksServingEndpoint

mlflow.set_registry_uri("databricks-uc")

resources = [
    DatabricksServingEndpoint(endpoint_name=config['llm_endpoint']),
]

with mlflow.start_run(run_name="graphrag_bible_agent"):
    model_info = mlflow.pyfunc.log_model(
        name="agent",
        python_model="../src/agent/agent_serving.py",
        resources=resources,
        pip_requirements=[
            "mlflow>=3.0",
            "databricks-langchain",
            "langgraph>=0.3.4",
            "databricks-agents",
        ],
        input_example={
            "input": [{"role": "user", "content": "Who is Abraham?"}]
        },
        registered_model_name=f"{config['catalog']}.{config['schema']}.graphrag_agent",
    )

print(f"Model logged: {model_info.model_uri}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deploy to Model Serving
# MAGIC
# MAGIC Deploy the agent to a Model Serving endpoint and wait for it to be online (~15 min).

# COMMAND ----------

# DBTITLE 1,Deploy Agent and Wait for Endpoint
from databricks import agents
from databricks.sdk import WorkspaceClient
import time

ENDPOINT_NAME = "graphrag-bible-agent"

deployment = agents.deploy(
    f"{config['catalog']}.{config['schema']}.graphrag_agent",
    version=model_info.registered_model_version,
    endpoint_name=ENDPOINT_NAME,
    tags={"source": "graphrag_solacc"},
)
print(f"Deployment initiated: {deployment.endpoint_name}")

w = WorkspaceClient()
MAX_WAIT_SECONDS = 1800  # 30 minutes
POLL_INTERVAL = 30
elapsed = 0

while elapsed < MAX_WAIT_SECONDS:
    ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
    ready = ep.state.ready if ep.state else None
    config_update = ep.state.config_update if ep.state else None
    print(f"  [{elapsed}s] ready={ready}, config_update={config_update}")
    if str(ready) == "READY":
        print(f"\nEndpoint '{ENDPOINT_NAME}' is READY!")
        break
    time.sleep(POLL_INTERVAL)
    elapsed += POLL_INTERVAL
else:
    print(f"\nWARNING: Endpoint did not reach READY state within {MAX_WAIT_SECONDS}s. Check the Serving UI.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Agent is built and logged. Proceed to **04_Query_Demo** for interactive querying.
