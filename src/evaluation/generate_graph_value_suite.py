"""Compatibility shim; implementation in ``src._internal.evaluation.generate_graph_value_suite``."""

from __future__ import annotations

import importlib
import runpy
import sys

_SKIP = frozenset(
    {
        "__name__",
        "__package__",
        "__loader__",
        "__spec__",
        "__file__",
        "__cached__",
        "__doc__",
    }
)

_MOD = "src._internal.evaluation.generate_graph_value_suite"
_core = importlib.import_module(_MOD)
_ns = globals()
for _k, _v in vars(_core).items():
    if _k in _SKIP:
        continue
    _ns[_k] = _v

if __name__ == "__main__":
    sys.argv[0] = __file__
    runpy.run_module(_MOD, alter_sys=True)
