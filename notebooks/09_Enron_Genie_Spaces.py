# Databricks notebook source
# MAGIC %md
# MAGIC # 09 — Enron Genie Spaces (C1)
# MAGIC
# MAGIC Documents three **Genie Space** configurations for the Enron GraphRAG corpus and creates them via the **Databricks SDK** (`WorkspaceClient().genie`).
# MAGIC
# MAGIC **Prerequisites:** Notebooks 06–07 (and related Enron silver tables) so Unity Catalog tables exist.
# MAGIC
# MAGIC **After creation:** Set environment variables on the agent endpoint (or job cluster):
# MAGIC - `GENIE_COMM_SPACE_ID` — Communication Analytics
# MAGIC - `GENIE_ORG_SPACE_ID` — Organizational Intelligence
# MAGIC - `GENIE_INVEST_SPACE_ID` — Email Investigation

# COMMAND ----------

# MAGIC %run ../src/config

# COMMAND ----------

# MAGIC %md
# MAGIC ## Space definitions (documentation)
# MAGIC
# MAGIC | Space | Key tables | Purpose |
# MAGIC |-------|------------|---------|
# MAGIC | **Communication Analytics** | `communication_dyads`, `person_activity`, `email_classification` | Volumes, top senders/pairs, internal vs external, automated patterns |
# MAGIC | **Organizational Intelligence** | `org_hierarchy`, `person_identity`, `person_role_timeline`, `entities` | Reporting lines, roles, departments; prefer `entity_type = 'Person'` on `entities` when listing people |
# MAGIC | **Email Investigation** | `emails`, `entity_mentions`, `threads`, `extraction_provenance` | Content search, entity mentions, truncation / extraction quality |

# COMMAND ----------

# DBTITLE 1,Dependencies
# MAGIC %pip install -U databricks-sdk --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Reload config (kernel restarted)
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Build serialized_space payloads (API v2)
import json
import uuid

from databricks.sdk import WorkspaceClient


def _nid() -> str:
    """32-char hex id similar to Genie API examples."""
    return uuid.uuid4().hex


def build_serialized_space_v2(
    *,
    table_identifiers: list[str],
    instructions: str,
    sample_questions: list[str],
) -> str:
    """Return JSON string for Genie `create_space(serialized_space=...)`."""
    payload = {
        "version": 2,
        "config": {
            "sample_questions": [
                {"id": _nid(), "question": [q]}
                for q in sample_questions
            ],
        },
        "data_sources": {
            "tables": [{"identifier": t} for t in table_identifiers],
        },
        "instructions": {
            "text_instructions": [
                {"id": _nid(), "content": [instructions]},
            ],
        },
    }
    return json.dumps(payload)


catalog = config["catalog"]
schema = config["enron_schema"]
base = f"{catalog}.{schema}"

SPACE_SPECS = [
    {
        "title": "Enron — Communication Analytics",
        "description": "Email communication patterns: volumes, pairs, internal/external mix.",
        "env_hint": "GENIE_COMM_SPACE_ID",
        "tables": [
            f"{base}.communication_dyads",
            f"{base}.person_activity",
            f"{base}.email_classification",
        ],
        "instructions": (
            "This space analyzes email communication patterns at Enron. Use it to find who "
            "communicated most, communication volumes, internal vs external email ratios, and "
            "automated email patterns."
        ),
        "sample_questions": [
            "Who sent the most emails?",
            "What percentage of emails were internal?",
            "Show me the top 10 communication pairs by volume",
        ],
    },
    {
        "title": "Enron — Organizational Intelligence",
        "description": "Org structure, identities, role history; entities table includes non-person types.",
        "env_hint": "GENIE_ORG_SPACE_ID",
        "tables": [
            f"{base}.org_hierarchy",
            f"{base}.person_identity",
            f"{base}.person_role_timeline",
            f"{base}.entities",
        ],
        "instructions": (
            "This space explores Enron's organizational structure. Use it to find reporting "
            "relationships, role histories, department structures, and person identities. "
            "When listing people from `entities`, filter with entity_type = 'Person' unless the "
            "question needs organizations."
        ),
        "sample_questions": [
            "Who did Jeff Skilling report to?",
            "List all C-suite executives and their tenures",
            "How many people are in each department?",
        ],
    },
    {
        "title": "Enron — Email Investigation",
        "description": "Investigative email analysis, mentions, threads, extraction provenance.",
        "env_hint": "GENIE_INVEST_SPACE_ID",
        "tables": [
            f"{base}.emails",
            f"{base}.entity_mentions",
            f"{base}.threads",
            f"{base}.extraction_provenance",
        ],
        "instructions": (
            "This space enables investigative email analysis at Enron. Use it to search emails "
            "by content, find mentions of specific entities, track extraction quality, and "
            "identify truncated analyses."
        ),
        "sample_questions": [
            "Find emails mentioning 'shred' or 'delete'",
            "Which threads had their text truncated during extraction?",
            "How many entities were extracted per thread on average?",
        ],
    },
]

# COMMAND ----------

# DBTITLE 1,Resolve SQL warehouse
warehouse_id = "399215661843ad19"
try:
    warehouse_id = spark.conf.get("spark.databricks.warehouse.id")
except Exception:
    pass

print(f"Using warehouse_id={warehouse_id}")

# COMMAND ----------

# DBTITLE 1,Create spaces via WorkspaceClient.genie
w = WorkspaceClient()
created = []

for spec in SPACE_SPECS:
    serialized = build_serialized_space_v2(
        table_identifiers=spec["tables"],
        instructions=spec["instructions"],
        sample_questions=spec["sample_questions"],
    )
    space = w.genie.create_space(
        warehouse_id=warehouse_id,
        serialized_space=serialized,
        title=spec["title"],
        description=spec["description"],
    )
    row = {
        "title": spec["title"],
        "space_id": space.space_id,
        "env_var": spec["env_hint"],
        "tables": spec["tables"],
    }
    created.append(row)
    print(json.dumps(row, indent=2))

# COMMAND ----------

# DBTITLE 1,Optional — persist IDs for agent configuration
summary_df = spark.createDataFrame(created)
display(summary_df)

print(
    "\nExport these to your Model Serving endpoint or secrets:\n"
    + "\n".join(f"  {r['env_var']}={r['space_id']}" for r in created)
)
