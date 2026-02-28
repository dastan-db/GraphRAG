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

# DBTITLE 1,Slugify Entity Names
def slugify(name):
    """Convert an entity name to a stable ID."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
