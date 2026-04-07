"""Factual hardening orchestrator — loads implementation into this module (see ``agent_serving``)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_impl_path = (
    Path(__file__).resolve().parents[1]
    / "_internal"
    / "agent"
    / "factual_parallel_orchestrator.py"
)
_spec = importlib.util.spec_from_file_location(__name__, _impl_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load orchestrator from {_impl_path}")

_mod = sys.modules[__name__]
_spec.loader.exec_module(_mod)
