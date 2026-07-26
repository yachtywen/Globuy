import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from app.agent.main_agent import (
    AgentLoop,
    _detected_preference_candidates,
    _hydrate_picker_picks,
    _merge_preference_candidates,
    _normalize_phase_tool_calls,
    _platform_outcomes,
)
from app.agent.middleware import (
    cache_breakpoint_update,
    compact_tool_content,
    loop_detected,
    tool_records,
)
from app.config import Settings
from app.tools import CORE_TOOL_NAMES, TERMINAL_TOOLS, TOOL_PHASES, build_core_tools
from app.tools.item_picker import item_picker
from app.tools.shopping_summary import SummaryNarrative, build_shopping_summary_tool
from app.utils.thread_ctx import thread_scope


def test_fork_depth_configuration_is_fixed_to_one() -> None:
    assert Settings().fork_max_depth == 1
    with pytest.raises(ValidationError):
        Settings(fork_max_depth=2)


def test_cross_phase_tool_calls_are_removed_before_history() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "category_insight", "args": {}, "id": "valid", "type": "tool_call"},
            {"name": "item_search", "args": {}, "id": "invalid", "type": "tool_call"},
        ],
    )

    normalized, had_invalid = _normalize_phase_tool_calls(
        response,
        TOOL_PHASES["reflect"],
    )

    assert had_invalid is True
    assert [call["name"] for call in normalized.tool_calls] == ["category_insight"]


@pytest.mark.parametrize(
    ("query", "expected_key", "expected_category"),
    [
        ("预算 500 元以内买耳机", "budget_max_cny", "preference"),
        ("我更喜欢头戴式耳机", "explicit_preference_", "preference"),
        ("不要入耳式耳机", "explicit_preference_", "blacklist"),
        ("我身高 180，必须选择合适尺码", "explicit_preference_", "preference"),
    ],
)
def test_explicit_memory_candidate_triggers(
    query: str, expected_key: str, expected_category: str
) -> None:
    candidates = _detected_preference_candidates(query)
    assert any(
        item["key"].startswith(expected_key) and item["category"] == expected_category
        for item in candidates
    )


def test_memory_candidate_merge_keeps_first_value_for_duplicate_key() -> None:
    first = {"key": "budget_max_cny", "category": "preference", "content": "不超过 500 元"}
    duplicate = {**first, "content": "不超过 800 元"}
    assert _merge_preference_candidates([first], [duplicate]) == [first]


def candidate(
    item_id: str,
    *,
    rank: int,
    price: float,
    material: str = "金属",
    product_url: str | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "platform": "taobao",
        "title": f"耳机 {item_id}",
        "price": price,
        "currency": "CNY",
        "rating": 4.5,
        "attributes": {"material": material},
        "product_url": product_url or f"https://example.com/{item_id}",
        "retrieval_rank": rank,
    }


class StructuredRunner:
    def __init__(self, owner: "ScriptedModel", schema: type) -> None:
        self.owner = owner
        self.schema = schema

    async def ainvoke(self, messages, config=None):
        self.owner.summary_calls += 1
        self.owner.summary_configs.append(config or {})
        if self.owner.summary_delay:
            await asyncio.sleep(self.owner.summary_delay)
        if self.owner.invalid_summary:
            return {"unexpected": "field"}
        return self.schema(final_text=self.owner.summary_text)


class ScriptedModel:
    def __init__(self, responses: Sequence[AIMessage] = ()) -> None:
        self.responses = list(responses)
        self.summary_calls = 0
        self.summary_configs: list[dict[str, Any]] = []
        self.summary_delay = 0.0
        self.invalid_summary = False
        self.summary_text = "## 精选清单\n\n- 已验证商品；下单前复核离线快照。"

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, **kwargs):
        assert schema is SummaryNarrative
        assert kwargs == {"method": "function_calling"}
        return StructuredRunner(self, schema)

    async def ainvoke(self, messages, config=None):
        return self.responses.pop(0)


