"""Re-export Bible registry for Dash pages that only add ``src/app`` to ``sys.path``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_canonical = Path(__file__).resolve().parent.parent / "bible_registry.py"
_spec = importlib.util.spec_from_file_location("_graphrag_bible_registry_canon", _canonical)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load bible registry from {_canonical}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
BIBLE_BOOKS_ALL = _mod.BIBLE_BOOKS_ALL

__all__ = ["BIBLE_BOOKS_ALL"]
