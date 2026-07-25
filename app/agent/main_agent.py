"""Explicit AgentLoop phases, homogeneous forks, compression, and streaming."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal, Self, TypedDict
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore

from app.agent.dispatch_tool import (
    build_dispatch_node,
    build_dispatch_tool,
    fork_dispatch_allowed,
    get_core_tools,
)
from app.agent.llm import get_chat_model
from app.agent.middleware import cache_breakpoint_update, loop_detected, tool_records
from app.agent.system_prompt import build_system_prompt
from app.api.monitor import EventType, current_monitor
from app.config import get_settings
from app.tools import TERMINAL_TOOLS, TOOL_PHASES
from app.utils.thread_ctx import (
    current_fork_depth,
    current_thread_id,
    current_user_id,
    fork_scope,
)

type Phase = Literal["think", "act", "observe", "reflect", "done"]

_BUDGET_PREFERENCE = re.compile(
    r"预算\s*(?:为|是|在)?\s*[￥¥]?\s*(\d+(?:\.\d+)?)\s*(?:元|块|RMB|rmb)?\s*(?:以内|以下|不超过|最多|上限)?"
)
_MEMORY_TRIGGER_WORDS = (
    "预算",
    "不超过",
    "以内",
    "以下",
    "最多",
    "上限",
    "喜欢",
    "偏好",
    "钟意",
    "更喜欢",
    "优先",
    "更看重",
    "在意",
    "不喜欢",
    "讨厌",
    "拒绝",
    "不要",
    "排除",
    "避开",
    "不能接受",
    "必须",
    "只要",
    "习惯",
    "常用",
    "常穿",
    "过敏",
    "敏感",
    "尺码",
    "身高",
    "体重",
    "肤质",
    "肤色",
)


def _detected_preference_candidates(content: str) -> list[dict[str, Any]]:
    """Extract explicit reusable preferences without writing any user data."""

    candidates: list[dict[str, Any]] = []
    match = _BUDGET_PREFERENCE.search(content)
    if match is not None:
        budget = float(match.group(1))
        if 0 < budget <= 1_000_000:
            candidates.append(
                {
                    "key": "budget_max_cny",
                    "category": "preference",
                    "content": f"购物预算不超过 {budget:g} 元",
                    "confidence": 1.0,
                }
            )
    normalized = " ".join(content.split()).strip()
    triggered = [word for word in _MEMORY_TRIGGER_WORDS if word in normalized]
    if triggered:
        is_blacklist = any(
            word in normalized
            for word in ("不喜欢", "讨厌", "拒绝", "不要", "排除", "避开", "不能接受")
        )
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        candidates.append(
            {
                "key": f"explicit_preference_{digest}",
                "category": "blacklist" if is_blacklist else "preference",
                "content": normalized[:500],
                "confidence": 1.0,
            }
        )
    return candidates


def _merge_preference_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    keys: set[str] = set()
    for group in groups:
        for candidate in group:
            if not isinstance(candidate, dict):
                continue
            key = str(candidate.get("key") or "").strip()
            if key and key not in keys:
                merged.append(candidate)
                keys.add(key)
    return merged


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    phase: Phase
    iteration: int
    decision_phase: Literal["think", "reflect"]
    tool_history: list[dict[str, str]]
    last_observation_digest: str | None
    terminal_result: dict[str, Any] | None
    original_query: str
    learned_preferences: list[dict[str, Any]]
    loop_detected: bool
    memory_context: str | None
    memory_status: str
    recalled_memories: list[dict[str, str]]


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


_CANDIDATE_FACT_FIELDS = (
    "product_id",
    "offer_id",
    "platform",
    "title",
    "price",
    "currency",
    "rating",
    "sales",
    "image_url",
    "attributes",
    "product_url",
    "retrieval_rank",
    "source_kind",
    "data_as_of",
    "wishlist_eligible",
)


def _collect_candidate_evidence(value: Any, evidence: dict[str, dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_candidate_evidence(item, evidence)
        return
    if not isinstance(value, dict):
        return
    item_id = value.get("item_id")
    if isinstance(item_id, str) and item_id.strip():
        evidence[item_id.strip()] = value
    for child in value.values():
        if isinstance(child, (dict, list)):
            _collect_candidate_evidence(child, evidence)


def _hydrate_picker_picks(picks: list[Any], messages: Sequence[BaseMessage]) -> list[Any]:
    """Restore verified candidate facts that the model omitted from ItemPicker args."""

    evidence: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {
            "item_search",
            "dispatch_tool",
        }:
            continue
        payload = _tool_payload(message)
        if payload is not None:
            _collect_candidate_evidence(payload, evidence)

    hydrated: list[Any] = []
    for raw in picks:
        if not isinstance(raw, dict):
            hydrated.append(raw)
            continue
        item_id = raw.get("item_id")
        source = evidence.get(str(item_id).strip()) if item_id is not None else None
        if source is None:
            hydrated.append(raw)
            continue
        item = dict(raw)
        for field in _CANDIDATE_FACT_FIELDS:
            if field in source:
                item[field] = source[field]
        hydrated.append(item)
    return hydrated


def _platform_outcomes(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Summarize the latest truthful ItemSearch outcome for each platform."""

    outcomes: dict[str, dict[str, Any]] = {}

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return
        platform = value.get("platform")
        status = value.get("status")
        candidates = value.get("candidates")
        if platform in {"taobao", "jingdong", "douyin"} and status in {
            "ok",
            "not_configured",
            "error",
        }:
            outcome: dict[str, Any] = {
                "platform": platform,
                "status": status,
                "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            }
            for field in ("source_kind", "cache_hit", "message"):
                if field in value:
                    outcome[field] = value[field]
            outcomes[platform] = outcome
        for child in value.values():
            if isinstance(child, (dict, list)):
                collect(child)

    for message in messages:
        if isinstance(message, ToolMessage) and message.name in {"item_search", "dispatch_tool"}:
            payload = _tool_payload(message)
            if payload is not None:
                collect(payload)
    return [outcomes[name] for name in ("taobao", "jingdong", "douyin") if name in outcomes]


def _last_user_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _tool_payload(message: ToolMessage) -> dict[str, Any] | None:
    if not isinstance(message.content, str):
        return None
    try:
        payload = json.loads(message.content)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_phase_tool_calls(
    response: AIMessage, allowed_tools: frozenset[str]
) -> tuple[AIMessage, bool]:
    """Remove hallucinated cross-phase calls before they enter message history."""

    calls = list(response.tool_calls)
    valid = [call for call in calls if str(call.get("name")) in allowed_tools]
    had_invalid = len(valid) != len(calls)
    if not had_invalid:
        return response, False
    return response.model_copy(update={"tool_calls": valid}), True


async def _report_phase(phase: Phase, *, started: bool, iteration: int) -> None:
    monitor = current_monitor()
    if monitor is None:
        return
    await monitor.emit(
        EventType.STEP_STARTED if started else EventType.STEP_FINISHED,
        phase=phase,
        iteration=iteration,
    )


class AgentLoop:
    """A phase-explicit stateful tool loop with homogeneous fork support."""

    def __init__(
        self,
        model: BaseChatModel | None = None,
        *,
        tools: Sequence[Any] | None = None,
        system_prompt: str | None = None,
        checkpointer=None,
        store: BaseStore | None = None,
        enable_dispatch: bool = True,
    ) -> None:
        self.model = model
        # Reads confirmed user memories before runs; Agent-originated writes stay disabled.
        self.store = store
        self.business_tools = list(tools if tools is not None else get_core_tools(model=self.model))
        self.enable_dispatch = enable_dispatch
        self.tools = list(self.business_tools)
        if enable_dispatch:
            self.tools.append(build_dispatch_tool(self))
        self.system_prompt = system_prompt or build_system_prompt(self.tools)
        self.checkpointer = checkpointer or InMemorySaver()
        self.children: dict[str, AgentLoop] = {}
        self.active_children: dict[str, AgentLoop] = {}
        self._by_name = {registered.name: registered for registered in self.tools}
        self.think_tools = [
            registered for registered in self.tools if registered.name in TOOL_PHASES["think"]
        ]
        self.reflect_tools = [
            registered for registered in self.tools if registered.name in TOOL_PHASES["reflect"]
        ]
        self.graph = self._build_graph()

    def _build_graph(self):
        think_model = (
            self.model.bind_tools(self.think_tools)
            if self.model is not None and self.think_tools
            else self.model
        )
        reflect_model = (
            self.model.bind_tools(self.reflect_tools)
            if self.model is not None and self.reflect_tools
            else self.model
        )

        async def think(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            iteration = state.get("iteration", 0) + 1
            await _report_phase("think", started=True, iteration=iteration)
            if think_model is None:
                user_text = _last_user_text(state["messages"])
                response = AIMessage(
                    content=f"[mock] globuy 已收到：{user_text}\n\nAgentLoop 阶段图工作正常。"
                )
                await _report_phase("think", started=False, iteration=iteration)
                return {
                    "messages": [response],
                    "phase": "done",
                    "decision_phase": "think",
                    "iteration": iteration,
                }
            phase_prompt = SystemMessage(
                content=(
                    "当前为 Think 阶段。只允许选择："
                    + "、".join(sorted(TOOL_PHASES["think"]))
                    + "。需要筛选、比价或收尾时不要直接回答，结束本阶段让 Reflect 处理。"
                )
            )
            prompt = self.system_prompt
            if state.get("memory_context"):
                prompt += "\n\n用户已确认的长期记忆：\n" + str(state["memory_context"])
            response = await think_model.ainvoke(
                [SystemMessage(content=prompt), phase_prompt, *state["messages"]],
                config=config,
            )
            response, _ = _normalize_phase_tool_calls(response, TOOL_PHASES["think"])
            await _report_phase("think", started=False, iteration=iteration)
            return {
                "messages": [response],
                "phase": "act" if response.tool_calls else "reflect",
                "decision_phase": "think",
                "iteration": iteration,
            }

        async def reflect(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            iteration = state.get("iteration", 0) + 1
            await _report_phase("reflect", started=True, iteration=iteration)
            if reflect_model is None:
                await _report_phase("reflect", started=False, iteration=iteration)
                return {"phase": "done", "iteration": iteration}
            guard = ""
            if state.get("loop_detected"):
                guard = (
                    "循环防护已触发：不要重复此前相同工具和参数。"
                    "已有精选商品时调用 shopping_summary，否则调用 chat_fallback。"
                )
            phase_prompt = SystemMessage(
                content=(
                    "当前为 Reflect 阶段。检查现有观察，只允许选择："
                    + "、".join(sorted(TOOL_PHASES["reflect"]))
                    + "。信息不足且需要继续检索时不调用工具，系统会返回 Think。"
                    "shopping_summary 成功后必须结束。" + guard
                )
            )
            prompt = self.system_prompt
            if state.get("memory_context"):
                prompt += "\n\n用户已确认的长期记忆：\n" + str(state["memory_context"])
            response = await reflect_model.ainvoke(
                [SystemMessage(content=prompt), phase_prompt, *state["messages"]],
                config=config,
            )
            response, requested_think_tool = _normalize_phase_tool_calls(
                response, TOOL_PHASES["reflect"]
            )
            await _report_phase("reflect", started=False, iteration=iteration)
            if response.tool_calls:
                next_phase: Phase = "act"
            elif requested_think_tool:
                next_phase = "think"
            elif current_fork_depth() > 0:
                # A homogeneous fork returns its compact findings to the parent. It must not
                # require a parent-only terminal shopping summary in order to stop.
                next_phase = "done"
            elif state.get("loop_detected"):
                next_phase = "done"
            else:
                next_phase = "think"
            return {
                "messages": [response],
                "phase": next_phase,
                "decision_phase": "reflect",
                "iteration": iteration,
            }

        async def observe(state: AgentState) -> dict[str, Any]:
            iteration = state.get("iteration", 0)
            await _report_phase("observe", started=True, iteration=iteration)
            history = tool_records(state["messages"])
            recent = history[-get_settings().loop_detection_window :]
            detected = loop_detected(recent)
            terminal_result: dict[str, Any] | None = None
            terminal_name: str | None = None
            for message in reversed(state["messages"]):
                if not isinstance(message, ToolMessage):
                    break
                if message.name not in TERMINAL_TOOLS:
                    continue
                payload = _tool_payload(message)
                if (
                    message.name == "chat_fallback"
                    and payload
                    and payload.get("status") == "needs_clarification"
                ):
                    terminal_result, terminal_name = payload, message.name
                    break
                if payload and payload.get("terminal") is True:
                    terminal_result, terminal_name = payload, message.name
                    break
            # ItemPicker is deterministic and already contains verified product
            # facts. Do not let an indecisive model re-enter Think/Reflect after
            # valid picks exist; it otherwise consumes the graph recursion budget.
            if terminal_result is None:
                for message in reversed(state["messages"]):
                    if not isinstance(message, ToolMessage) or message.name != "item_picker":
                        continue
                    payload = _tool_payload(message)
                    picks = payload.get("picks") if payload else None
                    if isinstance(picks, list) and picks:
                        picks = _hydrate_picker_picks(picks, state["messages"])
                        terminal_result = {
                            "status": "complete",
                            "terminal": True,
                            "final_text": "已根据你的条件筛选出可比较的商品。",
                            "picks": picks,
                            "unresolved": [],
                            "learned_preferences": state.get("learned_preferences", []),
                        }
                        terminal_name = "item_picker"
                        break
            if terminal_result and terminal_name == "shopping_summary":
                terminal_result = self._add_memory_reasons(
                    terminal_result, state.get("recalled_memories", [])
                )
            if terminal_result is not None:
                terminal_result = dict(terminal_result)
                terminal_result["platform_outcomes"] = _platform_outcomes(state["messages"])
            phase: Phase = "done" if terminal_result is not None else "reflect"
            await _report_phase("observe", started=False, iteration=iteration)
            return {
                "phase": phase,
                "tool_history": recent,
                "last_observation_digest": recent[-1]["result_digest"] if recent else None,
                "terminal_result": terminal_result,
                "learned_preferences": _merge_preference_candidates(
                    state.get("learned_preferences", []),
                    terminal_result.get("learned_preferences", [])
                    if terminal_name == "shopping_summary" and terminal_result
                    else [],
                ),
                "loop_detected": detected,
            }

        async def compress(state: AgentState) -> dict[str, Any]:
            update = cache_breakpoint_update(state["messages"])
            return {"messages": update} if update is not None else {}

        def route_decision(state: AgentState) -> Literal["act", "reflect", "think", "__end__"]:
            phase = state.get("phase", "think")
            return END if phase == "done" else phase

        builder = StateGraph(AgentState)
        builder.add_node("think", think)
        builder.add_node("act", build_dispatch_node(self.tools))
        builder.add_node("observe", observe)
        builder.add_node("compress", compress)
        builder.add_node("reflect", reflect)
        builder.add_edge(START, "think")
        builder.add_conditional_edges("think", route_decision)
        builder.add_edge("act", "observe")
        builder.add_edge("observe", "compress")
        builder.add_conditional_edges("compress", route_decision)
        builder.add_conditional_edges("reflect", route_decision)
        return builder.compile(checkpointer=self.checkpointer, store=self.store)

    def fork(self) -> Self:
        """Create a homogeneous child with the same model, business tools, and prompt."""

        return type(self)(
            self.model,
            tools=self.business_tools,
            system_prompt=self.system_prompt,
            store=self.store,
            enable_dispatch=self.enable_dispatch,
        )

    def expert(
        self,
        *,
        tool_names: Sequence[str],
        extra_instructions: str | None = None,
    ) -> Self:
        """Create a heterogeneous expert with an explicit tool/prompt subset."""

        tools = get_core_tools(tool_names, model=self.model)
        prompt = build_system_prompt(tools, extra_instructions=extra_instructions)
        return type(self)(
            self.model,
            tools=tools,
            system_prompt=prompt,
            store=self.store,
            enable_dispatch=False,
        )

    def _initial_state(self, content: str) -> AgentState:
        return {
            "messages": [HumanMessage(content=content)],
            "phase": "think",
            "iteration": 0,
            "decision_phase": "think",
            "tool_history": [],
            "last_observation_digest": None,
            "terminal_result": None,
            "original_query": content,
            "learned_preferences": _detected_preference_candidates(content),
            "loop_detected": False,
            "memory_context": None,
            "memory_status": "not_configured" if self.store is None else "ready",
            "recalled_memories": [],
        }

    async def _state_with_memory(self, content: str) -> AgentState:
        state = self._initial_state(content)
        user_id = current_user_id()
        if self.store is None or not user_id:
            return state
        try:
            memories = await self.store.asearch(
                ("users", user_id, "memories"), query=content, limit=10
            )
            lines = []
            recalled: list[dict[str, str]] = []
            for memory in memories:
                category = str(memory.value.get("category") or "preference")
                skill_name = str(memory.value.get("skill_name") or "通用偏好")
                content_value = str(memory.value.get("content") or "").strip()
                if content_value:
                    lines.append(f"- [{skill_name}/{category}] {memory.key}: {content_value}")
                    recalled.append(
                        {"key": memory.key, "skill_name": skill_name, "content": content_value}
                    )
            state["memory_context"] = "\n".join(lines) or None
            state["recalled_memories"] = recalled
            state["memory_status"] = "ready"
        except Exception:
            state["memory_status"] = "partial"
        return state

    @staticmethod
    def _add_memory_reasons(
        result: dict[str, Any], memories: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Expose confirmed preference influence without claiming unsupported item matches."""

        if not memories or not isinstance(result.get("picks"), list):
            return result
        enriched = dict(result)
        applied = [f"已纳入 {item['skill_name']} 偏好：{item['content']}" for item in memories[:2]]
        picks: list[dict[str, Any]] = []
        for raw in result["picks"]:
            if not isinstance(raw, dict):
                picks.append(raw)
                continue
            item = dict(raw)
            reasons = [str(value) for value in item.get("reasons", []) if isinstance(value, str)]
            item["reasons"] = [*reasons, *applied][:4]
            picks.append(item)
        enriched["picks"] = picks
        enriched["recalled_memory_keys"] = [item["key"] for item in memories]
        return enriched

    def _config(self, thread_id: str, *, child: bool = False) -> RunnableConfig:
        settings = get_settings()
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": (
                settings.fork_recursion_limit if child else settings.main_agent_recursion_limit
            ),
            "metadata": {"model_role": "coordinator"},
        }

    async def _invoke(self, content: str, thread_id: str, *, child: bool = False) -> AgentState:
        settings = get_settings()
        timeout = settings.fork_timeout_seconds if child else settings.main_agent_timeout_seconds
        async with asyncio.timeout(timeout):
            return await self.graph.ainvoke(
                await self._state_with_memory(content),
                config=self._config(thread_id, child=child),
            )

    async def astream(
        self, content: str, thread_id: str, *, child: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield LangGraph v2 events, including nested ShoppingSummary model events."""

        settings = get_settings()
        timeout = settings.fork_timeout_seconds if child else settings.main_agent_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                async for graph_event in self.graph.astream_events(
                    await self._state_with_memory(content),
                    config=self._config(thread_id, child=child),
                    version="v2",
                ):
                    yield graph_event
        except GraphRecursionError:
            fallback = {
                "messages": [
                    AIMessage(content="检索步骤已达到本次运行上限，请缩小条件后重试。")
                ],
                "phase": "done",
                "iteration": settings.fork_recursion_limit
                if child
                else settings.main_agent_recursion_limit,
                "learned_preferences": _detected_preference_candidates(content),
                "terminal_result": {
                    "status": "incomplete",
                    "terminal": True,
                    "final_text": "检索步骤已达到本次运行上限，请缩小条件后重试。",
                    "picks": [],
                    "unresolved": ["本次检索未能在步骤上限内收敛"],
                    "platform_outcomes": [],
                },
            }
            yield {
                "event": "on_chain_end",
                "name": "globuy_recursion_fallback",
                "data": {"output": fallback},
                "parent_ids": [],
                "metadata": {"model_role": "coordinator"},
            }

    @staticmethod
    def _compact_tool_results(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name not in {
                "item_search",
                "category_insight",
            }:
                continue
            payload = _tool_payload(message)
            if payload is not None:
                results.append({"tool": message.name, "result": payload})
        return results

    async def dispatch(self, demand: str) -> dict[str, Any]:
        settings = get_settings()
        if not fork_dispatch_allowed(settings.fork_max_depth):
            return {
                "status": "depth_limit",
                "message": "dispatch_tool 只能在主线程调用，且最大 fork 深度为 1。",
                "tool_results": [],
                "search_results": [],
            }
        parent_thread_id = current_thread_id()
        child_thread_id = f"{parent_thread_id}-fork-{uuid4().hex[:12]}"
        child = self.fork()
        self.children[child_thread_id] = child
        self.active_children[child_thread_id] = child
        monitor = current_monitor()
        if monitor is not None:
            await monitor.report_fork(
                child_thread_id,
                reason=demand,
                tool_names=[registered.name for registered in child.business_tools],
            )
        try:
            with fork_scope(child_thread_id):
                state = await child._invoke(demand, child_thread_id, child=True)
            final_message = state["messages"][-1]
            tool_results = self._compact_tool_results(state["messages"])
            return {
                "status": "ok",
                "child_thread_id": child_thread_id,
                "answer": _message_text(final_message),
                "tool_results": tool_results,
                "search_results": [
                    item["result"] for item in tool_results if item["tool"] == "item_search"
                ],
            }
        except TimeoutError:
            return {
                "status": "timeout",
                "child_thread_id": child_thread_id,
                "message": f"子任务在 {settings.fork_timeout_seconds:g}s 内未完成。",
                "tool_results": [],
                "search_results": [],
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "status": "error",
                "child_thread_id": child_thread_id,
                "message": str(exc),
                "tool_results": [],
                "search_results": [],
            }
        finally:
            self.active_children.pop(child_thread_id, None)

    @staticmethod
    def _answer(state: AgentState) -> tuple[str, BaseMessage]:
        terminal = state.get("terminal_result")
        if terminal:
            text = str(terminal.get("final_text") or terminal.get("message") or "")
            return text, state["messages"][-1]
        message = state["messages"][-1]
        return _message_text(message), message

    async def run(self, content: str, thread_id: str) -> tuple[str, dict[str, Any]]:
        try:
            state = await self._invoke(content, thread_id)
        except TimeoutError:
            return "主任务执行超时。", {
                "status": "timeout",
                "memory_status": "not_configured" if self.store is None else "partial",
            }
        except GraphRecursionError:
            return "检索步骤已达到本次运行上限，请缩小条件后重试。", {
                "status": "incomplete",
                "phase": "done",
                "memory_status": "not_configured" if self.store is None else "partial",
                "learned_preferences": _detected_preference_candidates(content),
            }
        answer, message = self._answer(state)
        metadata = {
            "status": "ok",
            "message_id": message.id,
            "model": getattr(message, "response_metadata", {}).get("model_name"),
            "usage": getattr(message, "usage_metadata", None),
            "phase": state.get("phase"),
            "iteration": state.get("iteration", 0),
            "memory_status": state.get("memory_status", "not_configured"),
            "learned_preferences": state.get("learned_preferences", []),
        }
        return answer, metadata


main_agent = AgentLoop(get_chat_model())


async def run_agent(content: str, thread_id: str) -> tuple[str, dict[str, Any]]:
    """Compatibility collector for one run."""

    return await main_agent.run(content, thread_id)


async def run_agent_stream(content: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
    """Default incremental AgentLoop entrypoint."""

    async for graph_event in main_agent.astream(content, thread_id):
        yield graph_event


__all__ = ["AgentLoop", "AgentState", "main_agent", "run_agent", "run_agent_stream"]
