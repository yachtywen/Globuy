"""Calculate where old messages should be compressed."""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


def estimate_tokens(message: BaseMessage) -> int:
    return max(1, len(str(message.content)) // 4)


class BreakpointDecision(BaseModel):
    should_compress: bool
    boundary: int
    estimated_tokens: int
    token_limit: int


def calculate_breakpoint(
    messages: Sequence[BaseMessage],
    *,
    token_limit: int,
    keep_recent: int = 8,
) -> BreakpointDecision:
    total = sum(estimate_tokens(message) for message in messages)
    boundary = max(0, len(messages) - max(keep_recent, 1))
    return BreakpointDecision(
        should_compress=total > token_limit and boundary > 0,
        boundary=boundary,
        estimated_tokens=total,
        token_limit=token_limit,
    )
