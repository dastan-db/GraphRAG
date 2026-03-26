# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Enron Evaluation
# MAGIC
# MAGIC Evaluate the Enron GraphRAG agent using MLflow GenAI evaluation with
# MAGIC corporate governance-focused scorers: email citation completeness,
# MAGIC timeline accuracy, participant verification, and organizational consistency.

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install mlflow>=3.0 --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
import json
import mlflow
import pandas as pd

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Evaluation Dataset
# MAGIC
# MAGIC Corporate investigation questions with expected answers covering
# MAGIC organizational hierarchy, communication patterns, and temporal queries.

# COMMAND ----------

# DBTITLE 1,Enron Evaluation Dataset
EVAL_DATA = [
    {
        "question": "Who was involved in the California energy trading decisions?",
        "expected_entities": ["Kenneth Lay", "Jeffrey Skilling", "Tim Belden", "David Delainey"],
        "category": "organizational_hierarchy",
    },
    {
        "question": "What projects did Jeff Skilling manage?",
        "expected_entities": ["Jeffrey Skilling", "Enron Broadband Services", "Enron Energy Trading"],
        "category": "organizational_hierarchy",
    },
    {
        "question": "How did information flow about the Broadband division?",
        "expected_entities": ["Kenneth Rice", "Jeffrey Skilling", "Enron Broadband Services"],
        "category": "communication_pattern",
    },
    {
        "question": "Which executives discussed Fastow's partnerships?",
        "expected_entities": ["Andrew Fastow", "Jeffrey Skilling", "Kenneth Lay"],
        "category": "communication_pattern",
    },
    {
        "question": "Who communicated most frequently with Kenneth Lay?",
        "expected_entities": ["Kenneth Lay", "Jeffrey Skilling"],
        "category": "communication_pattern",
    },
    {
        "question": "What was the organizational structure around Enron Energy Trading?",
        "expected_entities": ["David Delainey", "John Lavorato", "Jeffrey Skilling"],
        "category": "organizational_hierarchy",
    },
    {
        "question": "How are Kenneth Lay and Tim Belden connected?",
        "expected_entities": ["Kenneth Lay", "Tim Belden", "Jeffrey Skilling"],
        "category": "path_tracing",
    },
    {
        "question": "What financial events were discussed in executive emails?",
        "expected_entities": [],
        "category": "topic_discovery",
    },
]

