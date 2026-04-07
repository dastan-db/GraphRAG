"""LangGraph / MLflow agent compatibility surface."""

from src.agent.agent_serving import (
    AGENT,
    ENRON_SYSTEM_PROMPT,
    GraphRAGAgent,
    SYSTEM_PROMPT,
    TOOL_MAP,
    _build_tool_map,
)

# Preserve the broad, easy-to-discover symbol name some tooling expects.
Agent = GraphRAGAgent

__all__ = [
    "AGENT",
    "Agent",
    "ENRON_SYSTEM_PROMPT",
    "GraphRAGAgent",
    "SYSTEM_PROMPT",
    "TOOL_MAP",
    "_build_tool_map",
]
