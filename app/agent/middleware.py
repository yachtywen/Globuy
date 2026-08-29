"""Tool execution guards, compaction, loop detection, and cache-breakpoint helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.api.monitor import current_monitor
from app.compress.breakpoint import estimate_tokens
from app.config import get_settings
from app.observability.metrics import estimate_value_tokens, tool_observation_scope

logger = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in {"items", "picks", "learned_preferences"} and isinstance(value, list):
            safe[f"{key}_count"] = len(value)
        elif isinstance(value, str):
            safe[key] = value[:500]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, dict):
            safe[key] = {
                str(child_key): child_value
                for child_key, child_value in list(value.items())[:20]
                if isinstance(child_value, (str, int, float, bool)) or child_value is None
            }
        else:
            safe[key] = str(value)[:500]
    return safe


def _strip_private_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private_payload(child)
            for key, child in value.items()
            if key not in {"raw_evidence", "content_vector", "embedding", "prompt"}
        }
    if isinstance(value, list):
        return [_strip_private_payload(child) for child in value]
    return value


def _trim_value(value: Any, *, budget: int) -> tuple[Any, bool]:
    """Keep JSON structure while bounding the approximate serialized size."""

    if budget <= 32:
        return "[truncated]", True
    if isinstance(value, str):
        if len(value) <= budget:
            return value, False
        return value[: max(0, budget - 20)] + "…[truncated]", True
    if isinstance(value, list):
        result: list[Any] = []
        used = 2
        truncated = False
        for child in value:
            remaining = budget - used
            if remaining <= 32:
                truncated = True
                break
            compacted, child_truncated = _trim_value(child, budget=remaining)
            encoded = _json(compacted)
            if used + len(encoded) > budget:
                truncated = True
                break
            result.append(compacted)
            used += len(encoded) + 1
            truncated = truncated or child_truncated
        if len(result) < len(value):
            truncated = True
        return result, truncated
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        used = 2
        truncated = False
        for key, child in value.items():
            remaining = budget - used - len(str(key)) - 4
            if remaining <= 32:
                truncated = True
                break
            compacted, child_truncated = _trim_value(child, budget=remaining)
            encoded = _json(compacted)
            if used + len(str(key)) + len(encoded) + 4 > budget:
                truncated = True
                break
            result[str(key)] = compacted
            used += len(str(key)) + len(encoded) + 4
            truncated = truncated or child_truncated
        if len(result) < len(value):
            truncated = True
        if truncated:
            result["_truncated"] = True
        return result, truncated
    return value, False


def compact_tool_content(tool_name: str, content: Any) -> Any:
    """Compact one ToolMessage without changing the direct Python tool return."""
    """这一步压缩的是“LLM 消息里的工具结果”，不会改变工具在 Python 内部的原始返回值。"""
    if not isinstance(content, str):
        return content
    try:
        payload = json.loads(content)
    except ValueError:
        payload = content
    payload = _strip_private_payload(payload)
    settings = get_settings()
    if tool_name == "item_search" and isinstance(payload, dict):
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and len(candidates) > settings.fork_candidate_limit:
            payload["candidates"] = candidates[: settings.fork_candidate_limit]
            payload["truncated"] = True
    char_budget = settings.tool_result_token_limit * 4
    compacted, _ = _trim_value(payload, budget=char_budget)
    return _json(compacted) if not isinstance(compacted, str) else compacted


def _result_summary(tool_name: str, content: Any) -> dict[str, Any]:
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return {
            "status": "ok",
            "content_length": len(content),
            "tool_result_estimated_tokens": estimate_value_tokens(content),
        }
    if not isinstance(payload, dict):
        return {
            "status": "ok",
            "result_type": type(payload).__name__,
            "tool_result_estimated_tokens": estimate_value_tokens(payload),
        }
    summary: dict[str, Any] = {"status": payload.get("status", "ok")}
    for key in ("platform", "terminal", "truncated", "total_recall"):
        if key in payload:
            summary[key] = payload[key]
    for key in ("candidates", "picks", "offers", "results", "tool_results"):
        if isinstance(payload.get(key), list):
            summary[f"{key}_count"] = len(payload[key])
    for key in (
        "cache_type",
        "cache_name",
        "cache_hit",
        "cache_key_hash",
        "cache_ttl_seconds",
        "partial",
        "degraded_reason",
    ):
        if key in payload:
            summary[key] = payload[key]
    summary["tool_result_estimated_tokens"] = estimate_value_tokens(payload)
    summary["tool_name"] = tool_name
    return summary


async def _observe_rejected_tool(
    request: ToolCallRequest,
    message: ToolMessage,
    *,
    started_at: float,
    phase: str | None,
) -> None:
    """Close one callback-backed tool observation when middleware short-circuits."""

    config = request.runtime.config
    try:
        callback_manager = AsyncCallbackManager.configure(
            config.get("callbacks"),
            None,
            False,
            config.get("tags"),
            None,
            config.get("metadata"),
            None,
        )
        with tool_observation_scope(
            str(message.name or "unknown"),
            str(message.tool_call_id),
            phase,
            started_at=started_at,
        ):
            run_manager = await callback_manager.on_tool_start(
                {
                    "name": str(message.name or "unknown"),
                    "description": "middleware-rejected tool call",
                },
                str(request.tool_call.get("args") or {}),
                name=str(message.name or "unknown"),
                inputs=_safe_arguments(request.tool_call.get("args") or {}),
                tool_call_id=str(message.tool_call_id),
            )
            await run_manager.on_tool_end(message, name=str(message.name or "unknown"))
    except Exception:  # noqa: BLE001 - telemetry cannot change the tool result
        logger.warning("Rejected tool observation failed", exc_info=True)


async def guarded_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    """Publish one standard tool lifecycle and compact the resulting ToolMessage."""

    call = request.tool_call
    call_id = str(call.get("id") or "unknown")
    name = str(call.get("name") or "unknown")
    arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
    state = request.state if isinstance(request.state, dict) else {}
    if name == "item_search" and not arguments.get("intent") and state.get("shopping_intent"):
        arguments = {**arguments, "intent": state["shopping_intent"]}
        request = request.override(tool_call={**call, "args": arguments})
    monitor = current_monitor()
    started = time.perf_counter()
    if monitor is not None:
        await monitor.report_tool_start(call_id, name, _safe_arguments(arguments))
    if name == "item_search" and not state.get("shopping_intent") and not arguments.get("intent"):
        payload = {
            "status": "needs_planning",
            "message": "商品搜索前必须先通过 planner 形成结构化购物意图。",
        }
        rejected = ToolMessage(
            content=_json(payload), name=name, tool_call_id=call_id, status="error"
        )
        await _observe_rejected_tool(
            request,
            rejected,
            started_at=started,
            phase=state.get("decision_phase"),
        )
        if monitor is not None:
            await monitor.report_tool_end(
                call_id,
                {
                    "status": "needs_planning",
                    "tool_name": name,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        return rejected
    if (
        name == "dispatch_tool"
        and arguments.get("target_platform")
        and not state.get("shopping_intent")
        and not arguments.get("shopping_intent")
    ):
        payload = {
            "status": "needs_planning",
            "message": "商品检索分支必须继承 planner 已验证的结构化购物意图。",
        }
        rejected = ToolMessage(
            content=_json(payload), name=name, tool_call_id=call_id, status="error"
        )
        await _observe_rejected_tool(
            request,
            rejected,
            started_at=started,
            phase=state.get("decision_phase"),
        )
        if monitor is not None:
            await monitor.report_tool_end(
                call_id,
                {
                    "status": "needs_planning",
                    "tool_name": name,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        return rejected
    decision_phase = state.get("decision_phase")
    if decision_phase in {"think", "reflect"}:
        from app.tools import TOOL_PHASES

        if name not in TOOL_PHASES[decision_phase]:
            payload = {
                "status": "phase_rejected",
                "message": f"{name} 不允许在 {decision_phase} 阶段执行。",
            }
            rejected = ToolMessage(
                content=_json(payload),
                name=name,
                tool_call_id=call_id,
                status="error",
            )
            await _observe_rejected_tool(
                request,
                rejected,
                started_at=started,
                phase=state.get("decision_phase"),
            )
            if monitor is not None:
                await monitor.report_tool_end(
                    call_id,
                    {
                        "status": "phase_rejected",
                        "tool_name": name,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
            return rejected
    try:
        with tool_observation_scope(
            name,
            call_id,
            state.get("decision_phase"),
            started_at=started,
        ):
            result = await execute(request)
    except BaseException as exc:
        if monitor is not None:
            if isinstance(exc, asyncio.CancelledError):
                status = "cancelled"
            elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                status = "timeout"
            else:
                status = "error"
            await monitor.report_tool_end(
                call_id,
                {
                    "status": status,
                    "tool_name": name,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    if isinstance(result, ToolMessage):
        result = result.model_copy(update={"content": compact_tool_content(name, result.content)})
        result_summary = _result_summary(name, result.content)
    else:
        result_summary = {"status": "ok", "tool_name": name}
    if monitor is not None:
        await monitor.report_tool_end(
            call_id,
            {**result_summary, "duration_ms": duration_ms},
        )
    return result


def tool_records(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    """Extract completed tool calls as stable signature/result-digest records."""

    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    records: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[str(call.get("id") or "")] = (
                    str(call.get("name") or ""),
                    call.get("args") if isinstance(call.get("args"), dict) else {},
                )
        elif isinstance(message, ToolMessage):
            name, arguments = calls.get(str(message.tool_call_id), (str(message.name or ""), {}))
            signature = hashlib.sha256(f"{name}:{_json(arguments)}".encode()).hexdigest()
            digest = hashlib.sha256(str(message.content).encode("utf-8")).hexdigest()
            records.append({"tool_name": name, "signature": signature, "result_digest": digest})
    return records


def loop_detected(records: Sequence[dict[str, str]]) -> bool:
    settings = get_settings()
    recent = list(records)[-settings.loop_detection_window :]
    for record in recent:
        repeats = sum(
            candidate["signature"] == record["signature"]
            and candidate["result_digest"] == record["result_digest"]
            for candidate in recent
        )
        if repeats >= settings.loop_repeat_threshold:
            return True
    return False


def repair_incomplete_tool_groups(
    messages: Sequence[BaseMessage],
) -> list[BaseMessage] | None:
    """Close interrupted assistant tool calls before the next model request.

    OpenAI-compatible APIs require every assistant tool call to be followed by
    one matching ToolMessage before any later human/assistant message. A graph
    interruption can checkpoint the assistant message between those two nodes.
    Rebuild only those broken groups and persist an explicit interrupted result.
    """

    repaired: list[BaseMessage] = []
    changed = False
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            repaired.append(message)
            index += 1
            continue

        valid_calls = [call for call in message.tool_calls if str(call.get("id") or "")]
        if len(valid_calls) != len(message.tool_calls):
            message = message.model_copy(update={"tool_calls": valid_calls})
            changed = True
        repaired.append(message)

        following: list[ToolMessage] = []
        cursor = index + 1
        while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
            following.append(messages[cursor])
            cursor += 1
        by_id = {str(item.tool_call_id): item for item in following}
        declared_ids = {str(call["id"]) for call in valid_calls}
        for call in valid_calls:
            call_id = str(call["id"])
            existing = by_id.get(call_id)
            if existing is not None:
                repaired.append(existing)
                continue
            repaired.append(
                ToolMessage(
                    name=str(call.get("name") or "interrupted_tool"),
                    tool_call_id=call_id,
                    content=_json(
                        {
                            "status": "interrupted",
                            "terminal": False,
                            "message": "上一轮工具调用在完成前中断，已安全关闭。",
                        }
                    ),
                )
            )
            changed = True
        if any(str(item.tool_call_id) not in declared_ids for item in following):
            changed = True
        index = cursor

    if not changed:
        return None
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]


def _safe_boundary(messages: Sequence[BaseMessage], keep_recent_groups: int) -> int:
    group_starts = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage) and bool(message.tool_calls)
    ]
    if len(group_starts) <= keep_recent_groups:
        return 0
    boundary = group_starts[-keep_recent_groups]
    # Never begin the retained suffix with a ToolMessage.
    while boundary > 0 and isinstance(messages[boundary], ToolMessage):
        boundary -= 1
    return boundary


def _context_breakpoint_limits(settings: Any) -> tuple[int, int]:
    """Resolve window-derived limits while accepting legacy test settings."""

    trigger = getattr(settings, "compression_trigger_tokens", None)
    if trigger is None:
        trigger = settings.compression_token_limit
    target = getattr(settings, "compression_target_tokens", None)
    if target is None:
        target = trigger
    return int(trigger), min(int(target), int(trigger))


def _bounded_history_summary(
    messages: Sequence[BaseMessage], *, token_budget: int
) -> SystemMessage | None:
    header = "以下是 Cache Breakpoint 之前的历史摘要；它不包含当前未完成的工具调用：\n"
    lines: list[str] = []
    for message in messages:
        role = getattr(message, "type", "message")
        text = str(message.content).replace("\n", " ").strip()
        if text:
            lines.append(f"{role}: {text[:300]}")
    if not lines:
        return None

    # Prefer the most recent part of the old prefix. This remains deterministic,
    # is frozen until the next high-watermark crossing, and keeps the replacement
    # below the low-watermark budget in ordinary cases.
    kept: list[str] = []
    for line in reversed(lines):
        candidate = [line, *kept]
        content = header + "\n".join(candidate)
        if kept and estimate_tokens(SystemMessage(content=content)) > token_budget:
            break
        kept = candidate
    omitted = len(kept) < len(lines)
    if omitted:
        kept.insert(0, "[更早的历史已在上一个 Cache Breakpoint 中省略]")
    return SystemMessage(name="history_summary", content=header + "\n".join(kept))


def cache_breakpoint_update(messages: Sequence[BaseMessage]) -> list[BaseMessage] | None:
    """Compress at a model-window high watermark toward a stable low watermark."""

    settings = get_settings()
    total = sum(estimate_tokens(message) for message in messages)
    trigger_tokens, target_tokens = _context_breakpoint_limits(settings)
    if total <= trigger_tokens:
        return None
    boundary = _safe_boundary(messages, settings.compression_keep_recent)
    if boundary <= 0:
        return None

    retained = list(messages[boundary:])
    retained_tokens = sum(estimate_tokens(message) for message in retained)
    summary = _bounded_history_summary(
        messages[:boundary],
        token_budget=max(1_024, target_tokens - retained_tokens),
    )
    if summary is None:
        return None
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        summary,
        *retained,
    ]


__all__ = [
    "cache_breakpoint_update",
    "compact_tool_content",
    "guarded_tool_call",
    "loop_detected",
    "repair_incomplete_tool_groups",
    "tool_records",
]
