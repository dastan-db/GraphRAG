from __future__ import annotations

import os
from dataclasses import dataclass

from .contracts import (
    AnalyticsBackendMode,
    DataBackendMode,
    ModuleContract,
    ModuleTransport,
    RuntimeTopology,
    RuntimeTransport,
)


_BACKEND_ALIAS_MAP = {
    "warehouse": DataBackendMode.DATABRICKS,
    "databricks_sql": DataBackendMode.DATABRICKS,
    "databricks": DataBackendMode.DATABRICKS,
    "sql": DataBackendMode.DATABRICKS,
    "lakebase": DataBackendMode.LAKEBASE,
    "local": DataBackendMode.LOCAL,
}


def resolve_data_backend(env: dict[str, str] | None = None) -> DataBackendMode:
    env = env or os.environ
    raw_backend = (
        env.get("GRAPHRAG_BACKEND")
        or env.get("GRAPHRAG_DATA_BACKEND")
        or "local"
    ).strip().lower()
    return _BACKEND_ALIAS_MAP.get(raw_backend, DataBackendMode.LOCAL)


def resolve_transport(env: dict[str, str] | None = None) -> RuntimeTransport:
    env = env or os.environ
    raw = (env.get("GRAPHRAG_RUNTIME_TRANSPORT") or "direct").strip().lower()
    if raw == RuntimeTransport.ENDPOINT.value:
        return RuntimeTransport.ENDPOINT
    return RuntimeTransport.DIRECT


def resolve_module_transport(
    env_name: str,
    *,
    env: dict[str, str] | None = None,
    default: ModuleTransport,
) -> ModuleTransport:
    env = env or os.environ
    raw = (env.get(env_name) or default.value).strip().lower()
    if raw == ModuleTransport.MCP.value:
        return ModuleTransport.MCP
    return ModuleTransport.LOCAL


def _app_backend_alias(backend: DataBackendMode) -> str:
    if backend == DataBackendMode.DATABRICKS:
        return "warehouse"
    return backend.value


@dataclass(frozen=True)
class RuntimeConfig:
    transport: RuntimeTransport
    data_backend: DataBackendMode
    llm_provider: str
    router_transport: ModuleTransport
    planner_transport: ModuleTransport
    graph_transport: ModuleTransport
    evidence_transport: ModuleTransport
    analytics_transport: ModuleTransport
    analytics_backend: AnalyticsBackendMode

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RuntimeConfig":
        env = env or os.environ
        data_backend = resolve_data_backend(env)
        return cls(
            transport=resolve_transport(env),
            data_backend=data_backend,
            llm_provider=(env.get("GRAPHRAG_LLM_PROVIDER") or "databricks").strip().lower(),
            router_transport=resolve_module_transport(
                "GRAPHRAG_ROUTER_TRANSPORT",
                env=env,
                default=ModuleTransport.LOCAL,
            ),
            planner_transport=resolve_module_transport(
                "GRAPHRAG_PLANNER_TRANSPORT",
                env=env,
                default=ModuleTransport.LOCAL,
            ),
            graph_transport=resolve_module_transport(
                "GRAPHRAG_GRAPH_TRANSPORT",
                env=env,
                default=ModuleTransport.LOCAL,
            ),
            evidence_transport=resolve_module_transport(
                "GRAPHRAG_EVIDENCE_TRANSPORT",
                env=env,
                default=ModuleTransport.LOCAL,
            ),
            analytics_transport=resolve_module_transport(
                "GRAPHRAG_ANALYTICS_TRANSPORT",
                env=env,
                default=ModuleTransport.LOCAL,
            ),
            analytics_backend=AnalyticsBackendMode.DATABRICKS_SQL,
        )

    def build_topology(self) -> RuntimeTopology:
        return RuntimeTopology(
            transport=self.transport,
            router=ModuleContract(
                name="RouterModule",
                transport=self.router_transport,
                description="Classifier, entity extraction, and train-only routing cases behind a stable router contract.",
                local_backend=self.data_backend,
                remote_backend=self.data_backend,
            ),
            planner=ModuleContract(
                name="PlannerModule",
                transport=self.planner_transport,
                description="Question decomposition and dependency-aware sub-question planning behind a stable planner contract.",
                local_backend=self.data_backend,
                remote_backend=self.data_backend,
            ),
            graph=ModuleContract(
                name="GraphRetrievalMCP",
                transport=self.graph_transport,
                description="Read-only graph retrieval backed by DuckDB locally or Lakebase remotely.",
                local_backend=DataBackendMode.LOCAL,
                remote_backend=DataBackendMode.LAKEBASE,
            ),
            evidence=ModuleContract(
                name="EvidenceMCP",
                transport=self.evidence_transport,
                description="Read-only evidence retrieval backed by DuckDB locally or Lakebase remotely.",
                local_backend=DataBackendMode.LOCAL,
                remote_backend=DataBackendMode.LAKEBASE,
            ),
            analytics=ModuleContract(
                name="AnalyticsMCP",
                transport=self.analytics_transport,
                description="Databricks SQL analytics over materialized views and metric views.",
                local_backend=DataBackendMode.DATABRICKS,
                remote_backend=self.analytics_backend,
            ),
            verifier_local=True,
        )

    def agent_environment(self, *, corpus: str) -> dict[str, str]:
        return {
            "GRAPHRAG_CORPUS": corpus,
            "GRAPHRAG_BACKEND": self.data_backend.value,
            "GRAPHRAG_DATA_BACKEND": _app_backend_alias(self.data_backend),
            "GRAPHRAG_RUNTIME_TRANSPORT": self.transport.value,
            "GRAPHRAG_ROUTER_TRANSPORT": self.router_transport.value,
            "GRAPHRAG_PLANNER_TRANSPORT": self.planner_transport.value,
            "GRAPHRAG_GRAPH_TRANSPORT": self.graph_transport.value,
            "GRAPHRAG_EVIDENCE_TRANSPORT": self.evidence_transport.value,
            "GRAPHRAG_ANALYTICS_TRANSPORT": self.analytics_transport.value,
        }