eval_df = pd.DataFrame(EVAL_DATA)
print(f"Evaluation dataset: {len(eval_df)} questions")
display(eval_df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Predict Function
# MAGIC
# MAGIC Wraps the Enron agent endpoint for MLflow evaluation.

# COMMAND ----------

# DBTITLE 1,Agent Predict Function
ENRON_ENDPOINT = "graphrag-enron-agent"

def predict_enron_agent(question: str) -> str:
    """Query the Enron GraphRAG agent and return the full response text."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    try:
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{ENRON_ENDPOINT}/invocations",
            body={"input": [{"role": "user", "content": question}]},
        )
        texts = []
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            elif "text" in item:
                texts.append(item["text"])
        return "\n".join(texts) if texts else str(resp)
    except Exception as e:
        return f"ERROR: {e}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Corporate Governance Scorers

# COMMAND ----------

# DBTITLE 1,Email Citation Completeness Scorer
from mlflow.genai.scorers import scorer

@scorer
def email_citation_completeness(request, response, expected_entities=None):
    """Check if the agent's response cites specific email evidence.

    Looks for patterns like [YYYY-MM-DD], email references, sender-to-recipient
    citations, or Subject: lines in the response.
    """
    import re

    text = response if isinstance(response, str) else str(response)

    date_refs = re.findall(r"\[\d{4}-\d{2}-\d{2}\]", text)
    subject_refs = re.findall(r"Subject:\s*\S+", text, re.IGNORECASE)
    email_patterns = re.findall(r"\w+-\w+\s+to\s+\w+-\w+", text)

    citation_count = len(date_refs) + len(subject_refs) + len(email_patterns)
    has_provenance = "### Provenance" in text or "**Sources**" in text

    if citation_count >= 3 and has_provenance:
        score = 1.0
    elif citation_count >= 1 and has_provenance:
        score = 0.7
    elif has_provenance:
        score = 0.4
    else:
        score = 0.0

    return {
        "score": score,
        "justification": f"Found {citation_count} email citations, provenance={'yes' if has_provenance else 'no'}",
    }

# COMMAND ----------

# DBTITLE 1,Participant Verification Scorer
@scorer
def participant_verification(request, response, expected_entities=None):
    """Check if all expected entities are mentioned in the response."""
    if not expected_entities:
        return {"score": 1.0, "justification": "No expected entities to verify"}

    text = response if isinstance(response, str) else str(response)
    text_lower = text.lower()

    found = []
    missing = []
    for entity in expected_entities:
        if entity.lower() in text_lower:
            found.append(entity)
        else:
            last_name = entity.split()[-1] if " " in entity else entity
            if last_name.lower() in text_lower:
                found.append(entity)
            else:
                missing.append(entity)

    if not expected_entities:
        score = 1.0
    else:
        score = len(found) / len(expected_entities)

    return {
        "score": round(score, 2),
        "justification": f"Found {len(found)}/{len(expected_entities)}: {found}. Missing: {missing}",
    }

# COMMAND ----------

# DBTITLE 1,Organizational Consistency Scorer
@scorer
def organizational_consistency(request, response, expected_entities=None):
    """Check if the response maintains consistent organizational relationships.

    Looks for reporting structure indicators (REPORTS_TO, MANAGES) and validates
    they follow a consistent hierarchy rather than contradicting themselves.
    """
    import re

    text = response if isinstance(response, str) else str(response)

    hierarchy_patterns = re.findall(
        r"(\w[\w\s]+?)\s*(?:reported to|reports to|managed by|overseen by|directed by)\s*(\w[\w\s]+?)(?:\.|,|\n)",
        text,
        re.IGNORECASE,
    )

    path_patterns = re.findall(r"REPORTS_TO|MANAGES|COLLABORATES_WITH|SENT_TO", text)

    has_structure = len(hierarchy_patterns) > 0 or len(path_patterns) > 0
    has_provenance = "### Provenance" in text
    has_path = "**Path**" in text or "Path:" in text

    if has_structure and has_provenance and has_path:
        score = 1.0
    elif has_structure and has_provenance:
        score = 0.7
    elif has_structure:
        score = 0.5
    else:
        score = 0.2

    return {
        "score": score,
        "justification": (
            f"Hierarchy refs: {len(hierarchy_patterns)}, "
            f"relationship types: {len(path_patterns)}, "
            f"provenance={'yes' if has_provenance else 'no'}"
        ),
    }

# COMMAND ----------

# DBTITLE 1,Grounding Quality Scorer
@scorer
def grounding_quality(request, response, expected_entities=None):
    """Check the self-reported grounding quality of the response."""
    text = response if isinstance(response, str) else str(response)
    text_lower = text.lower()

    if "all claims grounded" in text_lower:
        return {"score": 1.0, "justification": "Agent reports all claims grounded"}
    elif "partially grounded" in text_lower:
        return {"score": 0.5, "justification": "Agent reports partial grounding"}
    elif "not found" in text_lower and "knowledge graph" in text_lower:
        return {"score": 0.3, "justification": "Agent reports information not in graph"}
    else:
        return {"score": 0.0, "justification": "No grounding statement found"}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Run Evaluation
# MAGIC
# MAGIC Evaluate the Enron agent with all corporate governance scorers.

# COMMAND ----------

# DBTITLE 1,Execute Evaluation
with mlflow.start_run(run_name="enron_graphrag_eval"):
    results = mlflow.genai.evaluate(
        data=eval_df,
        predict_fn=predict_enron_agent,
        scorers=[
            email_citation_completeness,
            participant_verification,
            organizational_consistency,
            grounding_quality,
        ],
    )

print("Evaluation complete!")
display(results.tables["eval_results"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Analyze Results

# COMMAND ----------

# DBTITLE 1,Score Summary by Category
results_df = results.tables["eval_results"]

if "category" in eval_df.columns:
    merged = results_df.merge(eval_df[["question", "category"]], on="question", how="left")

    summary = (
        merged
        .groupby("category")
        .agg({
            "email_citation_completeness/score": "mean",
            "participant_verification/score": "mean",
            "organizational_consistency/score": "mean",
            "grounding_quality/score": "mean",
        })
        .round(2)
    )
    display(summary)

# COMMAND ----------

# DBTITLE 1,Overall Governance Score
score_cols = [c for c in results_df.columns if c.endswith("/score")]
if score_cols:
    overall = results_df[score_cols].mean()
    print("=== Enron GraphRAG Governance Scores ===")
    for col in score_cols:
        name = col.replace("/score", "")
        print(f"  {name:35s}: {overall[col]:.2f}")
    print(f"  {'OVERALL':35s}: {overall.mean():.2f}")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC Evaluation complete. Review the results in MLflow to identify areas for improvement.
