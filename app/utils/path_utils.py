"""Safe paths for uploads, conversation sessions and generated output."""

import re
from pathlib import Path

_SAFE_PART = re.compile(r"[^a-zA-Z0-9_-]+")


def safe_path_part(value: str, fallback: str = "unknown") -> str:
    normalized = _SAFE_PART.sub("-", value).strip("-_")
    return (normalized or fallback)[:128]


def ensure_child(root: Path, *parts: str) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*(safe_path_part(part) for part in parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"路径越界: {candidate}")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def session_path(output_root: Path, thread_id: str) -> Path:
    return ensure_child(output_root, "sessions", thread_id)
