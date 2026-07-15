"""File-backed preference store with replace-by-key semantics."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from app.utils.path_utils import ensure_child, safe_path_part


class PreferenceEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: str
    key: str
    value: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreferenceStore:
    def __init__(self, root: Path = Path("output/memory")) -> None:
        self.root = root

    def _file(self, user_id: str) -> Path:
        folder = ensure_child(self.root, "users")
        return folder / f"{safe_path_part(user_id)}.json"

    def list(self, user_id: str) -> list[PreferenceEntry]:
        path = self._file(user_id)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [PreferenceEntry.model_validate(item) for item in payload]

    def upsert(self, entry: PreferenceEntry) -> PreferenceEntry:
        entries = {item.key: item for item in self.list(entry.user_id)}
        entry.updated_at = datetime.now(UTC)
        entries[entry.key] = entry
        path = self._file(entry.user_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in entries.values()],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return entry

    def delete(self, user_id: str, key: str) -> bool:
        entries = self.list(user_id)
        retained = [entry for entry in entries if entry.key != key]
        if len(retained) == len(entries):
            return False
        path = self._file(user_id)
        path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in retained],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return True
