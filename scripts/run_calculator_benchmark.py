"""Compatibility entrypoint; implementation in ``scripts/_internal/run_calculator_benchmark.py``."""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parent / "_internal" / "run_calculator_benchmark.py"),
        run_name="__main__",
    )