def test_deepseek_summary_model_disables_thinking() -> None:
    from langchain_openai import ChatOpenAI

    from app.tools.shopping_summary import _structured_summary_model

    model = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        extra_body={"existing": True},
    )
    structured = _structured_summary_model(model)

    assert structured is not model
    assert structured.extra_body == {
        "existing": True,
        "thinking": {"type": "disabled"},
    }


def test_item_picker_applies_hard_constraints_without_score() -> None:
    result = item_picker.invoke(
        {
            "items": [
                candidate("blocked", rank=1, price=100, material="塑料"),
                {
                    **candidate("kept", rank=2, price=200, material="金属"),
                    "product_id": "product-kept",
                    "offer_id": "offer-kept",
                },
            ],
            "constraints": {
                "max_price": 500,
                "excluded_attributes": {"material": ["塑料"]},
            },
            "limit": 3,
        }
    )

    assert result["status"] == "ok"
    assert [item["item_id"] for item in result["picks"]] == ["kept"]
    assert result["picks"][0]["product_id"] == "product-kept"
    assert result["picks"][0]["offer_id"] == "offer-kept"
    assert result["rejected_brief"] == ["blocked: 属性 material 命中黑名单"]
    assert "score" not in json.dumps(result, ensure_ascii=False)


def test_item_picker_rejects_missing_hard_constraint_evidence() -> None:
    raw = candidate("unknown", rank=1, price=100)
    raw["attributes"] = {}
    result = item_picker.invoke(
        {
            "items": [raw],
            "constraints": {"required_attributes": {"material": "金属"}},
        }
    )
    assert result["status"] == "insufficient_data"
    assert "缺少硬约束属性证据" in result["rejected_brief"][0]


