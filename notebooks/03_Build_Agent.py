# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Build the GraphRAG Agent
# MAGIC
# MAGIC Define graph traversal tools, build a LangGraph agent, test it, and log it to MLflow for deployment.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U mlflow>=3.0 databricks-langchain langgraph>=0.3.4 databricks-agents pydantic databricks-mcp --quiet
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

# DBTITLE 1,Test: Ranking Question (Cross-Testament)
test_request = ResponsesAgentRequest(
    input=[{"role": "user", "content": "Which person in the New Testament has the most relationships with persons from the Old Testament?"}]
)
result = AGENT.predict(test_request)
for item in result.output:
    if hasattr(item, 'text'):
        print(item.text)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Seed the Agent Prompts Table
# MAGIC
# MAGIC Store the system prompt in a Delta table so it can be updated without redeploying the serving endpoint.

# COMMAND ----------

# DBTITLE 1,Create and Seed agent_prompts Table
from datetime import datetime

AGENT_PROMPTS_TABLE = config['agent_prompts_table']

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {AGENT_PROMPTS_TABLE} (
        agent_id STRING,
        prompt_text STRING,
        updated_at TIMESTAMP
    ) USING DELTA
""")

spark.sql(f"""
    MERGE INTO {AGENT_PROMPTS_TABLE} AS target
    USING (SELECT 'bible-agent' AS agent_id) AS source
    ON target.agent_id = source.agent_id
    WHEN NOT MATCHED THEN
        INSERT (agent_id, prompt_text, updated_at)
        VALUES ('bible-agent', '{SYSTEM_PROMPT.replace("'", "''")}', current_timestamp())
""")

print(f"Agent prompts table ready: {AGENT_PROMPTS_TABLE}")
display(spark.table(AGENT_PROMPTS_TABLE))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Log the Agent to MLflow
# MAGIC
# MAGIC Register the agent in Unity Catalog so it can be deployed to Model Serving.

# COMMAND ----------

# DBTITLE 1,Log Model
import mlflow
from mlflow.models.resources import DatabricksServingEndpoint, DatabricksTable, DatabricksSQLWarehouse

mlflow.set_registry_uri("databricks-uc")

TABLE_NAMES = ["entities", "relationships", "verses", "agent_prompts",
               "entity_analytics", "entity_paths"]

resources = [
    DatabricksServingEndpoint(endpoint_name=config['llm_endpoint']),
    DatabricksServingEndpoint(endpoint_name=config['small_llm_endpoint']),
    *[DatabricksTable(table_name=f"{config['catalog']}.{config['schema']}.{t}") for t in TABLE_NAMES],
    DatabricksSQLWarehouse(warehouse_id="399215661843ad19"),
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
            "databricks-mcp",
            "databricks-sdk",
            "databricks-connect",
        ],
        input_example={
            "input": [{"role": "user", "content": "Who is Abraham?"}]
        },
        registered_model_name=f"{config['catalog']}.{config['schema']}.graphrag_agent",
    )

print(f"Model logged: {model_info.model_uri}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Deploy to Model Serving
# MAGIC
# MAGIC Deploy the agent to a Model Serving endpoint and wait for it to be online (~15 min).

# COMMAND ----------

# DBTITLE 1,Deploy Agent and Wait for Endpoint
from databricks import agents
from databricks.sdk import WorkspaceClient
import time

ENDPOINT_NAME = "graphrag-bible-agent"

try:
    deployment = agents.deploy(
        f"{config['catalog']}.{config['schema']}.graphrag_agent",
        model_info.registered_model_version,
        endpoint_name=ENDPOINT_NAME,
        tags={"source": "graphrag_solacc"},
    )
    print(f"Deployment initiated: {deployment.endpoint_name}")
except ValueError as e:
    if "currently updating" in str(e):
        print(f"Endpoint '{ENDPOINT_NAME}' is already updating — waiting for it to finish...")
    else:
        raise

w = WorkspaceClient()
MAX_WAIT_SECONDS = 1800  # 30 minutes
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
# MAGIC ## Step 5: Configure AI Gateway
# MAGIC
# MAGIC Enable AI Gateway features on the serving endpoint for governance and observability:
# MAGIC - **Usage tracking** — token counts per request in `system.serving.endpoint_usage`
# MAGIC - **Inference tables** — full request/response payloads logged to Delta for audit
# MAGIC - **Rate limits** — 60 QPM endpoint-level, 20 QPM per-user
# MAGIC - **AI Guardrails** — safety filters + PII masking on input and output
# MAGIC
# MAGIC > **Note:** Agent endpoints currently support inference tables and usage tracking.
# MAGIC > Rate limits and guardrails require external-model or provisioned-throughput endpoints.

# COMMAND ----------

# DBTITLE 1,Configure AI Gateway
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
INFERENCE_TABLE_PREFIX = "graphrag_gw"

ai_gateway_configs = [
    (
        "full features",
        dict(
            usage_tracking_config=AiGatewayUsageTrackingConfig(enabled=True),
            inference_table_config=AiGatewayInferenceTableConfig(
                enabled=True,
                catalog_name=config['catalog'],
                schema_name=config['schema'],
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
                catalog_name=config['catalog'],
                schema_name=config['schema'],
                table_name_prefix=INFERENCE_TABLE_PREFIX,
            ),
        ),
    ),
    (
        "inference tables only",
        dict(
            inference_table_config=AiGatewayInferenceTableConfig(
                enabled=True,
                catalog_name=config['catalog'],
                schema_name=config['schema'],
                table_name_prefix=INFERENCE_TABLE_PREFIX,
            ),
        ),
    ),
]

for label, gw_config in ai_gateway_configs:
    try:
        print(f"Trying AI Gateway config: {label}...")
        result = w.serving_endpoints.put_ai_gateway(name=ENDPOINT_NAME, **gw_config)
        print(f"AI Gateway configured ({label}).")
        print(f"  Inference tables: {config['catalog']}.{config['schema']}.{INFERENCE_TABLE_PREFIX}_request_response")
        break
    except Exception as e:
        print(f"  Failed: {e}")
else:
    print("WARNING: Could not configure AI Gateway on this endpoint type.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Verify AI Gateway Configuration

# COMMAND ----------

# DBTITLE 1,Show Current AI Gateway Config
import json

ep = w.serving_endpoints.get(name=ENDPOINT_NAME)
if ep.ai_gateway:
    print(json.dumps(ep.ai_gateway.as_dict(), indent=2, default=str))
else:
    print("No AI Gateway configured.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Query Usage Tracking (after sending some requests)

# COMMAND ----------

# DBTITLE 1,Usage Tracking — Recent Requests
display(spark.sql(f"""
    SELECT
        eu.request_time,
        eu.status_code,
        eu.input_token_count,
        eu.output_token_count,
        eu.requester
    FROM system.serving.endpoint_usage AS eu
    JOIN system.serving.served_entities AS se
        ON eu.served_entity_id = se.served_entity_id
    WHERE se.endpoint_name = '{ENDPOINT_NAME}'
    ORDER BY eu.request_time DESC
    LIMIT 20
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Agent is built and logged. Proceed to **04_Query_Demo** for interactive querying.
