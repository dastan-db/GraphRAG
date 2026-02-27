# Databricks notebook source
# MAGIC %md
# MAGIC # GraphRAG Solution Accelerator — Run Setup
# MAGIC
# MAGIC This notebook creates a Databricks Workflow to run the full GraphRAG pipeline.
# MAGIC
# MAGIC **Steps:**
# MAGIC 1. Attach this notebook to any cluster (DBR 15.4+ recommended) and hit **Run All**.
# MAGIC 2. A multi-step job will be created. Follow the printed link to run it.
# MAGIC 3. Or run the notebooks interactively in order: `00` → `01` → `02` → `03` → `04`.

# COMMAND ----------

# DBTITLE 1,Install Companion Library
# MAGIC %pip install databricks-sdk --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Define Workflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import (
    Task,
    NotebookTask,
    JobCluster,
    ClusterSpec,
    AutoScale,
)
import re

w = WorkspaceClient()

notebook_dir = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
notebook_dir = "/".join(notebook_dir.split("/")[:-1])

job_json = {
    "name": "[Solution Accelerator] GraphRAG — Bible Knowledge Graph",
    "timeout_seconds": 28800,
    "max_concurrent_runs": 1,
    "tags": {"usage": "solacc", "domain": "graphrag"},
    "tasks": [
        {
            "task_key": "00_Intro_and_Config",
            "notebook_task": {"notebook_path": f"{notebook_dir}/00_Intro_and_Config"},
            "job_cluster_key": "graphrag_cluster",
        },
        {
            "task_key": "01_Data_Prep",
            "notebook_task": {"notebook_path": f"{notebook_dir}/01_Data_Prep"},
            "job_cluster_key": "graphrag_cluster",
            "depends_on": [{"task_key": "00_Intro_and_Config"}],
        },
        {
            "task_key": "02_Build_Knowledge_Graph",
            "notebook_task": {"notebook_path": f"{notebook_dir}/02_Build_Knowledge_Graph"},
            "job_cluster_key": "graphrag_cluster",
            "depends_on": [{"task_key": "01_Data_Prep"}],
        },
        {
            "task_key": "03_Build_Agent",
            "notebook_task": {"notebook_path": f"{notebook_dir}/03_Build_Agent"},
            "job_cluster_key": "graphrag_cluster",
            "depends_on": [{"task_key": "02_Build_Knowledge_Graph"}],
        },
        {
            "task_key": "04_Query_Demo",
            "notebook_task": {"notebook_path": f"{notebook_dir}/04_Query_Demo"},
            "job_cluster_key": "graphrag_cluster",
            "depends_on": [{"task_key": "03_Build_Agent"}],
        },
        {
            "task_key": "05_Evaluation",
            "notebook_task": {"notebook_path": f"{notebook_dir}/05_Evaluation"},
            "job_cluster_key": "graphrag_cluster",
            "depends_on": [{"task_key": "03_Build_Agent"}],
        },
    ],
    "job_clusters": [
        {
            "job_cluster_key": "graphrag_cluster",
            "new_cluster": {
                "spark_version": "15.4.x-ml-scala2.12",
                "num_workers": 0,
                "node_type_id": "i3.xlarge",
                "spark_conf": {
                    "spark.databricks.delta.formatCheck.enabled": "false",
                    "spark.master": "local[*]",
                },
                "custom_tags": {"usage": "solacc_graphrag"},
            },
        }
    ],
}

# COMMAND ----------

# DBTITLE 1,Create or Update Job
existing_jobs = list(w.jobs.list(name="[Solution Accelerator] GraphRAG — Bible Knowledge Graph"))

if existing_jobs:
    job_id = existing_jobs[0].job_id
    w.jobs.reset(job_id=job_id, new_settings=job_json)
    print(f"Updated existing job: {job_id}")
else:
    job = w.jobs.create(**job_json)
    job_id = job.job_id
    print(f"Created new job: {job_id}")

# COMMAND ----------

# DBTITLE 1,Print Job Link
workspace_url = spark.conf.get("spark.databricks.workspaceUrl", "")
print(f"\nJob URL: https://{workspace_url}/#job/{job_id}")
print(f"\nRun it with: Run Now button in the Jobs UI, or run notebooks interactively in order.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC &copy; 2026 Databricks, Inc. All rights reserved.
