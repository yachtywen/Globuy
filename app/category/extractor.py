"""Strict JSON extraction adapters for offline cards and online insights."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.category.schemas import CategoryCard, InsightExtractionPayload

CARD_PROMPT_VERSION = "category-card-extract-v1"
INSIGHT_PROMPT_VERSION = "category-insight-extract-v1"


class CategoryExtractionError(RuntimeError):
    """Raised when a configured model cannot produce valid constrained JSON."""


class CategoryExtractionNotConfigured(CategoryExtractionError):
    """Raised when the required small model is not configured."""


class CardExtractor(Protocol):
    name: str
    prompt_version: str

    async def extract_card(self, draft: CategoryCard) -> CategoryCard: ...


class InsightExtractor(Protocol):
    prompt_version: str

    async def extract_insight(
        self, query: str, depth: str, cards: Sequence[CategoryCard]
    ) -> InsightExtractionPayload: ...


class PassthroughCardExtractor:
    """No-LLM adapter used only by deterministic tests and local dry runs."""

    name = "deterministic-passthrough"
    prompt_version = "deterministic-card-v1"

    async def extract_card(self, draft: CategoryCard) -> CategoryCard:
        return draft.model_copy(deep=True)


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?%?", text))


class DeepSeekCategoryExtractor:
    """One configured OpenAI-compatible chat model for both extraction stages."""

    name = "deepseek-json"
    prompt_version = CARD_PROMPT_VERSION

    def __init__(self, model: BaseChatModel | None) -> None:
        self.model = model

    async def _invoke_json(self, system: str, payload: dict) -> dict:
        if self.model is None:
            raise CategoryExtractionNotConfigured("CategoryInsight 需要已配置的 DeepSeek 模型")
        runnable = self.model.bind(response_format={"type": "json_object"})
        response = await runnable.ainvoke(
            [
                SystemMessage(content=system),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ]
        )
        try:
            parsed = json.loads(_json_text(_message_text(response)))
        except (ValueError, json.JSONDecodeError) as exc:
            raise CategoryExtractionError(f"模型返回非法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CategoryExtractionError("模型 JSON 根节点不是对象")
        return parsed

    async def extract_card(self, draft: CategoryCard) -> CategoryCard:
        system = (
            "你是 globuy 品类知识卡片抽取器。只输出一个 JSON 对象，不要解释。"
            "必须原样保留 card_id/category/card_type/raw_evidence/last_updated/confidence；"
            "summary 只能压缩措辞，不能增加事实或改变任何数字。"
        )
        error = ""
        for _ in range(2):
            try:
                payload = await self._invoke_json(system + error, draft.model_dump(mode="json"))
                card = CategoryCard.model_validate(payload)
                fixed = draft.model_dump(exclude={"summary"}, mode="json")
                actual = card.model_dump(exclude={"summary"}, mode="json")
                if actual != fixed:
                    raise ValueError("模型改写了受保护字段")
                if not _numbers(draft.summary).issubset(_numbers(card.summary)):
                    raise ValueError("summary 丢失或改写了统计数字")
                return card
            except CategoryExtractionNotConfigured:
                raise
            except (ValidationError, ValueError, CategoryExtractionError) as exc:
                error = f"\n上次校验失败：{exc}。请严格修正。"
        raise CategoryExtractionError(error.strip())

    async def extract_insight(
        self, query: str, depth: str, cards: Sequence[CategoryCard]
    ) -> InsightExtractionPayload:
        system = (
            "你是 globuy CategoryInsight 的结构化提炼器。只能依据输入卡片，输出 JSON："
            "components、bestsellers、attributes、price_tiers。不得输出状态、置信度或证据原文。"
            "components 只有套装组成证据时才填写；quick 模式 attributes 必须为空。"
            "bestsellers 每项包含 name/typical_price_cny/why_popular/platform；"
            "attributes 每项包含 name/distribution，分布值为 0 到 1 且总和约等于 1；"
            "price_tiers 使用 budget/mid/premium 和 range_cny。只输出 JSON，不要解释。"
        )
        payload = {
            "query": query,
            "depth": depth,
            "cards": [card.model_dump(mode="json") for card in cards],
        }
        error = ""
        for _ in range(2):
            try:
                result = await self._invoke_json(system + error, payload)
                extracted = InsightExtractionPayload.model_validate(result)
                if depth == "quick" and extracted.attributes:
                    raise ValueError("quick 模式不能返回 attributes")
                return extracted
            except CategoryExtractionNotConfigured:
                raise
            except (ValidationError, ValueError, CategoryExtractionError) as exc:
                error = f"\n上次校验失败：{exc}。请严格修正。"
        raise CategoryExtractionError(error.strip())
