"""Agent graph package with lazy exports to avoid tool-registry import cycles."""

import importlib
from typing import Any

__all__ = ["AgentLoop", "main_agent", "run_agent", "run_agent_stream"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = importlib.import_module("app.agent.main_agent")
        return getattr(module, name)
    raise AttributeError(name)
