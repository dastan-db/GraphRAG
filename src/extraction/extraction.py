# Databricks notebook source
# MAGIC %md
# MAGIC ### Extraction Utilities
# MAGIC Entity and relationship extraction prompts and helper functions for the knowledge graph build.

# COMMAND ----------

import re

# COMMAND ----------

# DBTITLE 1,Entity Extraction Prompt (for ai_query CONCAT)
ENTITY_PROMPT_PREFIX = """You are an expert biblical scholar. Extract all significant entities from the following chapter text.

For each entity, provide:
- name: The canonical name (e.g., Abraham not Abram unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept (treat God/Lord as Person)
- description: A brief description of this entity in context

Rules:
- Be comprehensive but only include entities actually mentioned in the text
- Use canonical names consistently
- Include divine figures (God, Lord, Holy Spirit) as Person type

"""

# COMMAND ----------

# DBTITLE 1,Relationship Extraction Prompt (for ai_query CONCAT)
RELATIONSHIP_SYSTEM_PROMPT = "You are a precise JSON extraction engine. Always return valid JSON only, no extra text."

RELATIONSHIP_PROMPT_PREFIX = """You are an expert biblical scholar. Given the following chapter text and a list of entities found in it, extract the relationships between these entities.

For each relationship, provide:
- source: Name of the source entity (must match an entity from the list)
- target: Name of the target entity (must match an entity from the list)
- relationship_type: One of: FAMILY_OF, PARENT_OF, CHILD_OF, SPOUSE_OF, ANCESTOR_OF, SPOKE_TO, COMMANDED, TRAVELED_TO, LOCATED_IN, PARTICIPATED_IN, LEADS, CREATED, PROMISED, PROPHESIED, BLESSED, SERVED, OPPOSED, WITNESSED
- description: A brief description of this specific relationship in context

Rules:
- Only use entity names from the provided list
- Each relationship should be grounded in what actually happens in this chapter
- Prefer specific relationship types over generic ones
- A single pair of entities can have multiple relationships

"""

# COMMAND ----------

# DBTITLE 1,Query Entity Extraction Prompt (for pre-linking user questions)
QUERY_ENTITY_PROMPT = """You are an expert biblical scholar. Extract all significant entities and concepts from the following user question.

For each entity, provide:
- name: The canonical name (e.g., Abraham not Abram unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept (treat God/Lord as Person)

Rules:
- Use canonical biblical names consistently
- Include divine figures (God, Lord, Holy Spirit) as Person type
- Include non-biblical terms exactly as the user stated them (e.g., "Arabs" stays "Arabs")
- Extract ALL nouns that could refer to entities, even if uncertain whether they appear in the Bible

Question:
"""

# COMMAND ----------

# DBTITLE 1,Corporate Entity Extraction Prompt (Enron)
CORPORATE_ENTITY_PROMPT_PREFIX = """You are a corporate communications analyst specializing in organizational intelligence. Extract all significant entities from the following email thread text.

For each entity, provide:
- name: The canonical name (e.g., "Kenneth Lay" not "Ken Lay" or "K. Lay"; "Enron Corp" not "Enron")
- entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event
- description: A brief description of this entity in context

Rules:
- Be comprehensive but only include entities actually mentioned in the text
- Use full canonical names for people (first and last name when available)
- NEVER use title prefixes (Dr., Mr., Mrs., Ms., Prof., Rev.) in entity names — use the bare name only (e.g., "Kenneth Lay" not "Dr. Kenneth Lay")
- NEVER abbreviate first names (e.g., "Kenneth Lay" not "Ken Lay", "Robert Smith" not "Bob Smith")
- If only an email address is visible, use the display name from the From/X-From header instead
- Use the EXACT SAME canonical name for the same person across all extractions
- Normalize company names consistently (e.g., "Enron Corp", "Arthur Andersen LLP")
- Classify internal business units as Division (e.g., "Enron Broadband Services", "Enron Energy Trading")
- Classify deals, initiatives, and ventures as Project (e.g., "Project Raptor", "Dabhol Power")
- Classify board meetings, strategy sessions, and scheduled calls as Meeting
- Classify contracts, reports, filings, and presentations as Document
- Classify earnings calls, SEC filings, stock events, acquisitions as Financial_Event

"""

# COMMAND ----------

# DBTITLE 1,Corporate Relationship Extraction Prompt (Enron)
CORPORATE_RELATIONSHIP_SYSTEM_PROMPT = "You are a precise JSON extraction engine. Always return valid JSON only, no extra text."

CORPORATE_RELATIONSHIP_PROMPT_PREFIX = """You are a corporate communications analyst. Given the following email thread text and a list of entities found in it, extract the relationships between these entities.

For each relationship, provide:
- source: Name of the source entity (must match an entity from the list)
- target: Name of the target entity (must match an entity from the list)
- relationship_type: One of: REPORTS_TO, COLLABORATES_WITH, MANAGES, PARTICIPATES_IN, CREATES, REFERENCES, LOCATED_AT, PARTNERS_WITH, SENT_TO, DISCUSSES, APPROVES, OPPOSES, NEGOTIATES_WITH, ADVISES, REVIEWS, ATTENDS
- description: A brief description of this specific relationship in context

Rules:
- Only use entity names from the provided list
- Each relationship should be grounded in what actually happens in this email thread
- Prefer specific relationship types over generic ones
- A single pair of entities can have multiple relationships
- SENT_TO captures explicit communication flow between people
- DISCUSSES captures when a person or meeting references a project, deal, or event
- MANAGES captures organizational authority (person manages division/project)

CRITICAL — edge direction rules (source → target):
- REPORTS_TO: the SUBORDINATE is source, the BOSS is target. "Alice reports to Bob" → source=Alice, target=Bob.
- MANAGES: the MANAGER is source, the MANAGED entity is target. "Bob manages Alice" → source=Bob, target=Alice.
- SENT_TO: the SENDER is source, the RECIPIENT is target.
- EMPLOYED_BY: the EMPLOYEE is source, the EMPLOYER is target.
If you are unsure about direction, re-read the email text and ask: "Who has authority over whom?" The authority figure is ALWAYS the target in REPORTS_TO and the source in MANAGES. Never reverse these.

"""

