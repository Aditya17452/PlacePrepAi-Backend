"""Vercel entry point.

Vercel's Python runtime looks for an ASGI `app` in the file its rewrite points at, so this
module exists only to expose the application. Everything else lives under `app/`.

The repository root is prepended to `sys.path` because a function's working directory is
not the repository root.

This file is intentionally outside `app/` — `api/` at the root is Vercel's own convention
and is unrelated to `app/api/`, which holds the routers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

__all__ = ["app"]
