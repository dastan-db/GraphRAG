# Databricks notebook source
# MAGIC %md
# MAGIC ### Configuration
# MAGIC Shared configuration for the GraphRAG Solution Accelerator.

# COMMAND ----------

if 'config' not in locals():
    config = {}

# COMMAND ----------

# DBTITLE 1,Catalog and Schema
config['catalog'] = 'serverless_8e8gyh_catalog'
config['schema'] = 'graphrag_bible'
config['llm_endpoint'] = 'databricks-meta-llama-3-3-70b-instruct'
config['small_llm_endpoint'] = 'databricks-meta-llama-3-1-8b-instruct'
config['embedding_endpoint'] = 'databricks-gte-large-en'
config['external_llm_endpoint'] = 'databricks-gpt-5-2'
config['judge_endpoint'] = 'databricks-claude-sonnet-4-6'

# COMMAND ----------

# DBTITLE 1,Table Names
config['verses_table'] = f"{config['catalog']}.{config['schema']}.verses"
config['chapters_table'] = f"{config['catalog']}.{config['schema']}.chapters"
config['entities_table'] = f"{config['catalog']}.{config['schema']}.entities"
config['relationships_table'] = f"{config['catalog']}.{config['schema']}.relationships"
config['entity_mentions_table'] = f"{config['catalog']}.{config['schema']}.entity_mentions"
config['agent_prompts_table'] = f"{config['catalog']}.{config['schema']}.agent_prompts"
config['entity_analytics_table'] = f"{config['catalog']}.{config['schema']}.entity_analytics"
config['entity_paths_table'] = f"{config['catalog']}.{config['schema']}.entity_paths"
config['book_registry_table'] = f"{config['catalog']}.{config['schema']}.book_registry"

# COMMAND ----------

# DBTITLE 1,Enron Corpus Config
config['enron_schema'] = 'graphrag_enron'
config['enron_emails_table'] = f"{config['catalog']}.{config['enron_schema']}.emails"
config['enron_participants_table'] = f"{config['catalog']}.{config['enron_schema']}.participants"
config['enron_threads_table'] = f"{config['catalog']}.{config['enron_schema']}.threads"
config['enron_entities_table'] = f"{config['catalog']}.{config['enron_schema']}.entities"
config['enron_relationships_table'] = f"{config['catalog']}.{config['enron_schema']}.relationships"
config['enron_entity_mentions_table'] = f"{config['catalog']}.{config['enron_schema']}.entity_mentions"
config['enron_entity_analytics_table'] = f"{config['catalog']}.{config['enron_schema']}.entity_analytics"
config['enron_entity_paths_table'] = f"{config['catalog']}.{config['enron_schema']}.entity_paths"

config['enron_key_custodians'] = [
    'lay-k', 'skilling-j', 'fastow-a', 'delainey-d', 'dasovich-j',
    'kaminski-v', 'kitchen-l', 'shackleton-s', 'germany-c', 'bass-e',
    'allen-p', 'arnold-j', 'beck-s', 'blair-l', 'campbell-l',
]

config['enron_max_emails'] = 20000

config['enron_communication_dyads_table'] = f"{config['catalog']}.{config['enron_schema']}.communication_dyads"
config['enron_person_activity_table'] = f"{config['catalog']}.{config['enron_schema']}.person_activity"
config['enron_org_hierarchy_table'] = f"{config['catalog']}.{config['enron_schema']}.org_hierarchy"
config['enron_investigation_timeline_table'] = f"{config['catalog']}.{config['enron_schema']}.investigation_timeline"
config['enron_person_identity_table'] = f"{config['catalog']}.{config['enron_schema']}.person_identity"
config['enron_ontology_registry_table'] = f"{config['catalog']}.{config['enron_schema']}.ontology_registry"
config['enron_corpus_coverage_table'] = f"{config['catalog']}.{config['enron_schema']}.corpus_coverage"

