"""Vercel entry point.

Vercel's Python runtime looks for an ASGI `app` in the file a rewrite points at, so this
module exists only to expose the FastAPI application. All routing lives in `main.py`.

The parent directory is added to `sys.path` because the function's working directory is
not the repository root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402

__all__ = ["app"]
