"""Stable, provider-neutral metrics used by Langfuse and AG-UI summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from app.compress.breakpoint import estimate_tokens


def estimate_value_tokens(value: Any) -> int:
    """Return the project's bounded trend estimate; never a billable token count."""

    encoded = (
        json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    )
    return max(1, len(encoded) // 4)


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    message_count: int
    character_count: int
    estimated_tokens: int
    system_estimated_tokens: int
    history_estimated_tokens: int
    tool_result_estimated_tokens: int
    tool_message_count: int

    def metadata(self) -> dict[str, int]:
        return {
            "context_message_count": self.message_count,
            "context_character_count": self.character_count,
            "context_estimated_tokens": self.estimated_tokens,
            "system_estimated_tokens": self.system_estimated_tokens,
            "history_estimated_tokens": self.history_estimated_tokens,
            "tool_result_estimated_tokens": self.tool_result_estimated_tokens,
            "tool_message_count": self.tool_message_count,
        }


@dataclass(frozen=True, slots=True)
class GenerationUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_miss_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit: bool | None = None
    cache_hit_ratio: float | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "cache_type": "llm_prompt",
            "cache_hit": self.cache_hit,
            "cache_hit_ratio": self.cache_hit_ratio,
            **{
                key: value
                for key, value in asdict(self).items()
                if key not in {"cache_hit", "cache_hit_ratio"} and value is not None
            },
        }

    def langchain_usage_metadata(self) -> dict[str, Any] | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        input_details: dict[str, int] = {}
        output_details: dict[str, int] = {}
        if self.cache_read_input_tokens is not None:
            input_details["cache_read"] = self.cache_read_input_tokens
        if self.reasoning_tokens is not None:
            output_details["reasoning"] = self.reasoning_tokens
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens or self.input_tokens + self.output_tokens,
            "input_token_details": input_details,
            "output_token_details": output_details,
        }

    def langfuse_usage_details(self) -> dict[str, int] | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        details = {
            "input": max(0, self.input_tokens - (self.cache_read_input_tokens or 0)),
            "output": max(0, self.output_tokens - (self.reasoning_tokens or 0)),
            "total": self.total_tokens or self.input_tokens + self.output_tokens,
        }
        if self.cache_read_input_tokens is not None:
            details["input_cache_read"] = self.cache_read_input_tokens
        if self.reasoning_tokens is not None:
            details["output_reasoning"] = self.reasoning_tokens
        return details


@dataclass(frozen=True, slots=True)
class ToolMetrics:
    tool_name: str
    tool_call_id: str | None
    phase: str | None
    fork_depth: int
    status: str
    duration_ms: int
    result_estimated_tokens: int
    result_count: int | None = None
    cache_hit: bool | None = None
    degraded_reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class ToolObservationContext:
    tool_name: str
    tool_call_id: str
    phase: str | None
    started_at: float


_tool_context: ContextVar[ToolObservationContext | None] = ContextVar(
    "globuy_tool_observation_context", default=None
)


@contextmanager
def tool_observation_scope(
    tool_name: str, tool_call_id: str, phase: str | None, *, started_at: float
):
    token = _tool_context.set(ToolObservationContext(tool_name, tool_call_id, phase, started_at))
    try:
        yield
    finally:
        _tool_context.reset(token)


def current_tool_observation_context() -> ToolObservationContext | None:
    return _tool_context.get()


@dataclass(frozen=True, slots=True)
class CompressionMetrics:
    compression_triggered: bool
    before_estimated_tokens: int
    after_estimated_tokens: int
    removed_message_count: int
    retained_tool_group_count: int

    def metadata(self) -> dict[str, Any]:
        return {"cache_type": "context_breakpoint", **asdict(self)}


