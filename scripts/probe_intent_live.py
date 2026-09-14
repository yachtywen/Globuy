"""Minimal live probe: capture what intent_mode Kimi's Planner really produces.

Reproduces the failing conversation thread b31d04d6fbdc45e69f58cd6579a952ad
against the current AgentLoop code with the real kimi-k2.6 model, then prints
the exact ShoppingIntent the model generated in planner arguments, the tools
actually executed, and the terminal answer. Read-only regarding existing data;
item_search may hydrate the PostgreSQL catalog with real JustOne candidates
only if an executable intent finally triggers the deterministic auto-search.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from app.agent.main_agent import AgentLoop
from app.agent.llm import get_chat_model


def _usage_of(messages: list) -> dict | None:
    for message in reversed(messages):
        metadata = getattr(message, "response_metadata", None) or {}
        usage = metadata.get("usage") or getattr(message, "usage_metadata", None)
        if usage:
            return {
                "input_tokens": getattr(usage, "input_tokens", None)
                or usage.get("prompt_tokens"),
                "output_tokens": getattr(usage, "output_tokens", None)
                or usage.get("completion_tokens"),
            }
    return None


def _planner_intent(state: dict) -> dict | None:
    for message in state.get("messages", []):
        name = getattr(message, "name", None)
        if name != "planner":
            continue
        content = message.content
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except (ValueError, TypeError):
            continue
        intent = (payload or {}).get("shopping_intent")
        if isinstance(intent, dict):
            return intent
    return None


def _planner_raw(state: dict) -> str:
    """Dump the raw planner tool-call arguments and its ToolMessage content."""
    lines: list[str] = []
    messages = state.get("messages", [])
    for index, message in enumerate(messages):
        if getattr(message, "type", None) == "ai" and message.tool_calls:
            for call in message.tool_calls:
                if str(call.get("name")) == "planner":
                    lines.append(f"-- AIMessage 第{index}条 planner 参数 (前1200字):")
                    lines.append(json.dumps(call.get("args"), ensure_ascii=False)[:1200])
        elif (
            getattr(message, "type", None) == "tool"
            and getattr(message, "name", None) == "planner"
        ):
            content = message.content
            lines.append(f"-- ToolMessage 第{index}条 planner 结果 (前1200字):")
            lines.append(str(content)[:1200])
    return "\n".join(lines)


def _executed_tools(state: dict) -> list[str]:
    seen: list[str] = []
    for message in state.get("messages", []):
        if getattr(message, "type", None) == "tool":
            name = str(getattr(message, "name", "") or "")
            if name and (not seen or seen[-1] != name):
                seen.append(name)
    return seen


def _final_text(state: dict) -> str:
    result = state.get("terminal_result") or {}
    text = str(result.get("final_text") or result.get("message") or "")
    if text:
        return text
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) == "ai":
            content = message.content
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _intent_digest(intent: dict) -> dict:
    return {
        "intent_mode": intent.get("intent_mode"),
        "needs_clarification": intent.get("needs_clarification"),
        "clarification_count": intent.get("clarification_count"),
        "clarification_question": intent.get("clarification_question"),
        "category_key": intent.get("category_key"),
        "category_name": intent.get("category_name"),
        "platforms": intent.get("platforms"),
        "filters": intent.get("filters"),
        "required_attributes": intent.get("required_attributes"),
        "primary_query": intent.get("primary_query"),
        "lexical_query": intent.get("lexical_query"),
        "semantic_query": intent.get("semantic_query"),
        "hard_constraints": intent.get("hard_constraints"),
        "soft_preferences": intent.get("soft_preferences"),
    }


async def run_turn(loop: AgentLoop, thread_id: str, user_text: str, label: str) -> None:
    started = time.perf_counter()
    print(f"\n{'=' * 70}\n[{label}] 用户输入: {user_text!r}\n{'=' * 70}")
    try:
        state = await loop._invoke(user_text, thread_id)
    except Exception as exc:  # noqa: BLE001 - report and continue with other probes
        print(f"[{label}] run 异常: {type(exc).__name__}: {exc}")
        return
    if os.environ.get("PROBE_VERBOSE") == "1":
        for message in state.get("messages", []):
            kind = getattr(message, "type", "?")
            name = getattr(message, "name", "") or ""
            content = message.content
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            print(f"  msg[{kind}] name={name!r} content={text[:300]}")
    wall = time.perf_counter() - started
    intent = _planner_intent(state)
    print(f"[{label}] Planner ShoppingIntent (模型真实产出):")
    print("  " + json.dumps(_intent_digest(intent), ensure_ascii=False, indent=2).replace("\n", "\n  ")
          if intent else "  （未找到 planner 产出）")
    print(f"[{label}] 实际执行的工具: {_executed_tools(state)}")
    print(f"[{label}] phase={state.get('phase')} iteration={state.get('iteration')} "
          f"loop_detected={state.get('loop_detected')} 墙钟={wall:.1f}s")
    usage = _usage_of(state.get("messages", []))
    if usage:
        print(f"[{label}] token usage: {usage}")
    text = _final_text(state)
    print(f"[{label}] 终态回复(前260字): {text[:260]}")


async def probe_raw(loop: AgentLoop, user_text: str) -> dict:
    """Run one graph turn and return the final state (messages may accumulate)."""
    thread = f"probe-raw-{uuid.uuid4().hex[:10]}"
    try:
        return await loop._invoke(user_text, thread)
    except Exception as exc:  # noqa: BLE001
        print(f"[raw] run 异常: {type(exc).__name__}: {exc}")
        return {"messages": []}


async def run_turn_bound(
    loop: AgentLoop, user_text: str, label: str, thread_id: str | None = None
) -> None:
    """Run one turn with the same thread/run ContextVars the API binds,
    so JustOne catalog hydration can execute during item_search."""
    from pathlib import Path

    from app.api.context import bind_context

    if thread_id is None:
        thread_id = f"probe-bound-{uuid.uuid4().hex[:10]}"
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    session_dir = Path("/root/globuy/output/probe-sessions") / thread_id
    session_dir.mkdir(parents=True, exist_ok=True)
    with bind_context(thread_id, session_dir, run_id=run_id, user_id="probe-user"):
        started = time.perf_counter()
        print(f"\n{'=' * 70}\n[{label}] 用户输入: {user_text!r}\n{'=' * 70}")
        try:
            state = await loop._invoke(user_text, thread_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] run 异常: {type(exc).__name__}: {str(exc)[:500]}")
            try:
                snapshot = await loop.graph.aget_state(
                    {"configurable": {"thread_id": thread_id}}
                )
                for message in (snapshot.values.get("messages") or []):
                    kind = getattr(message, "type", "?")
                    name = getattr(message, "name", "") or ""
                    if kind == "ai" and message.tool_calls:
                        ids = [str(call.get("id"))[:8] for call in message.tool_calls]
                        print(f"  snapshot ai(tool_calls={ids}) content={str(message.content)[:120]!r}")
                    elif kind == "tool":
                        print(f"  snapshot tool name={name!r} id={str(getattr(message, 'tool_call_id', ''))[:8]}")
            except Exception as dump_exc:  # noqa: BLE001
                print(f"  (快照失败: {dump_exc})")
            return
        wall = time.perf_counter() - started
        for message in state.get("messages", []):
            kind = getattr(message, "type", "?")
            name = getattr(message, "name", "") or ""
            content = message.content
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if kind == "tool":
                if isinstance(content, str) and content.startswith("Error:"):
                    print(f"  msg[{kind}] name={name!r} content(尾部1500)={text[-1500:]}")
                elif name in {"item_picker", "shopping_summary"}:
                    print(f"  msg[{kind}] name={name!r} content(全文)={text}")
                elif name == "item_search" and isinstance(content, str) and content.startswith("{"):
                    try:
                        payload = json.loads(content)
                        cands = payload.get("candidates") or []
                        url_ok = sum(1 for c in cands if str(c.get("product_url") or "").startswith("http"))
                        keys = sorted(cands[0].keys()) if cands else []
                        print(f"  msg[{kind}] item_search platform={payload.get('platform')} "
                              f"candidates={len(cands)} has_url={url_ok} status={payload.get('status')} keys={keys}")
                    except (ValueError, TypeError):
                        print(f"  msg[{kind}] name={name!r} content={text[:360]}")
                else:
                    limit = 2400 if name in {"planner", "item_picker", "chat_fallback"} else 360
                    print(f"  msg[{kind}] name={name!r} content={text[:limit]}")
        intent = _planner_intent(state)
        if intent:
            digest = _intent_digest(intent)
            print(f"[{label}] Planner intent_mode={digest['intent_mode']} "
                  f"needs_clarification={digest['needs_clarification']} "
                  f"platforms={digest['platforms']}")
        print(f"[{label}] 实际执行的工具: {_executed_tools(state)}")
        print(f"[{label}] phase={state.get('phase')} iteration={state.get('iteration')} "
              f"loop_detected={state.get('loop_detected')} 墙钟={wall:.1f}s")
        print(f"[{label}] 终态回复(前260字): {_final_text(state)[:260]}")


async def main() -> None:
    print("使用模型: kimi-k2.6（真实调用，最小成本）")
    loop = AgentLoop(get_chat_model())

    if os.environ.get("PROBE_SINGLE") == "1":
        await run_turn_bound(
            loop,
            "推荐适合通勤的降噪耳机",
            "SINGLE-C1",
        )
        print("\n探测结束（single）。")
        return

    shared = f"probe-c-{uuid.uuid4().hex[:10]}"
    for index, text in enumerate(
        [
            "推荐适合通勤的降噪耳机",
            "我需要降噪耳机",
            "tws的",
            "五百元左右把",
        ],
        start=1,
    ):
        await run_turn_bound(loop, text, f"C-复现第{index}轮", thread_id=shared)

    print("\n探测结束。")


if __name__ == "__main__":
    asyncio.run(main())
