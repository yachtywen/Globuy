"""Unified chat-model construction.

Keeping provider creation here prevents API and graph code from depending on a
specific vendor. More providers can be added without changing the AgentLoop.
"""

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings


def build_chat_model(settings: Settings | None = None) -> BaseChatModel | None:
    """Build one model instance shared by a parent loop and its forks."""
    settings = settings or get_settings()
    if settings.model_provider == "mock":
        return None

    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise RuntimeError("GLOBUY_MODEL_PROVIDER 非 mock 时必须设置 GLOBUY_LLM_API_KEY")

    kwargs: dict[str, object] = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "temperature": 0,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)
