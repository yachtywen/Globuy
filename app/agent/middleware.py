"""Tool execution guards, compaction, loop detection, and cache-breakpoint helpers."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

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
        return {"status": "ok", "content_length": len(content)}
    if not isinstance(payload, dict):
        return {"status": "ok", "result_type": type(payload).__name__}
    summary: dict[str, Any] = {"status": payload.get("status", "ok")}
    for key in ("platform", "terminal", "truncated", "total_recall"):
        if key in payload:
            summary[key] = payload[key]
    for key in ("candidates", "picks", "offers", "results", "tool_results"):
        if isinstance(payload.get(key), list):
            summary[f"{key}_count"] = len(payload[key])
    summary["tool_name"] = tool_name
    return summary


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
        if monitor is not None:
            await monitor.report_tool_end(
                call_id, {"status": "needs_planning", "tool_name": name, "duration_ms": 0}
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
        return ToolMessage(
            content=_json(payload), name=name, tool_call_id=call_id, status="error"
        )
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
            if monitor is not None:
                await monitor.report_tool_end(
                    call_id,
                    {"status": "phase_rejected", "tool_name": name, "duration_ms": 0},
                )
            return rejected
    try:
        result = await execute(request)
    except BaseException:
        if monitor is not None:
            await monitor.report_tool_end(
                call_id,
                {"status": "error", "tool_name": name, "duration_ms": 0},
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


# 先粗略估算token数，超过12000token才压缩，
def cache_breakpoint_update(messages: Sequence[BaseMessage]) -> list[BaseMessage] | None:
    """Replace only old complete message groups when the token boundary is crossed."""

    settings = get_settings()
    total = sum(estimate_tokens(message) for message in messages)
    if total <= settings.compression_token_limit:
        return None
    boundary = _safe_boundary(messages, settings.compression_keep_recent)
    if boundary <= 0:
        return None

    lines: list[str] = []
    for message in messages[:boundary]:
        role = getattr(message, "type", "message")
        text = str(message.content).replace("\n", " ").strip()
        if text:
            lines.append(f"{role}: {text[:300]}")
    if not lines:
        return None
    summary = SystemMessage(
        name="history_summary",
        content=(
            "以下是 Cache Breakpoint 之前的历史摘要；它不包含当前未完成的工具调用：\n"
            + "\n".join(lines)
        ),
    )
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        summary,
        *messages[boundary:],
    ]


__all__ = [
    "cache_breakpoint_update",
    "compact_tool_content",
    "guarded_tool_call",
    "loop_detected",
    "tool_records",
]
