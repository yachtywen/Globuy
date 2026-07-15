"""Render relevant preference entries for the system-prompt tail."""

from collections.abc import Iterable

from app.memory.store import PreferenceEntry


def render_memory_context(
    entries: Iterable[PreferenceEntry],
    *,
    minimum_confidence: float = 0.5,
    limit: int = 12,
) -> str:
    selected = sorted(
        (entry for entry in entries if entry.confidence >= minimum_confidence),
        key=lambda entry: (entry.confidence, entry.updated_at),
        reverse=True,
    )[:limit]
    return "\n".join(
        f"- {entry.key}: {entry.value} (confidence={entry.confidence:.2f})" for entry in selected
    )
