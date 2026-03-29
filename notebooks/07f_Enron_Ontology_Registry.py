# Databricks notebook source
# MAGIC %md
# MAGIC # 07f — Enron Ontology Registry
# MAGIC
# MAGIC Seed **`ontology_registry`** from the corporate extraction prompts in
# MAGIC `src/extraction/extraction.py` (entity types + `CORPORATE_CANONICAL_REL_TYPES`).
# MAGIC
# MAGIC **Output table:** `{catalog}.{enron_schema}.ontology_registry`

# COMMAND ----------

# DBTITLE 1,Load Configuration
# MAGIC %run ../src/config

# COMMAND ----------

# DBTITLE 1,Import Libraries
from pyspark.sql.types import StringType, StructField, StructType

# COMMAND ----------

# DBTITLE 1,Ontology rows (aligned with CORPORATE_ENTITY_PROMPT_PREFIX + canonical rel types)
EXTRACTION_PROMPT_VERSION = "enron_corporate_extract_v1"

ENTITY_ROWS = [
    (
        "Person",
        "entity",
        "An individual involved in communications or decisions (full formal name, no titles).",
        "Kenneth Lay; Jeff Skilling; Sherron Watkins",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
    (
        "Organization",
        "entity",
        "A company, partnership, regulator, or other formal organization.",
        "Enron Corp; Arthur Andersen LLP; Federal Energy Regulatory Commission",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
    (
        "Division",
        "entity",
        "An internal business unit or operating division within a larger organization.",
        "Enron Broadband Services; Enron Energy Trading; Enron Wholesale Services",
        EXTRACTION_PROMPT_VERSION,
        "Organization",
    ),
    (
        "Project",
        "entity",
        "A named initiative, deal, venture, or program.",
        "Project Raptor; Dabhol Power; Azurix restructuring",
        EXTRACTION_PROMPT_VERSION,
        "Division",
    ),
    (
        "Meeting",
        "entity",
        "A scheduled or referenced meeting, call, board session, or strategy session.",
        "Q3 earnings prep call; board risk committee; weekly trading meeting",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
    (
        "Document",
        "entity",
        "A contract, filing, memo, presentation, report, or similar artifact.",
        "10-K draft; ISDA master; confidentiality agreement",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
    (
        "Location",
        "entity",
        "A geographic place, region, or facility referenced in context.",
        "Houston; California power market; Dabhol, India",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
    (
        "Financial_Event",
        "entity",
        "An earnings release, stock move, financing, acquisition, or similar market event.",
        "Q4 2000 earnings surprise; stock buyback announcement; bridge loan closing",
        EXTRACTION_PROMPT_VERSION,
        None,
    ),
]

RELATIONSHIP_ROWS = [
    ("REPORTS_TO", "relationship", "Subordinate reports to a manager or executive (source=subordinate, target=boss).", "Analyst REPORTS_TO VP; Trader REPORTS_TO desk head", EXTRACTION_PROMPT_VERSION, None),
    ("COLLABORATES_WITH", "relationship", "Peers work together without a strict reporting line.", "Legal COLLABORATES_WITH finance on disclosure", EXTRACTION_PROMPT_VERSION, None),
    ("MANAGES", "relationship", "A person has direct authority over another person or team.", "COO MANAGES wholesale trading desk", EXTRACTION_PROMPT_VERSION, None),
    ("PARTICIPATES_IN", "relationship", "An entity takes part in a meeting, project, or process.", "CFO PARTICIPATES_IN board audit session", EXTRACTION_PROMPT_VERSION, None),
    ("CREATES", "relationship", "An entity authors or produces a document, model, or deliverable.", "Treasury CREATES cash forecast spreadsheet", EXTRACTION_PROMPT_VERSION, None),
    ("REFERENCES", "relationship", "One entity cites or points to another (report, filing, data).", "Email REFERENCES prior SEC correspondence", EXTRACTION_PROMPT_VERSION, None),
    ("LOCATED_AT", "relationship", "An entity is situated at or tied to a place or facility.", "Trading desk LOCATED_AT Houston office", EXTRACTION_PROMPT_VERSION, None),
    ("PARTNERS_WITH", "relationship", "Formal or strategic partnership between organizations.", "Enron PARTNERS_WITH utility on structured deal", EXTRACTION_PROMPT_VERSION, None),
    ("SENT_TO", "relationship", "Communication flow from sender to recipient (email, memo).", "VP SENT_TO counsel re litigation risk", EXTRACTION_PROMPT_VERSION, None),
    ("DISCUSSES", "relationship", "A thread or person discusses a project, deal, or topic.", "Thread DISCUSSES California ISO refunds", EXTRACTION_PROMPT_VERSION, None),
    ("APPROVES", "relationship", "One party signs off on another's action or document.", "GC APPROVES settlement terms memo", EXTRACTION_PROMPT_VERSION, None),
    ("OPPOSES", "relationship", "Active disagreement or resistance to a proposal or party.", "Risk OPPOSES aggressive MTM methodology", EXTRACTION_PROMPT_VERSION, None),
    ("NEGOTIATES_WITH", "relationship", "Parties bargaining over terms, pricing, or structure.", "Commercial lead NEGOTIATES_WITH counterparty bank", EXTRACTION_PROMPT_VERSION, None),
    ("ADVISES", "relationship", "Counsel or expert provides guidance to a decision-maker.", "Outside counsel ADVISES board on fiduciary duties", EXTRACTION_PROMPT_VERSION, None),
    ("REVIEWS", "relationship", "One party examines another's work product.", "Accounting REVIEWS quarter-end adjustments", EXTRACTION_PROMPT_VERSION, None),
    ("ATTENDS", "relationship", "Participation in a meeting or event (invitation/host context).", "Exec ATTENDS offsite strategy session", EXTRACTION_PROMPT_VERSION, None),
    ("EMPLOYED_BY", "relationship", "Employment relationship (source=employee, target=employer).", "Analyst EMPLOYED_BY Enron Corp", EXTRACTION_PROMPT_VERSION, None),
    ("RELATED_TO", "relationship", "Generic association when a finer-grained type does not apply.", "Subsidiary RELATED_TO parent restructuring", EXTRACTION_PROMPT_VERSION, None),
    ("COMMUNICATES_WITH", "relationship", "Ongoing correspondence without a single directed email edge.", "Compliance COMMUNICATES_WITH FERC staff", EXTRACTION_PROMPT_VERSION, None),
    ("INVESTIGATES", "relationship", "Regulatory or legal scrutiny of an entity or conduct.", "DOJ INVESTIGATES off-balance-sheet vehicles", EXTRACTION_PROMPT_VERSION, None),
]

ALL_ROWS = ENTITY_ROWS + RELATIONSHIP_ROWS

schema = StructType(
    [
        StructField("type_name", StringType()),
        StructField("category", StringType()),
        StructField("definition", StringType()),
        StructField("examples", StringType()),
        StructField("extraction_prompt_version", StringType()),
        StructField("parent_type", StringType()),
    ]
)

# COMMAND ----------

# DBTITLE 1,Write ontology_registry
out_table = config["enron_ontology_registry_table"]

df = spark.createDataFrame(ALL_ROWS, schema)

(
    df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(out_table)
)

n = spark.table(out_table).count()
print(f"ontology_registry: {n} rows → {out_table}")
spark.table(out_table).orderBy("category", "type_name").show(30, truncate=60)
