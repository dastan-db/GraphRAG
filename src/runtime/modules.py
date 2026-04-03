from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig
from .contracts import ModuleTransport, RuntimeTopology


def _load_agent_serving():
    import src.agent.agent_serving as agent_serving

    return agent_serving


class RouterLocalAdapter:
    def route(
        self,
        question: str,
        *,
        raw_question: str | None = None,
        contract: dict | None = None,
        routing_hint: dict | None = None,
    ) -> dict[str, Any]:
        agent_serving = _load_agent_serving()
        return agent_serving.classify_and_extract(
            question,
            raw_question=raw_question,
            contract=contract,
            routing_hint=routing_hint,
        )


class PlannerLocalAdapter:
    def plan(
        self,
        question: str,
        conversation_history: list[dict],
        entity_memory_context: str,
    ):
        agent_serving = _load_agent_serving()
        return agent_serving._plan_query(question, conversation_history, entity_memory_context)


def _create_mcp_client(server_url: str):
    if not server_url:
        return None
    try:
        from databricks.sdk import WorkspaceClient
        from databricks_mcp import DatabricksMCPClient
    except ImportError:
        return None

    return DatabricksMCPClient(
        server_url=server_url,
        workspace_client=WorkspaceClient(),
    )


def _normalize_mcp_result(result: Any):
    if result is None:
        return None
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    if isinstance(result, list):
        if len(result) == 1:
            return _normalize_mcp_result(result[0])
        return [_normalize_mcp_result(item) for item in result]
    if isinstance(result, dict):
        if "structuredContent" in result:
            return _normalize_mcp_result(result["structuredContent"])
        if "text" in result:
            return _normalize_mcp_result(result["text"])
        if "content" in result:
            return _normalize_mcp_result(result["content"])
        return result
    return result


class RouterMcpAdapter:
    def __init__(self, server_url: str):
        self.server_url = server_url
        self._fallback = RouterLocalAdapter()

    def route(
        self,
        question: str,
        *,
        raw_question: str | None = None,
        contract: dict | None = None,
        routing_hint: dict | None = None,
    ) -> dict[str, Any]:
        client = _create_mcp_client(self.server_url)
        if client is None:
            return self._fallback.route(
                question,
                raw_question=raw_question,
                contract=contract,
                routing_hint=routing_hint,
            )
        try:
            result = client.call_tool(
                "route_question",
                {
                    "question": question,
                    "raw_question": raw_question or question,
                    "contract": contract or {},
                    "routing_hint": routing_hint or {},
                },
            )
            normalized = _normalize_mcp_result(result)
            if isinstance(normalized, dict) and normalized:
                return normalized
        except Exception:
            pass
        return self._fallback.route(
            question,
            raw_question=raw_question,
            contract=contract,
            routing_hint=routing_hint,
        )


class PlannerMcpAdapter:
    def __init__(self, server_url: str):
        self.server_url = server_url
        self._fallback = PlannerLocalAdapter()

    def plan(
        self,
        question: str,
        conversation_history: list[dict],
        entity_memory_context: str,
    ):
        client = _create_mcp_client(self.server_url)
        if client is None:
            return self._fallback.plan(question, conversation_history, entity_memory_context)
        try:
            result = client.call_tool(
                "plan_query",
                {
                    "question": question,
                    "conversation_history": conversation_history,
                    "entity_memory_context": entity_memory_context,
                },
            )
            normalized = _normalize_mcp_result(result)
            if normalized:
                return normalized
        except Exception:
            pass
        return self._fallback.plan(question, conversation_history, entity_memory_context)


class LocalToolAdapter:
    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def invoke(self, payload: dict[str, Any]):
        agent_serving = _load_agent_serving()
        if not agent_serving.TOOL_MAP:
            agent_serving._build_tool_map()
        return agent_serving.TOOL_MAP[self.tool_name].invoke(payload)


class McpToolAdapter:
    def __init__(self, tool_name: str, server_url: str):
        self.tool_name = tool_name
        self.server_url = server_url

    def invoke(self, payload: dict[str, Any]):
        client = _create_mcp_client(self.server_url)
        if client is None:
            return []
        try:
            return _normalize_mcp_result(client.call_tool(self.tool_name, payload))
        except Exception:
            return []


def _tool_adapter(
    tool_name: str,
    *,
    transport: ModuleTransport,
    server_url: str,
):
    if transport == ModuleTransport.MCP:
        return McpToolAdapter(tool_name, server_url)
    return LocalToolAdapter(tool_name)


@dataclass
class RuntimeModuleSet:
    topology: RuntimeTopology
    router: Any
    planner: Any
    graph_tools: dict[str, Any]
    evidence_tools: dict[str, Any]
    analytics_tools: dict[str, Any]

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> "RuntimeModuleSet":
        topology = config.build_topology()
        server_url = (
            os.environ.get("GRAPHRAG_RUNTIME_MCP_URL")
            or os.environ.get("GRAPHFRAMES_MCP_URL")
            or ""
        )
        return cls(
            topology=topology,
            router=(
                RouterMcpAdapter(server_url)
                if topology.router.transport == ModuleTransport.MCP
                else RouterLocalAdapter()
            ),
            planner=(
                PlannerMcpAdapter(server_url)
                if topology.planner.transport == ModuleTransport.MCP
                else PlannerLocalAdapter()
            ),
            graph_tools={
                "find_entity": _tool_adapter(
                    "find_entity",
                    transport=topology.graph.transport,
                    server_url=server_url,
                ),
                "find_connections": _tool_adapter(
                    "find_connections",
                    transport=topology.graph.transport,
                    server_url=server_url,
                ),
                "get_entity_summary": _tool_adapter(
                    "get_entity_summary",
                    transport=topology.graph.transport,
                    server_url=server_url,
                ),
                "trace_path": _tool_adapter(
                    "trace_path",
                    transport=topology.graph.transport,
                    server_url=server_url,
                ),
            },
            evidence_tools={
                "get_source_evidence": _tool_adapter(
                    "get_source_evidence",
                    transport=topology.evidence.transport,
                    server_url=server_url,
                ),
                "get_emails_between": _tool_adapter(
                    "get_emails_between",
                    transport=topology.evidence.transport,
                    server_url=server_url,
                ),
                "get_relationship_evidence": _tool_adapter(
                    "get_relationship_evidence",
                    transport=topology.evidence.transport,
                    server_url=server_url,
                ),
                "get_hierarchy_evidence": _tool_adapter(
                    "get_hierarchy_evidence",
                    transport=topology.evidence.transport,
                    server_url=server_url,
                ),
                "search_emails": _tool_adapter(
                    "search_emails",
                    transport=topology.evidence.transport,
                    server_url=server_url,
                ),
            },
            analytics_tools={
                "query_and_enrich": _tool_adapter(
                    "query_and_enrich",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
                "get_top_individuals": _tool_adapter(
                    "get_top_individuals",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
                "get_top_email_pairs": _tool_adapter(
                    "get_top_email_pairs",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
                "find_top_contacts": _tool_adapter(
                    "find_top_contacts",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
                "get_communication_stats": _tool_adapter(
                    "get_communication_stats",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
                "get_dyad_topics": _tool_adapter(
                    "get_dyad_topics",
                    transport=topology.analytics.transport,
                    server_url=server_url,
                ),
            },
        )
