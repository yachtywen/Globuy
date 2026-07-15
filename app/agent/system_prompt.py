"""Compose the system prompt shared by parent and forked AgentLoops."""

from collections.abc import Iterable

from langchain_core.tools import BaseTool

from app.agent.prompts import MAIN_SYSTEM_PROMPT, get_prompt


def build_system_prompt(
    tools: Iterable[BaseTool] = (),
    memory_context: str | None = None,
    extra_instructions: str | None = None,
) -> str:
    sections = [get_prompt("system.base", MAIN_SYSTEM_PROMPT).strip()]
    tool_names = [tool.name for tool in tools]
    if tool_names:
        sections.append("可用工具：" + "、".join(tool_names))
    if memory_context:
        sections.append(get_prompt("system.memory_prefix", "用户长期偏好：") + memory_context)
    if extra_instructions:
        sections.append(extra_instructions.strip())
    return "\n\n".join(section for section in sections if section)