# COMMAND ----------

# DBTITLE 1,Corporate Query Entity Extraction Prompt (for pre-linking user questions)
CORPORATE_QUERY_ENTITY_PROMPT = """You are a corporate communications analyst. Extract all significant entities and concepts from the following user question about the Enron email corpus.

For each entity, provide:
- name: The canonical name (e.g., "Kenneth Lay" not "Ken"; "Enron Broadband Services" not "broadband")
- entity_type: One of: Person, Organization, Division, Project, Meeting, Document, Location, Financial_Event

Rules:
- Use full canonical names for people when possible
- Include company and division names as stated by the user
- Extract ALL nouns that could refer to entities in a corporate context
- Terms like "executives", "leadership", "management" should be extracted as Group-type concepts

Question:
"""

# COMMAND ----------

# DBTITLE 1,Canonical Relationship Type Normalization (Corporate)
CORPORATE_CANONICAL_REL_TYPES = {
    "REPORTS_TO", "COLLABORATES_WITH", "MANAGES", "PARTICIPATES_IN",
    "CREATES", "REFERENCES", "LOCATED_AT", "PARTNERS_WITH",
    "SENT_TO", "DISCUSSES", "APPROVES", "OPPOSES",
    "NEGOTIATES_WITH", "ADVISES", "REVIEWS", "ATTENDS",
    "EMPLOYED_BY", "RELATED_TO", "COMMUNICATES_WITH", "INVESTIGATES",
}

CORPORATE_REL_TYPE_MAP = {
    "WORKS_FOR": "EMPLOYED_BY",
    "EMPLOYEE_OF": "EMPLOYED_BY",
    "EMPLOYEE": "EMPLOYED_BY",
    "EMPLOYER": "EMPLOYED_BY",
    "WORKS_AT": "EMPLOYED_BY",
    "WORKS_IN": "EMPLOYED_BY",
    "WORKED_AT": "EMPLOYED_BY",
    "WORKS_WITH": "COLLABORATES_WITH",
    "WORKS_ON": "COLLABORATES_WITH",
    "ASSIGNED_TO": "COLLABORATES_WITH",
    "ASSISTS": "COLLABORATES_WITH",
    "ASSISTS_IN": "COLLABORATES_WITH",
    "SUPPORTS": "COLLABORATES_WITH",
    "CC": "SENT_TO",
    "CCS": "SENT_TO",
    "RECEIVES": "SENT_TO",
    "RECEIVES_EMAIL": "SENT_TO",
    "RECEIVED_FROM": "SENT_TO",
    "CONTACTS": "SENT_TO",
    "COMMUNICATES_WITH": "COMMUNICATES_WITH",
    "MEETS_WITH": "ATTENDS",
    "MEETS": "ATTENDS",
    "MET_WITH": "ATTENDS",
    "SCHEDULES": "ATTENDS",
    "SCHEDULED_AT": "ATTENDS",
    "SCHEDULED_ON": "ATTENDS",
    "SCHEDULED_FOR": "ATTENDS",
    "INVITES": "ATTENDS",
    "HOSTS": "ATTENDS",
    "MANAGED_BY": "REPORTS_TO",
    "CHAIRS": "MANAGES",
    "LEADS": "MANAGES",
    "ORGANIZES": "MANAGES",
    "SERVES": "MANAGES",
    "PART_OF": "PARTICIPATES_IN",
    "PART OF": "PARTICIPATES_IN",
    "MEMBER_OF": "PARTICIPATES_IN",
    "BELONGS_TO": "PARTICIPATES_IN",
    "INVOLVED_IN": "PARTICIPATES_IN",
    "ASSOCIATED_WITH": "PARTICIPATES_IN",
    "CREATED": "CREATES",
    "CREATED_BY": "CREATES",
    "PLANS": "CREATES",
    "SUBMITS": "CREATES",
    "SUBMITS_TO": "CREATES",
    "PRESENTS": "CREATES",
    "REPORTS_ON": "REFERENCES",
    "AFFECTS": "REFERENCES",
    "AFFECTED_BY": "REFERENCES",
    "RELATED_TO": "RELATED_TO",
    "LOCATED_IN": "LOCATED_AT",
    "LOCATED_NEAR": "LOCATED_AT",
    "OPERATES_IN": "LOCATED_AT",
    "VISITS": "LOCATED_AT",
    "VISITED": "LOCATED_AT",
    "SUBSIDIARY_OF": "PARTNERS_WITH",
    "CLIENT_OF": "PARTNERS_WITH",
    "SPONSORS": "PARTNERS_WITH",
    "REGULATES": "INVESTIGATES",
    "REGULATED_BY": "INVESTIGATES",
    "RECOMMENDS": "ADVISES",
}


def normalize_corporate_rel_type(rel_type):
    """Map a raw relationship type to a canonical type."""
    if rel_type is None:
        return "RELATED_TO"
    upper = rel_type.strip().upper().replace(" ", "_")
    if upper in CORPORATE_CANONICAL_REL_TYPES:
        return upper
    return CORPORATE_REL_TYPE_MAP.get(upper, "RELATED_TO")

# COMMAND ----------

# DBTITLE 1,Slugify Entity Names
def slugify(name):
    """Convert an entity name to a stable ID."""
    if name is None:
        return None
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