config['enron_extraction_provenance_table'] = f"{config['catalog']}.{config['enron_schema']}.extraction_provenance"
config['enron_entity_resolution_audit_table'] = f"{config['catalog']}.{config['enron_schema']}.entity_resolution_audit"
config['enron_email_classification_table'] = f"{config['catalog']}.{config['enron_schema']}.email_classification"
config['enron_data_quality_report_table'] = f"{config['catalog']}.{config['enron_schema']}.data_quality_report"
config['enron_person_role_timeline_table'] = f"{config['catalog']}.{config['enron_schema']}.person_role_timeline"
config['enron_topic_taxonomy_table'] = f"{config['catalog']}.{config['enron_schema']}.topic_taxonomy"
config['enron_pipeline_lineage_table'] = f"{config['catalog']}.{config['enron_schema']}.pipeline_lineage"
config['enron_agent_query_log_table'] = f"{config['catalog']}.{config['enron_schema']}.agent_query_log"
config['enron_org_hierarchy_evidence_table'] = f"{config['catalog']}.{config['enron_schema']}.org_hierarchy_evidence"

# COMMAND ----------

# DBTITLE 1,Evidence Traceability Config (tunable knobs)
config['evidence_config'] = {
    # C1: Build-time evidence linking (K1-K8)
    "strategy_weights": {"A": 1.0, "B": 0.7, "C": 0.9, "D": 0.4},
    "snippet_length": 500,
    "max_emails_per_pair": 20,
    "date_proximity_boost": 0.0,
    "date_proximity_window_days": 90,
    "recipient_type_weights": {"TO": 1.0, "CC": 0.6, "BCC": 0.3},
    "mass_mail_threshold": 5,
    "org_keyword_boost": 0.0,
    "min_relevance_threshold": 0.3,
    # C2: Evidence retrieval (K9-K14)
    "default_sort_order": "relevance",
    "body_preview_length": 1200,
    "thread_cap": 20,
    "email_type_thresholds": {"direct": 3, "group": 10},
    "expose_vector_scores": True,
    "preserve_source_threads": True,
    # C3: Evidence ranking (K15-K18)
    "signal_weights": {
        "direct_recipient": 1.0,
        "cc_recipient": 0.6,
        "body_mention": 0.55,
        "thread_cooccurrence": 0.3,
        "temporal_proximity": 0.2,
        "email_type_penalty": -0.3,
        "vector_similarity": 0.5,
        "org_keyword": 0.0,
    },
    "min_display_threshold": 0.2,
    "reranking_mode": "heuristic",
    "evidence_dedup": "thread-level",
    # C4: Pattern orchestration (K19-K24)
    "auto_evidence_mode": "always",
    "evidence_step_position": "late",
    "citation_depth": "both",
    "confidence_calibration": "hybrid",
    "evidence_sufficiency_threshold": 2,
    # C5: Evaluation (K25-K28)
    "plateau_window": 2,
    "plateau_threshold_pp": 2,
}

config['tool_sla_thresholds_ms'] = {
    "find_entity": 3000,
    "find_connections": 8000,
    "trace_path": 20000,
    "get_source_evidence": 15000,
    "get_source_context": 15000,
    "get_entity_summary": 15000,
    "graph_exhaustion_check": 20000,
}

# COMMAND ----------

# DBTITLE 1,Enron ABAC Config
config['enron_sensitivity_tiers'] = {
    'legal_team':     ['general', 'executive_confidential', 'attorney_client_privileged'],
    'executive_team': ['general', 'executive_confidential'],
    'analyst_team':   ['general'],
}

_enron_abac = f"{config['catalog']}.{config['enron_schema']}"
config['enron_abac_entities_view'] = f"{_enron_abac}.entities_abac"
config['enron_abac_relationships_view'] = f"{_enron_abac}.relationships_abac"
config['enron_abac_entity_mentions_view'] = f"{_enron_abac}.entity_mentions_abac"
config['enron_abac_entity_paths_view'] = f"{_enron_abac}.entity_paths_abac"
config['enron_abac_entity_analytics_view'] = f"{_enron_abac}.entity_analytics_abac"
config['enron_abac_emails_view'] = f"{_enron_abac}.emails_abac"

config['enron_abac_row_filter_fn'] = f"{_enron_abac}.email_access_filter"
config['enron_abac_col_mask_fn'] = f"{_enron_abac}.mask_bcc"

