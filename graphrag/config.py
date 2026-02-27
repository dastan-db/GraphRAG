from __future__ import annotations

from pydantic import BaseModel, Field


class Neo4jConfig(BaseModel):
    uri: str = Field(default="bolt://localhost:7687")
    username: str = Field(default="neo4j")
    password: str = Field(default="graphrag")
    database: str = Field(default="neo4j")


class LLMConfig(BaseModel):
    model_endpoint: str = Field(
        default="databricks-meta-llama-3-3-70b-instruct",
        description="Databricks Model Serving endpoint or Ollama model name",
    )
    provider: str = Field(
        default="databricks",
        description="LLM provider: 'databricks' or 'ollama'",
    )
    temperature: float = Field(default=0.0)


class DeltaConfig(BaseModel):
    catalog: str = Field(default="main")
    schema_name: str = Field(default="graphrag")
    entities_table: str = Field(default="entities")
    relationships_table: str = Field(default="relationships")
    code_snippets_table: str = Field(default="code_snippets")


class GraphRAGConfig(BaseModel):
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    delta: DeltaConfig = Field(default_factory=DeltaConfig)
