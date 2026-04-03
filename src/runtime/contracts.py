from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RuntimeTransport(str, Enum):
    DIRECT = "direct"
    ENDPOINT = "endpoint"


class DataBackendMode(str, Enum):
    LOCAL = "local"
    DATABRICKS = "databricks"
    LAKEBASE = "lakebase"


class ModuleTransport(str, Enum):
    LOCAL = "local"
    MCP = "mcp"


class AnalyticsBackendMode(str, Enum):
    DATABRICKS_SQL = "databricks_sql"


@dataclass(frozen=True)
class RuntimeQuery:
    question: str
    corpus: str = "enron"
    conversation: list[dict] = field(default_factory=list)
    user_tier: str = ""
    permitted_books: list[str] = field(default_factory=list)
    endpoint_name: str = ""


@dataclass(frozen=True)
class ModuleContract:
    name: str
    transport: ModuleTransport
    description: str
    local_backend: DataBackendMode | None = None
    remote_backend: DataBackendMode | AnalyticsBackendMode | None = None
    read_only: bool = True


@dataclass(frozen=True)
class RuntimeTopology:
    transport: RuntimeTransport
    router: ModuleContract
    planner: ModuleContract
    graph: ModuleContract
    evidence: ModuleContract
    analytics: ModuleContract
    verifier_local: bool = True
