"""CLI entry point for the versioned OpenSearch product index."""

from __future__ import annotations

import json

from app.search.service import build_default_index


def main() -> None:
    print(json.dumps(build_default_index(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
