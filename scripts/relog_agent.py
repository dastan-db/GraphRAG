"""Re-log the GraphRAG agent to MLflow and deploy to Model Serving."""
import mlflow
from mlflow.models.resources import DatabricksServingEndpoint, DatabricksTable, DatabricksSQLWarehouse

CATALOG = "serverless_8e8gyh_catalog"
SCHEMA = "graphrag_bible"
LLM_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"
SMALL_LLM_ENDPOINT = "databricks-meta-llama-3-1-8b-instruct"
REGISTERED_MODEL = f"{CATALOG}.{SCHEMA}.graphrag_agent"
ENDPOINT_NAME = "graphrag-bible-agent"

mlflow.set_registry_uri("databricks-uc")

TABLE_NAMES = ["entities", "relationships", "verses", "agent_prompts",
               "entity_analytics", "entity_paths"]

resources = [
    DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
    DatabricksServingEndpoint(endpoint_name=SMALL_LLM_ENDPOINT),
    *[DatabricksTable(table_name=f"{CATALOG}.{SCHEMA}.{t}") for t in TABLE_NAMES],
    DatabricksSQLWarehouse(warehouse_id="399215661843ad19"),
]

with mlflow.start_run(run_name="graphrag_agent_v27_table_resources"):
    model_info = mlflow.pyfunc.log_model(
        name="agent",
        python_model="src/agent/agent_serving.py",
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
            "input": [{"role": "user", "content": "Who is Abraham?"}]
        },
        registered_model_name=REGISTERED_MODEL,
    )

print(f"Model logged: {model_info.model_uri}")
version = model_info.registered_model_version
print(f"Registered model version: {version}")

from databricks import agents

try:
    deployment = agents.deploy(
        REGISTERED_MODEL,
        version,
        endpoint_name=ENDPOINT_NAME,
        tags={"source": "graphrag_solacc"},
    )
    print(f"Deployment initiated: {deployment.endpoint_name}")
except ValueError as e:
    if "currently updating" in str(e):
        print(f"Endpoint '{ENDPOINT_NAME}' is already updating — new version {version} queued.")
    else:
        raise

print("Done. Monitor endpoint status in the Databricks UI.")
