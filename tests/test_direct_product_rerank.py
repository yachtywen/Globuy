from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

import pytest

from app.products.grouping import cap_groups_balanced, group_candidates
from app.search.schemas import Candidate
from app.tools.item_picker import build_item_picker_tool

item_picker_module = importlib.import_module("app.tools.item_picker")


@pytest.fixture(autouse=True)
def disable_group_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.tools.item_picker._group_repository", lambda: None)


def candidate(
    item_id: str,
    platform: str,
    rank: int,
    *,
    price: float = 100,
    title: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Candidate:
    return Candidate(
        item_id=item_id,
        product_id=item_id,
        offer_id=f"{platform}:{item_id}",
        platform=platform,
        title=title or item_id,
        price=price,
        currency="CNY",
        attributes=attributes or {},
        product_url=f"https://example.com/{platform}/{item_id}",
        source_rank=rank,
    )


def test_conservative_grouping_requires_strong_identity_evidence() -> None:
    exact = {"brand": "Apple", "model": "A3102", "capacity": "256GB"}
    items = [
        candidate("same-id", "taobao", 1, attributes=exact),
        candidate("same-id", "jingdong", 1, price=95, attributes=exact),
        candidate(
            "other-capacity",
            "douyin",
            1,
            attributes={**exact, "capacity": "512GB"},
        ),
        candidate("opaque", "taobao", 2, title="Apple A3102 手机 256GB"),
        candidate("opaque", "jingdong", 2, title="Apple A3102 手机 256GB"),
    ]

    groups, summary = group_candidates(items)

    assert summary.input_offers == 5
    assert summary.product_groups == 4
    merged = next(group for group in groups if len(group.offers) == 2)
    assert merged.match_method == "brand_model_variant_exact"
    assert merged.representative.platform == "jingdong"
    assert len([group for group in groups if group.match_method == "singleton"]) == 2


def test_group_cap_round_robins_platforms_and_stops_at_36() -> None:
    groups, _ = group_candidates(
        [
            candidate(f"{platform}-{index}", platform, index + 1)
            for platform in ("taobao", "jingdong", "douyin")
            for index in range(15)
        ]
    )

    selected = cap_groups_balanced(groups, 36)

    assert len(selected) == 36
    assert [group.representative.platform for group in selected[:6]] == [
        "taobao",
        "jingdong",
        "douyin",
        "taobao",
        "jingdong",
        "douyin",
    ]


class RerankRunner:
    def __init__(self, owner: FakeRerankModel) -> None:
        self.owner = owner

    async def ainvoke(self, messages, config=None, **kwargs):
        self.owner.calls += 1
        if self.owner.mode == "timeout":
            await asyncio.sleep(0.1)
        payload = json.loads(messages[-1].content)
        group_ids = [item["product_group_id"] for item in payload["candidates"]]
        if self.owner.mode == "unknown_id":
            return {"ordered_group_ids": ["unknown-group"], "assessments": []}
        if self.owner.mode == "invalid_json":
            return "not-json"
        if self.owner.mode == "fact_tampering":
            return {
                "ordered_group_ids": group_ids,
                "assessments": [],
                "price": 0,
            }
        return {"ordered_group_ids": list(reversed(group_ids)), "assessments": []}


class FakeRerankModel:
    openai_api_base = None

    def __init__(self, *, mode: str = "valid") -> None:
        self.mode = mode
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        return RerankRunner(self)


@pytest.mark.asyncio
async def test_item_picker_calls_model_once_and_collapses_duplicate_slots() -> None:
    model = FakeRerankModel()
    tool = build_item_picker_tool(model)  # type: ignore[arg-type]
    exact = {"gtin": "6901234567890", "gtin_verified": True}
    items = [
        candidate("a", "taobao", 1, price=105, attributes=exact).model_dump(),
        candidate("b", "jingdong", 1, price=99, attributes=exact).model_dump(),
        candidate("c", "douyin", 1, price=120).model_dump(),
    ]

    result = await tool.ainvoke({"items": items, "limit": 3})

    assert model.calls == 1
    assert result["ranking_method"] == "llm"
    assert len(result["picks"]) == 2
    merged = next(item for item in result["picks"] if item["alternative_offers"])
    assert merged["platform"] == "jingdong"
    assert merged["alternative_offers"][0]["platform"] == "taobao"


@pytest.mark.asyncio
async def test_invalid_llm_ids_degrade_once_without_losing_candidates() -> None:
    model = FakeRerankModel(mode="unknown_id")
    tool = build_item_picker_tool(model)  # type: ignore[arg-type]
    items = [
        candidate("a", "taobao", 2).model_dump(),
        candidate("b", "jingdong", 1).model_dump(),
    ]

    result = await tool.ainvoke({"items": items, "limit": 2})

    assert model.calls == 1
    assert result["status"] == "degraded"
    assert result["ranking_method"] == "deterministic_fallback"
    assert result["fallback_reason"] == "valueerror"
    assert [item["item_id"] for item in result["picks"]] == ["b", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_json", "fact_tampering"])
async def test_invalid_llm_structure_degrades_without_retry(mode: str) -> None:
    model = FakeRerankModel(mode=mode)
    tool = build_item_picker_tool(model)  # type: ignore[arg-type]

    result = await tool.ainvoke({"items": [candidate("a", "taobao", 1).model_dump()]})

    assert model.calls == 1
    assert result["status"] == "degraded"
    assert result["ranking_method"] == "deterministic_fallback"


@pytest.mark.asyncio
async def test_llm_timeout_degrades_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeRerankModel(mode="timeout")
    settings = item_picker_module.get_settings().model_copy(
        update={"item_rerank_timeout_seconds": 0.001}
    )
    monkeypatch.setattr(item_picker_module, "get_settings", lambda: settings)
    tool = build_item_picker_tool(model)  # type: ignore[arg-type]

    result = await tool.ainvoke({"items": [candidate("a", "taobao", 1).model_dump()]})

    assert model.calls == 1
    assert result["status"] == "degraded"
    assert result["fallback_reason"] == "timeouterror"


@pytest.mark.asyncio
async def test_hard_filter_requires_url_and_reliable_required_attribute() -> None:
    tool = build_item_picker_tool(None)
    missing_url = candidate("a", "taobao", 1).model_dump()
    missing_url["product_url"] = None
    result = await tool.ainvoke(
        {
            "items": [missing_url, candidate("b", "jingdong", 1).model_dump()],
            "constraints": {"required_attributes": {"capacity": "256GB"}},
        }
    )

    assert result["status"] == "insufficient_data"
    assert len(result["rejected_brief"]) == 2
