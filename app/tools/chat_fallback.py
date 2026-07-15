"""Conversation fallback when no external tool is appropriate."""

from langchain_core.tools import tool


@tool
def chat_fallback(message: str) -> dict:
    """Handle clarification, casual chat, or unsupported requests safely."""

    return {
        "status": "needs_clarification",
        "message": message.strip(),
        "questions": ["预算范围是多少？", "收货国家或地区是哪里？", "最看重哪些指标？"],
    }
