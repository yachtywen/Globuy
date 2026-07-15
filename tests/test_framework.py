from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.agent.main_agent import AgentLoop
from app.compress import compress_messages
from app.eval import build_rubric, judge_answer
from app.memory import PreferenceEntry, PreferenceStore, render_memory_context
from app.recall import rank_items
from app.tools.item_picker import item_picker
from app.tools.planner import planner
from app.tools.price_compare import price_compare
from app.tools.shipping_calc import shipping_calc


def test_planner_exposes_an_ordered_tool_plan() -> None:
    result = planner.invoke({"goal": "预算 1000 元购买降噪耳机"})
    assert result["status"] == "ok"
    assert result["steps"][0]["order"] == 1
    assert result["steps"][-1]["tool"] == "shopping_summary"


def test_candidate_tools_rank_and_calculate_cost() -> None:
    candidates = [
        {"title": "A", "score": 0.8, "rating": 4.6, "price": 900},
        {"title": "B", "score": 0.9, "rating": 4.2, "price": 950},
    ]
    picked = item_picker.invoke({"items": candidates, "limit": 1})
    assert picked["selected"][0]["title"] == "B"

    compared = price_compare.invoke(
        {
            "items": [
                {"title": "A", "price": 900, "shipping_fee": 50},
                {"title": "B", "price": 920, "shipping_fee": 0},
            ]
        }
    )
    assert compared["best_offer"]["title"] == "B"

    shipping = shipping_calc.invoke(
        {"item_price": 100, "quantity": 2, "shipping_fee": 20, "duty_rate": 0.1}
    )
    assert shipping["total"] == 240


def test_preference_store_and_prompt_injection(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "memory")
    entry = store.upsert(
        PreferenceEntry(
            user_id="user-1",
            key="budget",
            value="1000 CNY",
            confidence=0.9,
        )
    )
    assert store.list("user-1") == [entry]
    assert "budget: 1000 CNY" in render_memory_context(store.list("user-1"))


def test_compression_keeps_recent_messages() -> None:
    messages = [HumanMessage(content="很长的历史消息" * 20) for _ in range(6)]
    result, retained = compress_messages(messages, token_limit=10, keep_recent=2)
    assert result.compressed is True
    assert len(retained) == 2
    assert result.summary.startswith("历史上下文摘要")


def test_three_tower_recall_returns_scored_items() -> None:
    ranked = rank_items(
        {"category": "耳机", "preference": "降噪"},
        "降噪 耳机",
        [
            {"title": "旅行降噪耳机", "category": "耳机"},
            {"title": "办公键盘", "category": "键盘"},
        ],
    )
    assert len(ranked) == 2
    assert all("recall_score" in item for item in ranked)


def test_baseline_judge_uses_dynamic_rubric() -> None:
    rubric = build_rubric("选择一款耳机")
    result = judge_answer("根据工具来源，预算内建议 A；下单前重新核验。" * 10, rubric)
    assert result.total_score == 1.0


@pytest.mark.asyncio
async def test_agentloop_can_fork_without_real_model() -> None:
    parent = AgentLoop(model=None)
    child = parent.fork(tool_names=["planner"], extra_instructions="只负责规划。")
    answer, metadata = await child.run("帮我规划", "fork-test")
    assert "帮我规划" in answer
    assert len(child.tools) == 1
    assert metadata["model"] is None
