"""Compatibility shim; implementation in ``src._internal.evaluation.runtime_baselines``."""

from src._internal.evaluation.runtime_baselines import (  # noqa: F401
    DEFAULT_BASELINE_PATH,
    DEFAULT_RUNTIME_BASELINES,
    load_runtime_baselines,
    write_runtime_baselines,
)

__all__ = [
    "DEFAULT_BASELINE_PATH",
    "DEFAULT_RUNTIME_BASELINES",
    "load_runtime_baselines",
    "write_runtime_baselines",
]
