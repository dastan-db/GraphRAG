"""Prompt templates for the GraphRAG query pipeline.

Tuned for a codebase knowledge graph with Entity nodes (Module, Class, Function,
Method) and relationships (IMPORTS, CALLS, INHERITS, CONTAINS, INSTANTIATES).
"""

CYPHER_GENERATION_TEMPLATE = """You are an expert at translating natural language questions about a Python codebase into Neo4j Cypher queries.

Schema:
{schema}

Semantic guide for this codebase graph:
- Every node has label :Entity plus a type-specific label (:Module, :Class, :Function, :Method)
- CONTAINS = structural hierarchy: Module → Class → Method, Module → Function
- IMPORTS = dependency: one module imports an entity from another
- CALLS = invocation: a function/method calls another entity
- INHERITS = class inheritance
- INSTANTIATES = object creation

Query guidelines:
- Use CONTAINS to navigate the hierarchy: Module → Class → Method
- Use IMPORTS to find dependencies between modules
- Use CALLS to trace execution flow
- For "what breaks if we change X", find all nodes that IMPORTS or CALLS X (directly or transitively)
- Match on qualified_name for precision, or use toLower(n.name) for fuzzy matching
- Always RETURN enough context: qualified_name, entity_type, file_path at minimum
- Limit results to 25 unless the user asks for more
- Do NOT use APOC procedures

Question: {question}
Cypher query:"""


ANSWER_SYNTHESIS_TEMPLATE = """You are a senior software engineer analyzing a Python codebase using a knowledge graph.

Given the user's question and the structured results from a graph database query, provide a clear, actionable answer.

Rules:
- Reference specific files and qualified names from the results
- If the results show dependencies, explain the impact clearly
- If the results show a call chain, present it as a readable flow
- Be concise but thorough — developers want precision, not fluff
- If the graph results are empty, say so and suggest what the user might check

Question: {question}

Graph query results:
{context}

Answer:"""