def context_metrics(messages: Sequence[BaseMessage]) -> ContextMetrics:
    total = sum(estimate_tokens(message) for message in messages)
    system = sum(
        estimate_tokens(message) for message in messages if isinstance(message, SystemMessage)
    )
    tool = sum(estimate_tokens(message) for message in messages if isinstance(message, ToolMessage))
    characters = sum(len(str(message.content)) for message in messages)
    return ContextMetrics(
        message_count=len(messages),
        character_count=characters,
        estimated_tokens=total,
        system_estimated_tokens=system,
        history_estimated_tokens=max(0, total - system - tool),
        tool_result_estimated_tokens=tool,
        tool_message_count=sum(isinstance(message, ToolMessage) for message in messages),
    )


def compression_metrics(
    before: Sequence[BaseMessage], update: Sequence[BaseMessage] | None
) -> CompressionMetrics:
    before_tokens = sum(estimate_tokens(message) for message in before)
    if update is None:
        return CompressionMetrics(False, before_tokens, before_tokens, 0, 0)
    retained = [message for message in update if not isinstance(message, RemoveMessage)]
    after_tokens = sum(estimate_tokens(message) for message in retained)
    retained_originals = sum(
        any(
            message is original
            or (
                getattr(message, "id", None) is not None
                and getattr(message, "id", None) == getattr(original, "id", None)
            )
            for message in retained
        )
        for original in before
    )
    return CompressionMetrics(
        compression_triggered=True,
        before_estimated_tokens=before_tokens,
        after_estimated_tokens=after_tokens,
        removed_message_count=max(0, len(before) - retained_originals),
        retained_tool_group_count=sum(
            isinstance(message, AIMessage) and bool(message.tool_calls) for message in retained
        ),
    )


def normalize_generation_usage(raw: Mapping[str, Any] | None) -> GenerationUsage:
    """Normalize OpenAI-compatible usage while preserving mutually exclusive buckets."""

    if not raw:
        return GenerationUsage()
    prompt = _integer(raw.get("prompt_tokens"))
    if prompt is None:
        prompt = _integer(raw.get("input_tokens"))
    completion = _integer(raw.get("completion_tokens"))
    if completion is None:
        completion = _integer(raw.get("output_tokens"))
    total = _integer(raw.get("total_tokens"))
    hit = _integer(raw.get("prompt_cache_hit_tokens"))
    miss = _integer(raw.get("prompt_cache_miss_tokens"))
    if hit is None:
        details = raw.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            hit = _integer(details.get("cached_tokens"))
    if hit is None:
        details = raw.get("input_token_details")
        if isinstance(details, Mapping):
            hit = _integer(details.get("cache_read"))
    completion_details = raw.get("completion_tokens_details")
    if not isinstance(completion_details, Mapping):
        completion_details = raw.get("output_token_details")
    reasoning = (
        _integer(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, Mapping)
        else None
    )
    input_tokens = prompt
    if hit is not None and miss is not None:
        input_tokens = hit + miss
    ratio_denominator = (hit or 0) + (miss or 0)
    return GenerationUsage(
        input_tokens=input_tokens,
        output_tokens=completion,
        total_tokens=total,
        cache_read_input_tokens=hit,
        cache_miss_input_tokens=miss,
        reasoning_tokens=reasoning,
        cache_hit=(hit > 0) if hit is not None else None,
        cache_hit_ratio=(
            (hit / ratio_denominator) if hit is not None and ratio_denominator else None
        ),
    )


def result_metrics(value: Any) -> tuple[str, int | None, bool | None, str | None]:
    if not isinstance(value, Mapping):
        return "ok", None, None, None
    status = str(value.get("status") or "ok")
    count = None
    for key in ("candidates", "picks", "offers", "results", "tool_results"):
        if isinstance(value.get(key), Sequence) and not isinstance(value.get(key), (str, bytes)):
            count = len(value[key])
            break
    cache_hit = value.get("cache_hit") if isinstance(value.get("cache_hit"), bool) else None
    degraded = value.get("degraded_reason")
    return status, count, cache_hit, str(degraded) if degraded else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


__all__ = [
    "CompressionMetrics",
    "ContextMetrics",
    "GenerationUsage",
    "ToolMetrics",
    "current_tool_observation_context",
    "context_metrics",
    "compression_metrics",
    "estimate_value_tokens",
    "normalize_generation_usage",
    "result_metrics",
    "tool_observation_scope",
]
