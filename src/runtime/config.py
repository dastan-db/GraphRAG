from __future__ import annotations

import os
from dataclasses import dataclass

from .contracts import DataBackendMode, RuntimeTransport


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


def _app_backend_alias(backend: DataBackendMode) -> str:
    if backend == DataBackendMode.DATABRICKS:
        return "warehouse"
    return backend.value


@dataclass(frozen=True)
class RuntimeConfig:
    transport: RuntimeTransport
    data_backend: DataBackendMode
    llm_provider: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RuntimeConfig":
        env = env or os.environ
        return cls(
            transport=resolve_transport(env),
            data_backend=resolve_data_backend(env),
            llm_provider=(env.get("GRAPHRAG_LLM_PROVIDER") or "databricks").strip().lower(),
        )

    def agent_environment(self, *, corpus: str) -> dict[str, str]:
        return {
            "GRAPHRAG_CORPUS": corpus,
            "GRAPHRAG_BACKEND": self.data_backend.value,
            "GRAPHRAG_DATA_BACKEND": _app_backend_alias(self.data_backend),
            "GRAPHRAG_RUNTIME_TRANSPORT": self.transport.value,
            "GRAPHRAG_LLM_PROVIDER": self.llm_provider,
        }
