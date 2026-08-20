"""globuy backend package."""

from __future__ import annotations

import asyncio
import sys

# Psycopg's asynchronous implementation requires a selector loop on Windows.
# Configure the policy while the package is imported, before Uvicorn/Alembic
# creates the process event loop.
if sys.platform == "win32":  # pragma: no cover - exercised by Windows runtime
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

__version__ = "0.1.0"
