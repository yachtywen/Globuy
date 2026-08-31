"""Langfuse v4 callback adapter for OpenAI-compatible usage and safe tool metrics."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from langfuse.langchain import CallbackHandler

from app.observability.metrics import (
    ToolMetrics,
    current_tool_observation_context,
    estimate_value_tokens,
    normalize_generation_usage,
    result_metrics,
)
from app.utils.thread_ctx import current_fork_depth

logger = logging.getLogger(__name__)


class GlobuyLangfuseCallbackHandler(CallbackHandler):
    """Enrich the SDK's existing observations without creating duplicates."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_started: dict[UUID, float] = {}
        self._tool_contexts: dict[UUID, Any] = {}
        self._tool_metadata: dict[UUID, dict[str, Any]] = {}
        self._llm_metadata: dict[UUID, dict[str, Any]] = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._llm_metadata[run_id] = dict(metadata or {})
        return super().on_llm_start(
            serialized,
            prompts,
            run_id=run_id,
            parent_run_id=parent_run_id,
            metadata=metadata,
            **kwargs,
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._llm_metadata[run_id] = dict(metadata or {})
        return super().on_chat_model_start(
            serialized,
            messages,
            run_id=run_id,
            parent_run_id=parent_run_id,
            metadata=metadata,
            **kwargs,
        )

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            raw = _raw_usage(response)
            usage = normalize_generation_usage(raw)
            normalized = usage.langchain_usage_metadata()
            if normalized is not None:
                for group in getattr(response, "generations", []):
                    for generation in group:
                        message = getattr(generation, "message", None)
                        if message is not None:
                            message.usage_metadata = normalized
                observation = getattr(self, "_runs", {}).get(run_id)
                if observation is not None:
                    observation.update(
                        metadata={
                            **self._llm_metadata.get(run_id, {}),
                            **usage.metadata(),
                        }
                    )
        except Exception:  # noqa: BLE001 - observability must remain fail-open
            logger.warning("Langfuse usage normalization failed", exc_info=True)
        try:
            return super().on_llm_end(
                response, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )
        finally:
            self._llm_metadata.pop(run_id, None)

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        context = current_tool_observation_context()
        self._tool_started[run_id] = (
            context.started_at if context is not None else time.perf_counter()
        )
        self._tool_contexts[run_id] = context
        self._tool_metadata[run_id] = dict(metadata or {})
        return super().on_tool_start(
            serialized,
            input_str,
            run_id=run_id,
            parent_run_id=parent_run_id,
            metadata=metadata,
            **kwargs,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return super().on_llm_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
        finally:
            self._llm_metadata.pop(run_id, None)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        self._update_tool(run_id, output, fallback_status="ok")
        return super().on_tool_end(output, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        self._update_tool(run_id, None, fallback_status=_error_status(error))
        return super().on_tool_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def _update_tool(self, run_id: UUID, output: Any, *, fallback_status: str) -> None:
        try:
            started = self._tool_started.pop(run_id, time.perf_counter())
            context = self._tool_contexts.pop(run_id, None)
            inherited_metadata = self._tool_metadata.pop(run_id, {})
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            payload = _tool_payload(output)
            status, count, cache_hit, degraded = result_metrics(payload)
            observation = getattr(self, "_runs", {}).get(run_id)
            if observation is None:
                return
            metadata = ToolMetrics(
                tool_name=context.tool_name if context else "unknown",
                tool_call_id=context.tool_call_id if context else None,
                phase=context.phase if context else None,
                fork_depth=current_fork_depth(),
                status=status if payload is not None else fallback_status,
                duration_ms=duration_ms,
                result_estimated_tokens=estimate_value_tokens(payload),
                result_count=count,
                cache_hit=cache_hit,
                degraded_reason=degraded,
            ).metadata()
            if cache_hit is not None:
                metadata.update({"cache_type": "application", "cache_name": metadata["tool_name"]})
            observation.update(metadata={**inherited_metadata, **metadata})
        except Exception:  # noqa: BLE001
            logger.warning("Langfuse tool metric update failed", exc_info=True)


def _raw_usage(response: Any) -> Mapping[str, Any] | None:
    output = getattr(response, "llm_output", None)
    if isinstance(output, Mapping):
        for key in ("token_usage", "usage"):
            if isinstance(output.get(key), Mapping):
                return output[key]
    for group in getattr(response, "generations", []):
        for generation in group:
            message = getattr(generation, "message", None)
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, Mapping):
                for key in ("token_usage", "usage"):
                    if isinstance(response_metadata.get(key), Mapping):
                        return response_metadata[key]
            usage_metadata = getattr(message, "usage_metadata", None)
            if isinstance(usage_metadata, Mapping):
                return usage_metadata
    return None


def _tool_payload(output: Any) -> Any:
    import json

    content = getattr(output, "content", output)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


def _error_status(error: BaseException) -> str:
    import asyncio

    import httpx
    from pydantic import ValidationError

    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, ValidationError) or type(error).__name__ == "ToolInvocationError":
        return "validation_error"
    if isinstance(error, httpx.HTTPError):
        return "provider_error"
    return "unknown"


__all__ = ["GlobuyLangfuseCallbackHandler"]
