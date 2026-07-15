"""Compress messages before a breakpoint into a deterministic summary."""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from app.compress.breakpoint import calculate_breakpoint


class CompressionResult(BaseModel):
    compressed: bool
    summary: str = ""
    retained_count: int
    original_count: int


def compress_messages(
    messages: Sequence[BaseMessage],
    *,
    token_limit: int,
    keep_recent: int = 8,
) -> tuple[CompressionResult, list[BaseMessage]]:
    decision = calculate_breakpoint(
        messages,
        token_limit=token_limit,
        keep_recent=keep_recent,
    )
    if not decision.should_compress:
        result = CompressionResult(
            compressed=False,
            retained_count=len(messages),
            original_count=len(messages),
        )
        return result, list(messages)

    old_messages = messages[: decision.boundary]
    lines = []
    for message in old_messages:
        role = getattr(message, "type", "message")
        text = str(message.content).replace("\n", " ").strip()
        lines.append(f"{role}: {text[:240]}")
    summary = "历史上下文摘要：\n" + "\n".join(lines)
    retained = list(messages[decision.boundary :])
    result = CompressionResult(
        compressed=True,
        summary=summary,
        retained_count=len(retained),
        original_count=len(messages),
    )
    return result, retained
