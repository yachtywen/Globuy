"""Unified chat-model construction.

Keeping provider creation here prevents API and graph code from depending on a
specific vendor. More providers can be added without changing the AgentLoop.
"""

from functools import lru_cache
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings


def _is_deepseek_endpoint(base_url: str | None) -> bool:
    """Return whether the OpenAI-compatible endpoint is DeepSeek's official API."""

    hostname = (urlparse(base_url or "").hostname or "").lower()
    return hostname == "api.deepseek.com" or hostname.endswith(".api.deepseek.com")


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
        "temperature": settings.llm_temperature,
        # Streaming responses must include the final usage chunk so LangFuse can
        # aggregate prompt/completion tokens for every generation.
        "stream_usage": True,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    if _is_deepseek_endpoint(settings.llm_base_url):
        # DeepSeek thinking mode requires every historical assistant message to
        # round-trip its vendor-specific reasoning_content. LangChain tool-call
        # messages do not guarantee that field survives the graph state, so a
        # later Think/Reflect turn can otherwise fail with HTTP 400.
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel | None:
    """Return the process-wide model shared by parent and forked loops."""

    return build_chat_model(get_settings())
