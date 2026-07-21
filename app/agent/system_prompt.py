"""Compose the system prompt shared by parent and forked AgentLoops."""

from collections.abc import Iterable

from langchain_core.tools import BaseTool

from app.agent.prompts import get_system_prompt


def build_system_prompt(
    tools: Iterable[BaseTool] = (),
    memory_context: str | None = None,
    extra_instructions: str | None = None,
) -> str:
    sections = [get_system_prompt(memory_context).strip()]
    tool_names = [tool.name for tool in tools]
    if tool_names:
        sections.append("可用工具：" + "、".join(tool_names))
    if extra_instructions:
        sections.append(extra_instructions.strip())
    return "\n\n".join(section for section in sections if section)
