"""GraphRAG agent for Model Serving — loads implementation into this module.

The body lives in ``_agent_core.py`` (same directory) and is executed with
``__name__ == "src.agent.agent_serving"`` so function globals and
``patch("src.agent.agent_serving.…")`` behave like a single module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_impl_path = Path(__file__).resolve().with_name("_agent_core.py")
_spec = importlib.util.spec_from_file_location(__name__, _impl_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load agent implementation from {_impl_path}")

_mod = sys.modules[__name__]
_spec.loader.exec_module(_mod)
