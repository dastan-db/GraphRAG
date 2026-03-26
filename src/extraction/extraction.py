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

# DBTITLE 1,Slugify Entity Names
def slugify(name):
    """Convert an entity name to a stable ID."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
