# Databricks notebook source
# MAGIC %md
# MAGIC ### Extraction Utilities
# MAGIC LLM prompts and parsing logic for entity and relationship extraction.

# COMMAND ----------

import json
import re
import time
from openai import OpenAI

# COMMAND ----------

# DBTITLE 1,LLM Client Setup
def get_llm_client():
    """Return an OpenAI-compatible client pointed at the Databricks Foundation Model API."""
    import mlflow.deployments
    return mlflow.deployments.get_deploy_client("databricks")

# COMMAND ----------

# DBTITLE 1,Entity Extraction Prompt
ENTITY_EXTRACTION_PROMPT = """You are an expert biblical scholar. Given the following chapter text from the Bible, extract all significant entities mentioned.

For each entity, provide:
- name: The canonical name (e.g., "Abraham" not "Abram" unless before the name change)
- entity_type: One of: Person, Place, Event, Group, Concept
- description: A one-sentence description of who/what this is in context

Return ONLY a JSON array of objects. Example:
[
  {"name": "Abraham", "entity_type": "Person", "description": "Patriarch called by God to leave Ur and father of the nation of Israel"},
  {"name": "Canaan", "entity_type": "Place", "description": "The promised land that God gave to Abraham and his descendants"}
]

Rules:
- Include God/Lord as entity_type "Person" (divine person)
- Groups like "Israelites", "Pharisees", "Twelve Apostles" are entity_type "Group"
- Abstract concepts like "covenant", "faith", "salvation" are entity_type "Concept"
- Events like "The Exodus", "The Crucifixion", "The Flood" are entity_type "Event"
- Be comprehensive but only include entities actually mentioned in the text
- Use canonical names (Moses not "he", Jerusalem not "the city" unless no name given)

Book: {book}, Chapter: {chapter}
Text:
{text}

JSON array of entities:"""

# COMMAND ----------

# DBTITLE 1,Relationship Extraction Prompt
RELATIONSHIP_EXTRACTION_PROMPT = """You are an expert biblical scholar. Given the following chapter text and a list of entities found in it, extract the relationships between these entities.

For each relationship, provide:
- source: Name of the source entity (must match an entity from the list)
- target: Name of the target entity (must match an entity from the list)
- relationship_type: One of: FAMILY_OF, PARENT_OF, CHILD_OF, SPOUSE_OF, ANCESTOR_OF, SPOKE_TO, COMMANDED, TRAVELED_TO, LOCATED_IN, PARTICIPATED_IN, LEADS, CREATED, PROMISED, PROPHESIED, BLESSED, SERVED, OPPOSED, WITNESSED
- description: A brief description of this specific relationship in context

Return ONLY a JSON array of objects. Example:
[
  {{"source": "God", "target": "Abraham", "relationship_type": "SPOKE_TO", "description": "God called Abraham to leave his country and go to Canaan"}},
  {{"source": "Abraham", "target": "Canaan", "relationship_type": "TRAVELED_TO", "description": "Abraham journeyed to the land of Canaan as God commanded"}}
]

Rules:
- Only use entity names from the provided list
- Each relationship should be grounded in what actually happens in this chapter
- Prefer specific relationship types over generic ones
- A single pair of entities can have multiple relationships

Book: {book}, Chapter: {chapter}

Entities found in this chapter:
{entities_list}

Text:
{text}

JSON array of relationships:"""

# COMMAND ----------

# DBTITLE 1,Call LLM with Retry
def call_llm(client, endpoint, prompt, max_retries=3):
    """Call the Foundation Model API with retry logic."""
    for attempt in range(max_retries):
        try:
            response = client.predict(
                endpoint=endpoint,
                inputs={
                    "messages": [
                        {"role": "system", "content": "You are a precise JSON extraction engine. Always return valid JSON arrays only, no extra text."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
            )
            return response.choices[0]["message"]["content"]
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  Retry {attempt + 1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"  Failed after {max_retries} attempts: {e}")
                return "[]"

# COMMAND ----------

# DBTITLE 1,Parse JSON from LLM Response
def parse_json_response(text):
    """Extract a JSON array from LLM response text, handling common formatting issues."""
    text = text.strip()
    # Try to find JSON array in the response
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Try the whole thing
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []

# COMMAND ----------

# DBTITLE 1,Slugify Entity Names
def slugify(name):
    """Convert an entity name to a stable ID."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
