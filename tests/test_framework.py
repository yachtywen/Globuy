import pytest
from langchain_core.messages import HumanMessage

from app.agent.main_agent import AgentLoop
from app.compress import compress_messages
from app.eval import build_rubric, judge_answer
from app.tools.item_picker import item_picker
from app.tools.planner import planner
from app.tools.price_compare import price_compare


def test_planner_exposes_an_ordered_tool_plan() -> None:
    result = planner.invoke(
        {
            "goal": "预算 1000 元购买降噪耳机",
            "shopping_intent": {
                "category_key": "headphones",
                "category_name": "耳机",
                "primary_query": "降噪耳机",
                "platforms": ["taobao", "jingdong", "douyin"],
                "filters": {"max_price": 1000, "currency": "CNY"},
            },
        }
    )
    assert result["status"] == "ok"
    assert result["steps"][0]["order"] == 1
    assert result["steps"][-1]["tool"] == "shopping_summary"


def test_planner_repairs_missing_category_fields_from_query() -> None:
    result = planner.invoke(
        {
            "goal": "推荐适合通勤的TWS无线降噪耳机，预算500元左右",
            "shopping_intent": {
                "platforms": ["taobao", "jingdong", "douyin"],
                "intent_mode": "category_explore",
                "primary_query": "TWS无线降噪耳机 通勤",
                "filters": {"min_price": 400, "max_price": 600, "currency": "CNY"},
                "intent_confidence": "high",
            },
        }
    )
    assert result["status"] == "ok"
    intent = result["shopping_intent"]
    assert intent["category_name"] == "TWS无线降噪耳机 通勤"
    assert intent["category_key"]
    assert result.get("repaired") is True
    assert result.get("repaired_fields") == ["category_name", "category_key"]


def test_planner_derives_primary_query_and_category_from_goal() -> None:
    result = planner.invoke(
        {
            "goal": "推荐适合通勤的TWS无线降噪耳机，预算500元左右",
            "shopping_intent": {
                "platforms": ["taobao"],
                "intent_mode": "category_explore",
            },
        }
    )
    assert result["status"] == "ok"
    intent = result["shopping_intent"]
    assert intent["primary_query"] == "推荐适合通勤的TWS无线降噪耳机，预算500元左右"
    assert intent["category_name"]
    assert intent["category_key"]


def test_planner_ignores_unknown_model_invented_keys() -> None:
    result = planner.invoke(
        {
            "goal": "500元左右的降噪耳机",
            "shopping_intent": {
                "category_key": "headphones",
                "category_name": "降噪耳机",
                "primary_query": "降噪耳机",
                "platforms": ["taobao"],
                "filters": {"max_price": 500, "brand": "sony"},
                "brand_hint": "sony",
            },
        }
    )
    assert result["status"] == "ok"
    assert "brand_hint" not in result["shopping_intent"]
    assert "brand" not in result["shopping_intent"]["filters"]


def test_planner_reconciles_goal_explore_without_clarification() -> None:
    result = planner.invoke(
        {
            "goal": "我想买点东西",
            "shopping_intent": {
                "platforms": ["taobao"],
                "intent_mode": "goal_explore",
                "needs_clarification": False,
            },
        }
    )
    assert result["status"] == "needs_clarification"
    intent = result["shopping_intent"]
    assert intent["needs_clarification"] is True
    assert intent["clarification_question"]
    assert result.get("repaired") is True


def test_planner_returns_invalid_intent_for_unfixable_exact_product() -> None:
    result = planner.invoke(
        {
            "goal": "买索尼 WH-1000XM5",
            "shopping_intent": {
                "category_key": "headphones",
                "category_name": "耳机",
                "primary_query": "WH-1000XM5",
                "platforms": ["jingdong"],
                "intent_mode": "exact_product",
            },
        }
    )
    assert result["status"] == "invalid_intent"
    assert result["shopping_intent"] is None


def test_planner_passes_through_complete_intent_without_repair() -> None:
    result = planner.invoke(
        {
            "goal": "预算 1000 元购买降噪耳机",
            "shopping_intent": {
                "category_key": "headphones",
                "category_name": "耳机",
                "primary_query": "降噪耳机",
                "platforms": ["taobao", "jingdong", "douyin"],
                "filters": {"max_price": 1000, "currency": "CNY"},
            },
        }
    )
    assert result["status"] == "ok"
    assert "repaired" not in result


def test_candidate_tools_rank_and_calculate_cost() -> None:
    candidates = [
        {
            "item_id": "a",
            "platform": "taobao",
            "title": "A",
            "retrieval_rank": 2,
            "rating": 4.6,
            "price": 900,
        },
        {
            "item_id": "b",
            "platform": "jingdong",
            "title": "B",
            "retrieval_rank": 1,
            "rating": 4.2,
            "price": 950,
        },
    ]
    picked = item_picker.invoke({"items": candidates, "limit": 1})
    assert picked["picks"][0]["title"] == "B"


