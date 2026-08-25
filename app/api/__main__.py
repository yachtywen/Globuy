"""Cross-platform API launcher that installs Globuy's event-loop policy first."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    # ``auto`` asks Uvicorn to create a Proactor loop on Windows. Psycopg's
    # async implementation requires a Selector loop, whose policy is installed
    # by ``app.__init__`` before this module is executed. Let asyncio create the
    # loop from that policy instead of overriding it in Uvicorn.
    settings = get_settings()
    uvicorn.run(
        "app.api.server:app",
        host=settings.host,
        port=settings.port,
        loop="none",
    )


if __name__ == "__main__":
    main()