def test_picker_terminal_restores_verified_source_links_from_dispatch() -> None:
    messages = [
        ToolMessage(
            content=json.dumps(
                {
                    "status": "ok",
                    "search_results": [
                        {
                            "status": "ok",
                            "platform": "douyin",
                            "candidates": [
                                {
                                    "item_id": "douyin:30001",
                                    "product_id": "product-30001",
                                    "offer_id": "offer-30001",
                                    "platform": "douyin",
                                    "title": "抖音降噪耳机",
                                    "price": 159,
                                    "currency": "CNY",
                                    "rating": None,
                                    "sales": 5000,
                                    "image_url": "https://example.com/item.jpg",
                                    "attributes": {},
                                    "product_url": "https://haohuo.jinritemai.com/item/30001",
                                    "retrieval_rank": 1,
                                    "source_kind": "realtime_provider",
                                    "data_as_of": "2026-07-25T08:00:00Z",
                                    "wishlist_eligible": True,
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            name="dispatch_tool",
            tool_call_id="dispatch-1",
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "status": "ok",
                    "picks": [
                        {
                            **candidate("douyin:30001", rank=1, price=159),
                            "platform": "douyin",
                            "product_url": None,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            name="item_picker",
            tool_call_id="picker-1",
        ),
    ]

    hydrated = _hydrate_picker_picks(json.loads(messages[-1].content)["picks"], messages)

    assert hydrated[0]["product_url"] == "https://haohuo.jinritemai.com/item/30001"
    assert hydrated[0]["offer_id"] == "offer-30001"
    assert hydrated[0]["wishlist_eligible"] is True
    assert _platform_outcomes(messages) == [
        {"platform": "douyin", "status": "ok", "candidate_count": 1}
    ]


def test_item_picker_retains_successful_douyin_candidate_across_platforms() -> None:
    items = [
        {**candidate("taobao:1", rank=1, price=199), "platform": "taobao"},
        {**candidate("jingdong:2", rank=2, price=209), "platform": "jingdong"},
        {**candidate("douyin:3", rank=3, price=219), "platform": "douyin"},
    ]
    result = item_picker.invoke({"items": items, "constraints": {}, "limit": 3})
    assert {item["platform"] for item in result["picks"]} == {
        "taobao",
        "jingdong",
        "douyin",
    }


@pytest.mark.asyncio
async def test_shopping_summary_calls_shared_model_once_and_preserves_facts(
    tmp_path: Path,
) -> None:
    model = ScriptedModel()
    model.summary_text = (
        "## 精选清单\n\n| 价格 | ¥199 |\n| 运费 | 待确认 |\n\n> 数据说明：来自离线快照。"
    )
    summary = build_shopping_summary_tool(model)
    picked = {
        **candidate("one", rank=1, price=199),
        "sales": 1280,
        "reasons": ["检索顺位 1"],
        "flags": [],
        "category_annotations": {},
    }
    with thread_scope("thread-1", tmp_path, run_id="run-1"):
        message = await summary.ainvoke(
            {
                "name": "shopping_summary",
                "type": "tool_call",
                "id": "summary-1",
                "args": {
                    "goal": "购买耳机",
                    "picks": [picked],
                    "learned_preferences": [
                        {
                            "key": "material",
                            "category": "blacklist",
                            "content": "不要塑料",
                            "confidence": 1,
                        }
                    ],
                },
            },
            config={"metadata": {"parent": "main"}},
        )

    payload = json.loads(message.content)
    assert model.summary_calls == 1
    assert payload["status"] == "complete"
    assert payload["terminal"] is True
    assert payload["picks"][0]["price"] == 199
    assert "运费" not in payload["final_text"]
    assert "离线快照" not in payload["final_text"]
    assert "¥199" in payload["final_text"]
    assert payload["picks"][0]["rating"] == 4.5
    assert payload["picks"][0]["sales"] == 1280
    assert payload["learned_preferences"][0]["source_session"] == "thread-1"
    assert model.summary_configs[0]["metadata"]["model_role"] == "shopping_summary"
    assert "config" not in summary.args_schema.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_shopping_summary_not_configured_is_non_terminal() -> None:
    summary = build_shopping_summary_tool(None)
    payload = await summary.ainvoke(
        {
            "goal": "购买耳机",
            "picks": [
                {
                    **candidate("one", rank=1, price=199),
                    "reasons": [],
                    "flags": [],
                    "category_annotations": {},
                }
            ],
        }
    )
    assert payload["status"] == "not_configured"
    assert payload["terminal"] is False


@pytest.mark.asyncio
async def test_shopping_summary_timeout_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ScriptedModel()
    model.summary_delay = 0.05
    monkeypatch.setattr(
        "app.tools.shopping_summary.get_settings",
        lambda: SimpleNamespace(summary_timeout_seconds=0.001),
    )
    summary = build_shopping_summary_tool(model)
    payload = await summary.ainvoke(
        {
            "goal": "购买耳机",
            "picks": [
                {
                    **candidate("one", rank=1, price=199),
                    "reasons": [],
                    "flags": [],
                    "category_annotations": {},
                }
            ],
        }
    )
    assert payload["status"] == "error"
    assert payload["terminal"] is False
    assert model.summary_calls == 1


@pytest.mark.asyncio
async def test_shopping_summary_propagates_cancellation() -> None:
    model = ScriptedModel()
    model.summary_delay = 10
    summary = build_shopping_summary_tool(model)
    task = asyncio.create_task(
        summary.ainvoke(
            {
                "goal": "购买耳机",
                "picks": [
                    {
                        **candidate("one", rank=1, price=199),
                        "reasons": [],
                        "flags": [],
                        "category_annotations": {},
                    }
                ],
            }
        )
    )
    for _ in range(20):
        if model.summary_calls:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.summary_calls == 1


def test_registry_has_nine_business_tools_and_phase_contracts() -> None:
    tools = build_core_tools(None)
    assert tuple(tool.name for tool in tools) == CORE_TOOL_NAMES
    assert len(tools) == 9
    assert "dispatch_tool" not in CORE_TOOL_NAMES
    assert TERMINAL_TOOLS == {"shopping_summary", "chat_fallback", "web_search"}
    assert "dispatch_tool" in TOOL_PHASES["think"]
    assert "shopping_summary" in TOOL_PHASES["reflect"]


def test_loop_detection_uses_tool_arguments_and_result_digest() -> None:
    repeated: list[Any] = []
    for index in range(4):
        repeated.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "item_search",
                            "args": {"query": "耳机", "platform": "taobao"},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"status":"ok","candidates":[]}',
                    name="item_search",
                    tool_call_id=f"call-{index}",
                ),
            ]
        )
    assert loop_detected(tool_records(repeated)) is True

    different = []
    for index, platform in enumerate(("taobao", "jingdong", "douyin")):
        different.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "dispatch_tool",
                            "args": {"demand": platform},
                            "id": f"d-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"status":"ok"}',
                    name="dispatch_tool",
                    tool_call_id=f"d-{index}",
                ),
            ]
        )
    assert loop_detected(tool_records(different)) is False


def test_tool_compaction_removes_private_category_fields() -> None:
    compacted = compact_tool_content(
        "category_insight",
        json.dumps(
            {
                "status": "ok",
                "raw_evidence": ["private"],
                "content_vector": [1.0, 2.0],
                "components": ["耳塞"],
            }
        ),
    )
    assert "raw_evidence" not in compacted
    assert "content_vector" not in compacted
    assert "耳塞" in compacted


def test_cache_breakpoint_keeps_three_complete_tool_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.agent.middleware.get_settings",
        lambda: SimpleNamespace(
            compression_token_limit=10,
            compression_keep_recent=3,
        ),
    )
    messages: list[Any] = [HumanMessage(content="很长的历史" * 50)]
    for index in range(4):
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "planner",
                            "args": {"goal": str(index)},
                            "id": f"p-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"status":"ok"}',
                    name="planner",
                    tool_call_id=f"p-{index}",
                ),
            ]
        )
    update = cache_breakpoint_update(messages)
    assert update is not None
    retained_tools = [message for message in update if isinstance(message, ToolMessage)]
    assert len(retained_tools) == 3