def test_item_picker_ignores_provider_rich_fields_on_candidates() -> None:
    # The LLM echoes item_search candidates verbatim (product_id/offer_id/shop_name
    # etc.). PickerCandidate must ignore unknown fields instead of failing the call.
    candidates = [
        {
            "item_id": "taobao:993593054906",
            "product_id": "9e9a244da6b3028e8f83a505c0aff28b",
            "offer_id": "c090f7a8a5597f2b",
            "source_item_id": "993593054906",
            "platform": "taobao",
            "title": "竹林鸟锦瑟T50蓝牙ANC降噪耳机",
            "price": 99.0,
            "sales": 1000,
            "source_rank": 1,
            "shop_name": "竹林鸟声音伙伴专卖店",
            "rating_value": 4.8,
            "rating_scale": 5.0,
            "sales_value": 1000,
            "is_active": True,
            "last_seen_at": "2026-09-07T00:00:00",
        },
        {
            "item_id": "douyin:3840607746168848660",
            "product_id": "p2",
            "offer_id": "o2",
            "platform": "douyin",
            "title": "2026新款耳夹蓝牙耳机降噪",
            "price": 299.0,
            "sales": 200,
            "source_rank": 2,
            "shop_name": "吉芯客潮玩旗舰店",
        },
    ]
    result = item_picker.invoke({"items": candidates, "limit": 2})
    assert result["status"] == "ok"
    assert len(result["picks"]) == 2
    assert result["picks"][0]["platform"] == "taobao"

    compared = price_compare.invoke(
        {
            "items": [
                {"title": "A", "price": 900, "shipping_fee": 50},
                {"title": "B", "price": 920, "shipping_fee": 0},
            ]
        }
    )
    assert compared["best_offer"]["title"] == "B"


def test_compression_keeps_recent_messages() -> None:
    messages = [HumanMessage(content="很长的历史消息" * 20) for _ in range(6)]
    result, retained = compress_messages(messages, token_limit=10, keep_recent=2)
    assert result.compressed is True
    assert len(retained) == 2
    assert result.summary.startswith("历史上下文摘要")


def test_baseline_judge_uses_dynamic_rubric() -> None:
    rubric = build_rubric("选择一款耳机")
    result = judge_answer("根据工具来源，预算内建议 A；下单前重新核验。" * 10, rubric)
    assert result.total_score == 1.0


@pytest.mark.asyncio
async def test_agentloop_can_fork_without_real_model() -> None:
    parent = AgentLoop(model=None)
    child = parent.fork()
    answer, metadata = await child.run("帮我规划", "fork-test")
    assert "帮我规划" in answer
    assert [tool.name for tool in child.tools] == [tool.name for tool in parent.tools]
    assert child.business_tools == parent.business_tools
    assert child.system_prompt == parent.system_prompt
    assert child.model is parent.model
    assert child.checkpointer is not parent.checkpointer
    assert metadata["model"] is None


def test_agentloop_expert_is_separate_from_homogeneous_fork() -> None:
    parent = AgentLoop(model=None)
    expert = parent.expert(tool_names=["planner"], extra_instructions="只负责规划。")

    assert [tool.name for tool in expert.tools] == ["planner"]
    assert "只负责规划" in expert.system_prompt
    assert expert.enable_dispatch is False


@pytest.mark.asyncio
async def test_llm_item_picker_tolerates_provider_rich_fields_and_drops_bad_entries() -> None:
    """The LLM-facing picker must never crash on rich/malformed candidate input."""
    from app.tools.item_picker import build_item_picker_tool

    picker = build_item_picker_tool(None)
    exact_intent = {
        "intent_mode": "exact_product",
        "product_identity": {"model": "NOTHING-XYZ"},
        "category_key": "headphones",
        "category_name": "耳机",
        "primary_query": "WH-1000XM5",
        "platforms": ["taobao"],
    }
    result = await picker.ainvoke(
        {
            "items": [
                {
                    "item_id": "taobao:1",
                    "platform": "taobao",
                    "title": "降噪耳机 A",
                    "price": 99.0,
                    "currency": "CNY",
                    "product_url": "https://item.taobao.com/item.htm?id=1",
                    "product_id": "9e9a244d",
                    "offer_id": "c090f7a8",
                    "shop_name": "测试店",
                    "source_item_id": "1",
                    "is_active": True,
                },
                {
                    "item_id": "jingdong:2",
                    "platform": "jingdong",
                    "title": "降噪耳机 B",
                    "price": "299.0",  # numeric string must still coerce
                    "currency": "CNY",
                    "product_url": "https://item.jd.com/2.html",
                    "rating_value": 4.9,  # unknown field, must be ignored
                },
                {"item_id": "bad", "platform": "taobao", "title": "坏条目", "price": None},
                {"foo": "bar", "whatever": 1},
            ],
            "goal": "买索尼 WH-1000XM5",
            "shopping_intent": exact_intent,
            "constraints": {"min_price": "不合法", "blocked_item_ids": [123]},
        }
    )
    assert result["picks"] == []
    assert result["status"] in {"ok", "insufficient_data"}
    assert result.get("fallback_reason") in {None, "exact_identity_not_found"}


@pytest.mark.asyncio
async def test_llm_item_picker_empty_after_dropping_invalid_entries_is_honest() -> None:
    from app.tools.item_picker import build_item_picker_tool

    picker = build_item_picker_tool(None)
    result = await picker.ainvoke(
        {
            "items": [
                {"item_id": "x", "title": "无价格", "platform": "taobao"},
                {"foo": "bar"},
            ],
            "goal": "随便",
            "shopping_intent": {"garbage": True},  # unusable intent -> None
        }
    )
    assert result["status"] == "insufficient_data"
    assert result["picks"] == []
