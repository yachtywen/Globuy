"""Unified chat-model construction.

Keeping provider creation here prevents API and graph code from depending on a
specific vendor. More providers can be added without changing the AgentLoop.
"""

from functools import lru_cache
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings


def _is_deepseek_endpoint(base_url: str | None) -> bool:
    """Return whether the OpenAI-compatible endpoint is DeepSeek's official API."""

    hostname = (urlparse(base_url or "").hostname or "").lower()
    return hostname == "api.deepseek.com" or hostname.endswith(".api.deepseek.com")


def _is_kimi_endpoint(base_url: str | None) -> bool:
    """Return whether the endpoint is Moonshot's official Kimi API."""

    hostname = (urlparse(base_url or "").hostname or "").lower()
    return hostname == "api.moonshot.cn" or hostname.endswith(".api.moonshot.cn")


def model_request_kwargs(
    cache_key: str | None,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Return provider-specific per-request options without changing model identity."""

    settings = settings or get_settings()
    if (
        not cache_key
        or not isinstance(model, ChatOpenAI)
        or not _is_kimi_endpoint(settings.llm_base_url)
    ):
        return {}
    return {
        "extra_body": {
            "thinking": {"type": "disabled"},
            "prompt_cache_key": cache_key,
        }
    }


def build_chat_model(settings: Settings | None = None) -> BaseChatModel | None:
    """Build one model instance shared by a parent loop and its forks."""
    settings = settings or get_settings()
    if settings.model_provider == "mock":
        return None

    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise RuntimeError("GLOBUY_MODEL_PROVIDER 非 mock 时必须设置 GLOBUY_LLM_API_KEY")

    is_kimi = _is_kimi_endpoint(settings.llm_base_url)
    kwargs: dict[str, object] = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        # Streaming responses must include the final usage chunk so LangFuse can
        # aggregate prompt/completion tokens for every generation.
        "stream_usage": True,
    }
    if not is_kimi:
        kwargs["temperature"] = settings.llm_temperature
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    if _is_deepseek_endpoint(settings.llm_base_url) or is_kimi:
        # Both official DeepSeek and Kimi thinking modes require historical
        # reasoning_content to round-trip across multi-step tool calls. The graph
        # stores the portable LangChain message contract, so disable vendor-specific
        # thinking rather than risk a later Think/Reflect HTTP 400.
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    if is_kimi or _is_deepseek_endpoint(settings.llm_base_url):
        # Make the 32K output reserve explicit so the request and the
        # context-breakpoint calculation use the same contract.
        kwargs["max_tokens"] = settings.llm_max_output_tokens
    if settings.llm_requests_per_minute is not None:
        kwargs["rate_limiter"] = InMemoryRateLimiter(
            requests_per_second=settings.llm_requests_per_minute / 60,
            check_every_n_seconds=0.25,
            max_bucket_size=1,
        )
    kwargs["request_timeout"] = settings.llm_request_timeout_seconds
    kwargs["max_retries"] = settings.llm_max_retries
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel | None:
    """Return the process-wide model shared by parent and forked loops."""

    return build_chat_model(get_settings())
