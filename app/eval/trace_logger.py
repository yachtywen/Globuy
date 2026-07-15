"""Append high-value execution traces as JSON Lines."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.utils.thread_ctx import current_session_dir, current_thread_id


class TraceLogger:
    def __init__(self, fallback_root: Path = Path("output/traces")) -> None:
        self.fallback_root = fallback_root

    def append(self, event_type: str, payload: dict[str, Any]) -> Path:
        session_dir = current_session_dir()
        target_dir = session_dir or self.fallback_root
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "trace.jsonl"
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "thread_id": current_thread_id(),
            "type": event_type,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as destination:
            destination.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
