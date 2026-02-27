# Databricks notebook source
# MAGIC %md
# MAGIC ### Configuration
# MAGIC Shared configuration for the GraphRAG Solution Accelerator.

# COMMAND ----------

if 'config' not in locals():
    config = {}

# COMMAND ----------

# DBTITLE 1,Catalog and Schema
config['catalog'] = 'main'
config['schema'] = 'graphrag_bible'
config['llm_endpoint'] = 'databricks-meta-llama-3-3-70b-instruct'
config['small_llm_endpoint'] = 'databricks-meta-llama-3-1-8b-instruct'
config['embedding_endpoint'] = 'databricks-gte-large-en'

# COMMAND ----------

# DBTITLE 1,Table Names
config['verses_table'] = f"{config['catalog']}.{config['schema']}.verses"
config['entities_table'] = f"{config['catalog']}.{config['schema']}.entities"
config['relationships_table'] = f"{config['catalog']}.{config['schema']}.relationships"
config['entity_mentions_table'] = f"{config['catalog']}.{config['schema']}.entity_mentions"

# COMMAND ----------

# DBTITLE 1,Bible Books to Ingest
config['bible_books'] = {
    'Genesis': {'chapters': 50, 'testament': 'OT'},
    'Exodus': {'chapters': 40, 'testament': 'OT'},
    'Ruth': {'chapters': 4, 'testament': 'OT'},
    'Matthew': {'chapters': 28, 'testament': 'NT'},
    'Acts': {'chapters': 28, 'testament': 'NT'},
}

# COMMAND ----------

# DBTITLE 1,Create Schema
_ = spark.sql(f"CREATE CATALOG IF NOT EXISTS {config['catalog']}")
_ = spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config['catalog']}.{config['schema']}")
_ = spark.catalog.setCurrentDatabase(f"{config['catalog']}.{config['schema']}")

# COMMAND ----------

# DBTITLE 1,Teardown Helper
def teardown():
    """Drop all tables and schema. Use only for full reset."""
    for t in ['entity_mentions', 'relationships', 'entities', 'verses']:
        _ = spark.sql(f"DROP TABLE IF EXISTS {config['catalog']}.{config['schema']}.{t}")
    _ = spark.sql(f"DROP SCHEMA IF EXISTS {config['catalog']}.{config['schema']} CASCADE")

# COMMAND ----------

config
