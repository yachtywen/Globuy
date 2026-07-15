"""AgentLoop assembly, execution entrypoint and homogeneous fork mechanism."""

from collections.abc import Sequence
from typing import Any, Self

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agent.dispatch_tool import build_dispatch_node, get_core_tools, route_after_assistant
from app.agent.llm import build_chat_model
from app.agent.system_prompt import build_system_prompt


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


def _last_user_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


class AgentLoop:
    """A stateful tool-calling loop that can fork with another prompt/tool set."""

    def __init__(
        self,
        model: BaseChatModel | None = None,
        *,
        tools=None,
        system_prompt: str | None = None,
        checkpointer=None,
    ) -> None:
        self.model = model
        self.tools = list(tools if tools is not None else get_core_tools())
        self.system_prompt = system_prompt or build_system_prompt(self.tools)
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = self._build_graph()

    def _build_graph(self):
        bound_model = self.model.bind_tools(self.tools) if self.model and self.tools else self.model

        async def call_assistant(state: MessagesState) -> dict[str, list[AIMessage]]:
            if bound_model is None:
                user_text = _last_user_text(state["messages"])
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                "[mock] globuy 已收到："
                                f"{user_text}\n\nAgentLoop 与实时事件链路工作正常。"
                            )
                        )
                    ]
                }

            response = await bound_model.ainvoke(
                [SystemMessage(content=self.system_prompt), *state["messages"]]
            )
            return {"messages": [response]}

        builder = StateGraph(MessagesState)
        builder.add_node("assistant", call_assistant)
        builder.add_edge(START, "assistant")
        if self.tools:
            builder.add_node("tools", build_dispatch_node(self.tools))
            builder.add_conditional_edges("assistant", route_after_assistant)
            builder.add_edge("tools", "assistant")
        else:
            builder.add_edge("assistant", END)
        return builder.compile(checkpointer=self.checkpointer)

    def fork(
        self,
        *,
        tool_names: Sequence[str] | None = None,
        extra_instructions: str | None = None,
    ) -> Self:
        tools = get_core_tools(tool_names) if tool_names is not None else self.tools
        prompt = build_system_prompt(tools, extra_instructions=extra_instructions)
        return type(self)(self.model, tools=tools, system_prompt=prompt)

    async def run(self, content: str, thread_id: str) -> tuple[str, dict[str, Any]]:
        result = await self.graph.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 24},
        )
        message = result["messages"][-1]
        metadata = {
            "message_id": message.id,
            "model": message.response_metadata.get("model_name"),
            "usage": message.usage_metadata,
        }
        return _message_text(message), metadata


main_agent = AgentLoop(build_chat_model())


async def run_agent(content: str, thread_id: str) -> tuple[str, dict[str, Any]]:
    """Run one turn while preserving in-process history by thread id."""

    return await main_agent.run(content, thread_id)