# COMMAND ----------

# DBTITLE 1,Bible Books to Ingest
# Full 66-book KJV Bible corpus (loaded eagerly from bible_registry below)
# This dict is populated after BIBLE_BOOKS_ALL is imported.

# COMMAND ----------

# DBTITLE 1,Complete Bible Registry (66 books)
import sys, os
try:
    from bible_registry import BIBLE_BOOKS_ALL
except ModuleNotFoundError:
    _found = False
    try:
        _nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
        if '/notebooks/' in _nb_path:
            _ws_base = '/Workspace' + _nb_path.split('/notebooks/')[0]
        else:
            _ws_base = '/Workspace' + _nb_path.rsplit('/', 1)[0]
        for _sub in ['src', '.', 'src/agent']:
            _candidate = os.path.join(_ws_base, _sub)
            if os.path.isfile(os.path.join(_candidate, 'bible_registry.py')):
                sys.path.insert(0, _candidate)
                _found = True
                break
    except Exception:
        pass
    if not _found:
        for _p in [os.path.join(os.getcwd(), 'src'), os.getcwd()]:
            if os.path.isfile(os.path.join(_p, 'bible_registry.py')):
                sys.path.insert(0, _p)
                break
    from bible_registry import BIBLE_BOOKS_ALL
config['bible_books_all'] = BIBLE_BOOKS_ALL
config['bible_books'] = BIBLE_BOOKS_ALL

# COMMAND ----------

# DBTITLE 1,Create Schemas
_ = spark.sql(f"USE CATALOG {config['catalog']}")
_ = spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config['catalog']}.{config['schema']}")
_ = spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config['catalog']}.{config['enron_schema']}")
_ = spark.sql(f"USE SCHEMA {config['schema']}")

# COMMAND ----------

# DBTITLE 1,Teardown Helper
def teardown():
    """Drop all tables and schema. Use only for full reset."""
    for t in ['entity_analytics', 'entity_paths', 'entity_mentions', 'relationships',
              'entities', 'chapters', 'verses', 'book_registry']:
        _ = spark.sql(f"DROP TABLE IF EXISTS {config['catalog']}.{config['schema']}.{t}")
    _ = spark.sql(f"DROP SCHEMA IF EXISTS {config['catalog']}.{config['schema']} CASCADE")

# COMMAND ----------

# DBTITLE 1,Book Registry Helpers
def init_book_registry(existing_books=None):
    """Create and populate the book_registry table from bible_books_all.

    Args:
        existing_books: list of book names already ingested (marked 'active').
                        Defaults to the keys in config['bible_books'].
    """
    if existing_books is None:
        existing_books = list(config['bible_books'].keys())

    reg_table = config['book_registry_table']
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {reg_table} (
            book_name STRING,
            testament STRING,
            total_chapters INT,
            status STRING,
            entity_count INT,
            relationship_count INT,
            verse_count INT,
            added_at TIMESTAMP,
            updated_at TIMESTAMP
        ) USING DELTA
    """)

    from pyspark.sql.types import StructType, StructField, StringType, IntegerType, TimestampType
    import pyspark.sql.functions as F

    schema = StructType([
        StructField("book_name", StringType()),
        StructField("testament", StringType()),
        StructField("total_chapters", IntegerType()),
        StructField("status", StringType()),
    ])
    rows = []
    for name, meta in config['bible_books_all'].items():
        status = 'active' if name in existing_books else 'available'
        rows.append((name, meta['testament'], meta['chapters'], status))

    df = (
        spark.createDataFrame(rows, schema)
        .withColumn("entity_count", F.lit(0))
        .withColumn("relationship_count", F.lit(0))
        .withColumn("verse_count", F.lit(0))
        .withColumn("added_at", F.when(F.col("status") == "active", F.current_timestamp()))
        .withColumn("updated_at", F.current_timestamp())
    )

    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(reg_table)
    active_count = df.filter("status = 'active'").count()
    print(f"Book registry: {df.count()} books ({active_count} active) → {reg_table}")

# COMMAND ----------

config
