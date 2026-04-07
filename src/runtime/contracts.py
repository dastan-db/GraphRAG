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

@dataclass(frozen=True)
class RuntimeQuery:
    question: str
    corpus: str = "enron"
    conversation: list[dict] = field(default_factory=list)
    user_tier: str = ""
    permitted_books: list[str] = field(default_factory=list)
    endpoint_name: str = ""
