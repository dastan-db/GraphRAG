"""Query pipeline: natural language -> Cypher -> Neo4j -> answer."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseLanguageModel
from langchain_core.prompts import PromptTemplate
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph

from graphrag.config import GraphRAGConfig
from graphrag.query.prompts import ANSWER_SYNTHESIS_TEMPLATE, CYPHER_GENERATION_TEMPLATE

logger = logging.getLogger(__name__)


def _build_llm(config: GraphRAGConfig) -> BaseLanguageModel:
    """Instantiate the LLM based on the provider in config."""
    if config.llm.provider == "databricks":
        from langchain_databricks import ChatDatabricks

        return ChatDatabricks(
            endpoint=config.llm.model_endpoint,
            temperature=config.llm.temperature,
        )
    elif config.llm.provider == "ollama":
        from langchain_community.chat_models import ChatOllama

        return ChatOllama(
            model=config.llm.model_endpoint,
            temperature=config.llm.temperature,
        )
    else:
        raise ValueError(f"Unsupported LLM provider: {config.llm.provider}")


class GraphRAGQueryEngine:
    """Answers natural language questions about a codebase using a knowledge graph."""

    def __init__(
        self,
        config: GraphRAGConfig,
        llm: BaseLanguageModel | None = None,
    ):
        self.config = config
        self._graph = Neo4jGraph(
            url=config.neo4j.uri,
            username=config.neo4j.username,
            password=config.neo4j.password,
            database=config.neo4j.database,
        )
        self._llm = llm or _build_llm(config)

        cypher_prompt = PromptTemplate(
            input_variables=["question", "schema"],
            template=CYPHER_GENERATION_TEMPLATE,
        )
        qa_prompt = PromptTemplate(
            input_variables=["question", "context"],
            template=ANSWER_SYNTHESIS_TEMPLATE,
        )

        self._chain = GraphCypherQAChain.from_llm(
            llm=self._llm,
            graph=self._graph,
            cypher_prompt=cypher_prompt,
            qa_prompt=qa_prompt,
            verbose=True,
            return_intermediate_steps=True,
            validate_cypher=True,
            allow_dangerous_requests=True,
        )

    @classmethod
    def from_config(
        cls,
        config: GraphRAGConfig | None = None,
        llm: BaseLanguageModel | None = None,
    ) -> GraphRAGQueryEngine:
        return cls(config or GraphRAGConfig(), llm=llm)

    def query(self, question: str) -> dict[str, Any]:
        """Ask a question about the codebase.

        Returns a dict with keys:
          - answer: the synthesized answer string
          - cypher: the generated Cypher query
          - graph_results: raw results from Neo4j
        """
        result = self._chain.invoke({"query": question})
        intermediate = result.get("intermediate_steps", [])

        cypher_query = intermediate[0]["query"] if intermediate else ""
        graph_results = intermediate[1]["context"] if len(intermediate) > 1 else []

        return {
            "answer": result.get("result", ""),
            "cypher": cypher_query,
            "graph_results": graph_results,
        }
