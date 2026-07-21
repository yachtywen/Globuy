"""LLM-backed terminal shopping manifest generation."""

from __future__ import annotations

import asyncio
import json
from typing import Literal
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.prompts import get_shopping_summary_prompt
from app.config import get_settings
from app.presentation import sanitize_shopping_markdown, visible_unresolved
from app.search.catalog_images import enrich_product_images
from app.tools.item_picker import PickedItem
from app.utils.thread_ctx import current_thread_id


class PreferenceCandidate(BaseModel):
    """A current-run preference awaiting a future BaseStore adapter."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=120)
    category: Literal["blacklist", "preference", "history"]
    content: str = Field(min_length=1, max_length=500)
    source_session: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)


class SummaryNarrative(BaseModel):
    """The only fields the nested LLM is allowed to generate."""

    model_config = ConfigDict(extra="forbid")

    final_text: str = Field(min_length=1, max_length=12_000)


class ShoppingSummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "incomplete", "not_configured", "error"]
    final_text: str = ""
    picks: list[PickedItem] = Field(default_factory=list, max_length=3)
    unresolved: list[str] = Field(default_factory=list)
    learned_preferences: list[PreferenceCandidate] = Field(default_factory=list)
    terminal: bool = False
    message: str | None = None


def _model_config(config: RunnableConfig | None) -> RunnableConfig:
    inherited: RunnableConfig = dict(config or {})
    inherited["tags"] = [*inherited.get("tags", []), "shopping_summary"]
    inherited["metadata"] = {
        **inherited.get("metadata", {}),
        "model_role": "shopping_summary",
    }
    return inherited


def _structured_summary_model(model: BaseChatModel) -> BaseChatModel:
    """Use DeepSeek V4 function calling in non-thinking mode for forced schemas."""

    base_url = str(getattr(model, "openai_api_base", "") or "")
    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname == "api.deepseek.com" or hostname.endswith(".api.deepseek.com"):
        extra_body = dict(getattr(model, "extra_body", None) or {})
        extra_body["thinking"] = {"type": "disabled"}
        return model.model_copy(update={"extra_body": extra_body})
    return model


def build_shopping_summary_tool(model: BaseChatModel | None) -> BaseTool:
    """Bind the terminal tool to the same model instance used by the AgentLoop."""

    @tool("shopping_summary")
    async def shopping_summary(
        goal: str,
        picks: list[PickedItem],
        config: RunnableConfig,
        unresolved: list[str] | None = None,
        learned_preferences: list[PreferenceCandidate] | None = None,
    ) -> dict:
        """Generate the final Markdown from validated facts with exactly one LLM call."""

        settings = get_settings()
        catalog_path = getattr(settings, "product_image_catalog_path", None)
        raw_picks = [
            item.model_dump(mode="json") if isinstance(item, PickedItem) else item
            for item in picks[:3]
        ]
        validated_picks = [
            item if isinstance(item, PickedItem) else PickedItem.model_validate(item)
            for item in enrich_product_images(raw_picks, catalog_path)
        ]
        pending = [
            item
            if isinstance(item, PreferenceCandidate)
            else PreferenceCandidate.model_validate(item)
            for item in (learned_preferences or [])
        ]
        session = current_thread_id()
        pending = [
            item.model_copy(update={"source_session": item.source_session or session})
            for item in pending
        ]
        visible_pending = visible_unresolved(unresolved)

        if not validated_picks:
            return ShoppingSummaryOutput(
                status="incomplete",
                picks=[],
                unresolved=visible_pending,
                learned_preferences=pending,
                message="至少需要一项有效的 ItemPicker 结果才能生成终结清单。",
            ).model_dump(mode="json")
        if any(not item.product_url for item in validated_picks):
            return ShoppingSummaryOutput(
                status="incomplete",
                picks=validated_picks,
                unresolved=visible_pending,
                learned_preferences=pending,
                message="精选商品缺少来源链接，不能生成可核验的终结清单。",
            ).model_dump(mode="json")
        if model is None:
            return ShoppingSummaryOutput(
                status="not_configured",
                picks=validated_picks,
                unresolved=visible_pending,
                learned_preferences=pending,
                message="ShoppingSummary 的共享模型未配置。",
            ).model_dump(mode="json")

        presentation_picks = [
            {
                key: value
                for key, value in item.model_dump(mode="json").items()
                if key not in {"shipping_fee", "total_cost"}
            }
            for item in validated_picks
        ]
        facts = {
            "goal": goal.strip(),
            "picks": presentation_picks,
            "unresolved": visible_pending,
            "rules": [
                "只整理输入事实，不新增或改写商品、价格、库存、链接和偏好",
                "当前数据源不提供运费；最终回答完全省略运费、邮费、包邮及其待确认提示",
                "不要增加统一的数据说明、快照徽标、快照免责声明或下单提示段落",
            ],
        }
        # DeepSeek's OpenAI-compatible endpoint supports tool calling but may reject
        # LangChain's default response_format=json_schema mode.
        structured_model = _structured_summary_model(model).with_structured_output(
            SummaryNarrative,
            method="function_calling",
        )
        try:
            async with asyncio.timeout(settings.summary_timeout_seconds):
                response = await structured_model.ainvoke(
                    [
                        SystemMessage(content=get_shopping_summary_prompt()),
                        HumanMessage(
                            content=json.dumps(facts, ensure_ascii=False, sort_keys=True)
                        ),
                    ],
                    config=_model_config(config),
                )
            narrative = (
                response
                if isinstance(response, SummaryNarrative)
                else SummaryNarrative.model_validate(response)
            )
        except TimeoutError:
            return ShoppingSummaryOutput(
                status="error",
                picks=validated_picks,
                unresolved=visible_pending,
                learned_preferences=pending,
                message="ShoppingSummary 模型调用超时。",
            ).model_dump(mode="json")
        except (ValidationError, ValueError, TypeError) as exc:
            return ShoppingSummaryOutput(
                status="error",
                picks=validated_picks,
                unresolved=visible_pending,
                learned_preferences=pending,
                message=f"ShoppingSummary 结构化输出无效：{exc}",
            ).model_dump(mode="json")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ShoppingSummaryOutput(
                status="error",
                picks=validated_picks,
                unresolved=visible_pending,
                learned_preferences=pending,
                message=f"ShoppingSummary 模型调用失败：{exc}",
            ).model_dump(mode="json")

        final_text = sanitize_shopping_markdown(narrative.final_text) or (
            "## 精选商品\n\n已整理出可核验的商品清单，请查看下方商品卡片。"
        )
        return ShoppingSummaryOutput(
            status="complete",
            final_text=final_text,
            picks=validated_picks,
            unresolved=visible_pending,
            learned_preferences=pending,
            terminal=True,
        ).model_dump(mode="json")

    return shopping_summary


# Import compatibility only. Runtime AgentLoops use ``build_shopping_summary_tool(model)``.
shopping_summary = build_shopping_summary_tool(None)


__all__ = [
    "PreferenceCandidate",
    "ShoppingSummaryOutput",
    "SummaryNarrative",
    "build_shopping_summary_tool",
    "shopping_summary",
]