@pytest.mark.asyncio
async def test_explicit_phase_graph_terminates_on_chat_fallback(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "chat_fallback",
                        "args": {"message": "请补充预算"},
                        "id": "fallback-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    loop = AgentLoop(model)
    with thread_scope("phase-thread", tmp_path, run_id="phase-run"):
        answer, metadata = await loop.run("买耳机", "phase-thread")
    assert answer == "请补充预算"
    assert metadata["phase"] == "done"
    assert metadata["iteration"] == 1


@pytest.mark.asyncio
async def test_explicit_phase_graph_runs_nested_summary_once(tmp_path: Path) -> None:
    picked = {
        **candidate("one", rank=1, price=199),
        "reasons": ["检索顺位 1"],
        "flags": [],
        "category_annotations": {},
    }
    model = ScriptedModel(
        [
            AIMessage(content="进入反思"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "shopping_summary",
                        "args": {
                            "goal": "购买耳机",
                            "picks": [picked],
                            "unresolved": ["运费未知"],
                            "learned_preferences": [],
                        },
                        "id": "summary-graph-1",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    loop = AgentLoop(model)
    with thread_scope("summary-thread", tmp_path, run_id="summary-run"):
        answer, metadata = await loop.run("买耳机", "summary-thread")
    assert answer.startswith("## 精选清单")
    assert metadata["phase"] == "done"
    assert model.summary_calls == 1


@pytest.mark.asyncio
async def test_provider_failure_and_empty_picker_converge_to_fallback(tmp_path: Path) -> None:
    @tool("item_search")
    async def failed_item_search(query: str, platform: str) -> dict[str, Any]:
        """Return a deterministic provider failure for loop testing."""

        return {
            "status": "error",
            "platform": platform,
            "candidates": [],
            "message": f"{query} provider failed",
        }

    from app.tools.chat_fallback import chat_fallback

    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "item_search",
                        "args": {"query": "耳机", "platform": "douyin"},
                        "id": "search-failed",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "item_picker",
                        "args": {"items": [], "constraints": {}, "limit": 3},
                        "id": "picker-empty",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "chat_fallback",
                        "args": {"message": "暂未取得可验证商品，请稍后重试"},
                        "id": "fallback-after-failure",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    loop = AgentLoop(
        model,
        tools=[failed_item_search, item_picker, chat_fallback],
        enable_dispatch=False,
    )
    with thread_scope("provider-failure", tmp_path, run_id="provider-failure-run"):
        state = await loop._invoke("买耳机", "provider-failure")
    assert state["phase"] == "done"
    assert state["terminal_result"]["status"] == "needs_clarification"
    assert state["terminal_result"]["platform_outcomes"] == [
        {
            "platform": "douyin",
            "status": "error",
            "candidate_count": 0,
            "message": "耳机 provider failed",
        }
    ]


@pytest.mark.asyncio
async def test_product_review_web_search_terminates_without_shopping_tools(tmp_path: Path) -> None:
    @tool("web_search")
    async def review_web_search(query: str, search_mode: str = "general") -> dict[str, Any]:
        """Return a deterministic review result for loop testing."""

        assert query == "Sony WH-1000XM6"
        assert search_mode == "product_reviews"
        review = {
            "rank": 1,
            "source": "知乎",
            "title": "真实体验",
            "url": "https://www.zhihu.com/question/1",
            "content": "长期使用感受",
            "score": 0.9,
            "published_date": None,
        }
        return {
            "status": "incomplete",
            "terminal": True,
            "source_kind": "content_review",
            "final_text": (
                "## 小红书 / 知乎测评 Top 3\n\n"
                "1. [真实体验](https://www.zhihu.com/question/1) · 知乎"
            ),
            "review_results": [review],
            "picks": [],
            "unresolved": ["仅检索到 1 条目标平台有效结果"],
        }

    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {
                            "query": "Sony WH-1000XM6",
                            "search_mode": "product_reviews",
                        },
                        "id": "review-search-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    loop = AgentLoop(model, tools=[review_web_search], enable_dispatch=False)
    with thread_scope("review-thread", tmp_path, run_id="review-run"):
        state = await loop._invoke("了解 Sony WH-1000XM6", "review-thread")

    assert state["phase"] == "done"
    assert state["iteration"] == 1
    assert state["terminal_result"]["source_kind"] == "content_review"
    assert state["terminal_result"]["review_results"][0]["source"] == "知乎"


@pytest.mark.asyncio
async def test_agentloop_astream_exposes_v2_graph_events(tmp_path: Path) -> None:
    loop = AgentLoop(None)
    with thread_scope("stream-thread", tmp_path, run_id="stream-run"):
        events = [event async for event in loop.astream("测试流式", "stream-thread")]
    assert events
    assert all("event" in event for event in events)
    assert any(event["event"] == "on_chain_end" and not event.get("parent_ids") for event in events)


@pytest.mark.asyncio
async def test_agentloop_returns_graceful_result_at_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = AgentLoop(None)

    async def exhausted(*_args, **_kwargs):
        raise GraphRecursionError("test limit")

    monkeypatch.setattr(loop, "_invoke", exhausted)
    answer, metadata = await loop.run("预算 500 元以内买耳机", "limit-thread")
    assert "达到本次运行上限" in answer
    assert metadata["status"] == "incomplete"
    assert metadata["phase"] == "done"
    assert metadata["learned_preferences"][0]["key"] == "budget_max_cny"


@pytest.mark.asyncio
async def test_agentloop_stream_emits_terminal_fallback_at_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = AgentLoop(None)

    class ExhaustedGraph:
        async def astream_events(self, *_args, **_kwargs):
            if False:
                yield {}
            raise GraphRecursionError("test limit")

    monkeypatch.setattr(loop, "graph", ExhaustedGraph())
    events = [event async for event in loop.astream("买耳机", "stream-limit")]
    output = events[-1]["data"]["output"]
    assert events[-1]["event"] == "on_chain_end"
    assert output["terminal_result"]["status"] == "incomplete"
    assert output["terminal_result"]["picks"] == []
