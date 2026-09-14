import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.base import SearchItem
from pydantic import ValidationError

from app.agent.llm import build_chat_model, model_request_kwargs
from app.agent.main_agent import (
    AgentLoop,
    _decision_budget_exhausted,
    _deterministic_summary_fallback,
    _forced_termination_response,
    _normalize_phase_tool_calls,
    _single_summary_tool_call,
)
from app.agent.middleware import (
    cache_breakpoint_update,
    compact_tool_content,
    loop_detected,
    repair_incomplete_tool_groups,
    tool_records,
)
from app.api.run_registry import _accumulate_final_state, _product_search_summary
from app.config import Settings
from app.tools import CORE_TOOL_NAMES, TERMINAL_TOOLS, TOOL_PHASES, build_core_tools
from app.tools.item_picker import PickedItem, build_item_picker_tool, item_picker
from app.tools.shopping_summary import SummaryNarrative, build_shopping_summary_tool
from app.utils.thread_ctx import thread_scope


def test_fork_depth_configuration_is_fixed_to_one() -> None:
    assert Settings().fork_max_depth == 1
    with pytest.raises(ValidationError):
        Settings(fork_max_depth=2)


def test_product_search_summary_ignores_conversation_only_results() -> None:
    state = {
        "messages": [
            ToolMessage(
                content=json.dumps({"status": "needs_clarification"}),
                name="chat_fallback",
                tool_call_id="chat-1",
            )
        ]
    }

    assert _product_search_summary(state) == (False, 0)


def test_product_search_summary_tracks_direct_empty_search() -> None:
    state = {
        "messages": [
            ToolMessage(
                content=json.dumps({"status": "ok", "candidates": []}),
                name="item_search",
                tool_call_id="search-1",
            )
        ]
    }

    assert _product_search_summary(state) == (True, 0)


def test_product_search_summary_reads_fork_search_results() -> None:
    state = {
        "messages": [
            ToolMessage(
                content=json.dumps(
                    {
                        "status": "ok",
                        "search_results": [
                            {"status": "partial", "candidates": [{"item_id": "one"}]},
                            {
                                "status": "partial",
                                "provider_status": "blocked",
                                "candidates": [],
                            },
                        ],
                    }
                ),
                name="dispatch_tool",
                tool_call_id="dispatch-1",
            )
        ]
    }

    assert _product_search_summary(state) == (True, 1)


@pytest.mark.asyncio
async def test_memory_prompt_budget_limits_plain_text_memories(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(UTC)

    class FakeMemoryStore:
        async def asearch(self, namespace, *, query: str, limit: int):
            del namespace, query, limit
            return [
                SearchItem(
                    namespace=("users", "user-1", "memories"),
                    key="first-memory",
                    value={"memory": "prefer over-ear headphones"},
                    created_at=now,
                    updated_at=now,
                ),
                *[
                    SearchItem(
                        namespace=("users", "user-1", "memories"),
                        key=f"ordinary-{index}",
                        value={"memory": "x" * 400},
                        created_at=now,
                        updated_at=now,
                    )
                    for index in range(10)
                ],
            ]

    monkeypatch.setattr(
        "app.agent.main_agent.get_settings",
        lambda: SimpleNamespace(memory_recall_limit=10, memory_prompt_token_limit=128),
    )
    loop = AgentLoop(model=None, tools=[], store=FakeMemoryStore(), enable_dispatch=False)
    with thread_scope("thread", tmp_path, run_id="run", user_id="user-1"):
        state = await loop._state_with_memory("headphones")
    assert "prefer over-ear headphones" in state["memory_context"]
    assert state["memory_metrics"]["injected_count"] >= 1
    assert state["memory_metrics"]["injected_estimated_tokens"] <= 128


def test_cross_phase_tool_calls_are_removed_before_history() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "price_compare", "args": {}, "id": "valid", "type": "tool_call"},
            {"name": "item_search", "args": {}, "id": "invalid", "type": "tool_call"},
        ],
    )

    normalized, had_invalid = _normalize_phase_tool_calls(
        response,
        TOOL_PHASES["reflect"],
    )

    assert had_invalid is True
    assert [call["name"] for call in normalized.tool_calls] == ["price_compare"]


def test_summary_is_the_only_and_single_terminal_call() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "price_compare", "args": {}, "id": "price", "type": "tool_call"},
            {
                "name": "shopping_summary",
                "args": {"goal": "one", "picks": []},
                "id": "summary-1",
                "type": "tool_call",
            },
            {
                "name": "shopping_summary",
                "args": {"goal": "two", "picks": []},
                "id": "summary-2",
                "type": "tool_call",
            },
        ],
    )

    normalized = _single_summary_tool_call(response)

    assert [call["id"] for call in normalized.tool_calls] == ["summary-1"]


def test_incomplete_tool_group_is_repaired_before_next_human_message() -> None:
    messages = [
        HumanMessage(content="old turn"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "shopping_summary",
                    "args": {"goal": "old", "picks": []},
                    "id": "summary-interrupted",
                    "type": "tool_call",
                }
            ],
        ),
        HumanMessage(content="new turn"),
    ]

    update = repair_incomplete_tool_groups(messages)

    assert update is not None
    repaired = update[1:]
    assert isinstance(repaired[2], ToolMessage)
    assert repaired[2].tool_call_id == "summary-interrupted"
    assert json.loads(repaired[2].content)["status"] == "interrupted"
    assert isinstance(repaired[3], HumanMessage)
    assert repaired[3].content == "new turn"


def test_complete_tool_group_is_not_rewritten() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "planner", "args": {}, "id": "planner-1", "type": "tool_call"}],
        ),
        ToolMessage(content='{"status":"ok"}', name="planner", tool_call_id="planner-1"),
    ]

    assert repair_incomplete_tool_groups(messages) is None


def test_partial_parallel_tool_group_gets_only_missing_response() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "planner", "args": {}, "id": "planner-1", "type": "tool_call"},
                {
                    "name": "web_search",
                    "args": {},
                    "id": "web-1",
                    "type": "tool_call",
                },
            ],
        ),
        ToolMessage(content='{"status":"ok"}', name="planner", tool_call_id="planner-1"),
        HumanMessage(content="next"),
    ]

    update = repair_incomplete_tool_groups(messages)

    assert update is not None
    tool_messages = [message for message in update if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["planner-1", "web-1"]
    assert json.loads(tool_messages[1].content)["status"] == "interrupted"


def test_decision_budget_stops_before_graph_recursion_limit() -> None:
    assert _decision_budget_exhausted(7, repeat_threshold=4) is False
    assert _decision_budget_exhausted(8, repeat_threshold=4) is True


def test_missing_source_link_requires_deterministic_fallback() -> None:
    state = {
        "messages": [
            HumanMessage(content="buy"),
            ToolMessage(
                name="item_picker",
                tool_call_id="picker-missing-link",
                content=json.dumps(
                    {
                        "status": "ok",
                        "picks": [{"item_id": "one", "product_url": None}],
                    }
                ),
            ),
        ]
    }

    message = _deterministic_summary_fallback(state)

    assert message is not None
    assert "来源链接" in message


def test_forced_termination_selects_verified_candidates_then_summarizes() -> None:
    search = ToolMessage(
        name="item_search",
        tool_call_id="search-1",
        content=json.dumps(
            {
                "status": "ok",
                "platform": "taobao",
                "candidates": [candidate("jeans", rank=1, price=499)],
            }
        ),
    )
    picker_call = _forced_termination_response(
        {
            "messages": [search],
            "original_query": "买500元左右的牛仔裤",
            "shopping_intent": {"filters": {"min_price": 350, "max_price": 550}},
        }
    )

    assert picker_call.tool_calls[0]["name"] == "item_picker"
    assert picker_call.tool_calls[0]["args"]["constraints"] == {
        "min_price": 350,
        "max_price": 550,
    }

    picked = item_picker.invoke(picker_call.tool_calls[0]["args"])
    summary_call = _forced_termination_response(
        {
            "messages": [
                ToolMessage(
                    name="item_picker",
                    tool_call_id="picker-1",
                    content=json.dumps(picked),
                )
            ],
            "original_query": "买500元左右的牛仔裤",
        }
    )

    assert summary_call.tool_calls[0]["name"] == "shopping_summary"
    assert summary_call.tool_calls[0]["args"]["picks"][0]["item_id"] == "jeans"


def test_forced_termination_deduplicates_by_offer_before_compat_item_id() -> None:
    rows = []
    for platform, offer in (("taobao", "offer-a"), ("jingdong", "offer-b")):
        row = candidate("shared-item-id", rank=1, price=499)
        row.update({"platform": platform, "offer_id": offer})
        rows.append(row)
    response = _forced_termination_response(
        {
            "messages": [
                ToolMessage(
                    name="dispatch_tool",
                    tool_call_id="dispatch-results",
                    content=json.dumps(
                        {
                            "search_results": [
                                {"platform": "taobao", "candidates": [rows[0]]},
                                {"platform": "jingdong", "candidates": [rows[1]]},
                            ]
                        }
                    ),
                )
            ],
            "original_query": "跨平台比较",
            "shopping_intent": {"filters": {}},
        }
    )

    assert [item["offer_id"] for item in response.tool_calls[0]["args"]["items"]] == [
        "offer-a",
        "offer-b",
    ]


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
        self.model_configs: list[dict[str, Any]] = []
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
        self.model_configs.append(config or {})
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


def test_deepseek_main_agent_disables_thinking_for_multiturn_tools() -> None:
    model = build_chat_model(
        Settings(
            model_provider="openai-compatible",
            llm_model="deepseek-v4-flash",
            llm_api_key="test-key",
            llm_base_url="https://api.deepseek.com",
        )
    )

    assert model is not None
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_non_deepseek_main_agent_keeps_provider_defaults() -> None:
    model = build_chat_model(
        Settings(
            model_provider="openai-compatible",
            llm_model="test-model",
            llm_api_key="test-key",
            llm_base_url="https://llm.example.com/v1",
        )
    )

    assert model is not None
    assert model.extra_body is None


def test_kimi_k26_uses_non_thinking_tools_and_window_budget() -> None:
    model = build_chat_model(
        Settings(
            model_provider="openai-compatible",
            llm_model="kimi-k2.6",
            llm_api_key="test-key",
            llm_base_url="https://api.moonshot.cn/v1",
            llm_temperature=0.3,
            llm_context_window_tokens=262_144,
            llm_max_output_tokens=32_768,
        )
    )

    assert model is not None
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert model.temperature is None
    assert model.max_tokens == 32_768


def test_kimi_request_uses_thread_as_prompt_cache_key() -> None:
    settings = Settings(
        model_provider="openai-compatible",
        llm_api_key="test-key",
        llm_base_url="https://api.moonshot.cn/v1",
    )

    model = build_chat_model(settings)
    assert model_request_kwargs("thread-123", model=model, settings=settings) == {
        "extra_body": {
            "thinking": {"type": "disabled"},
            "prompt_cache_key": "thread-123",
        }
    }
    assert model_request_kwargs(None, model=model, settings=settings) == {}


def test_chat_model_can_apply_configured_request_throttle() -> None:
    from langchain_openai import ChatOpenAI

    settings = Settings(
        model_provider="openai-compatible",
        llm_api_key="test-key",
        llm_base_url="https://api.moonshot.cn/v1",
        llm_requests_per_minute=2,
    )

    model = build_chat_model(settings)

    assert isinstance(model, ChatOpenAI)
    assert model.rate_limiter is not None


def test_picked_item_truncates_extra_model_generated_reasons() -> None:
    item = PickedItem.model_validate(
        {
            "item_id": "item-1",
            "platform": "jingdong",
            "title": "Headphones",
            "price": 699,
            "reasons": ["one", "two", "three", "four"],
        }
    )

    assert item.reasons == ["one", "two", "three"]


def test_non_kimi_request_does_not_add_prompt_cache_key() -> None:
    settings = Settings(
        model_provider="openai-compatible",
        llm_api_key="test-key",
        llm_base_url="https://llm.example.com/v1",
    )

    model = build_chat_model(settings)
    assert model_request_kwargs("thread-123", model=model, settings=settings) == {}


def test_kimi_context_window_derives_cache_breakpoint_watermarks() -> None:
    settings = Settings(
        llm_context_window_tokens=262_144,
        llm_max_output_tokens=32_768,
        compression_token_limit=None,
        compression_trigger_ratio=0.75,
        compression_target_ratio=0.50,
        compression_safety_margin_tokens=16_384,
    )

    assert settings.compression_trigger_tokens == 196_608
    assert settings.compression_target_tokens == 131_072


def test_explicit_legacy_compression_limit_overrides_window_ratio() -> None:
    settings = Settings(compression_token_limit=20_000)

    assert settings.compression_trigger_tokens == 20_000
    assert settings.compression_target_tokens == 18_976


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


@pytest.mark.asyncio
async def test_item_picker_relaxes_schema_level_unverifiable_attribute_requirement() -> None:
    # 模型把自由文本硬约束（降噪功能）翻译成 required_attributes {"降噪": "是"}，但候选
    # 属性 schema 根本不含该键（≥2 个候选、全数据集都无此键时无法区分优劣）——生产工具应放宽
    # 该键并继续，而不是让全部真实候选被零信息过滤。
    tool = build_item_picker_tool(None)
    first = candidate("a", rank=1, price=100)
    second = {**candidate("b", rank=2, price=200), "product_id": "product-b"}
    result = await tool.ainvoke(
        {
            "items": [first, second],
            "constraints": {"required_attributes": {"降噪": "是"}},
            "limit": 3,
        }
    )
    assert result["status"] in {"ok", "degraded"}
    assert result["picks"]
    assert any(
        "已放宽候选属性中不存在的必需字段：降噪" in line for line in result["rejected_brief"]
    )


@pytest.mark.asyncio
async def test_item_picker_keeps_enforcing_verifiable_required_attribute() -> None:
    # 键在部分候选中存在时可区分优劣，缺失证据的候选仍被拒绝（契约不变）。
    tool = build_item_picker_tool(None)
    first = {**candidate("with-evidence", rank=1, price=100, material="金属"), "product_id": "p3"}
    second = candidate("without-evidence", rank=2, price=200)
    second["attributes"] = {}
    result = await tool.ainvoke(
        {
            "items": [first, second],
            "constraints": {"required_attributes": {"material": "金属"}},
            "limit": 3,
        }
    )
    assert result["status"] in {"ok", "degraded"}
    assert [item["item_id"] for item in result["picks"]] == ["with-evidence"]
    assert any("缺少硬约束属性证据" in line for line in result["rejected_brief"])


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
    assert model.summary_configs[0]["metadata"]["model_role"] == "shopping_summary"
    assert model.summary_configs[0]["run_name"] == "shopping_summary.generation"
    assert model.summary_configs[0]["metadata"]["context_message_count"] == 2
    assert model.summary_configs[0]["metadata"]["context_estimated_tokens"] > 0
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


def test_registry_has_seven_business_tools_and_phase_contracts() -> None:
    tools = build_core_tools(None)
    assert tuple(tool.name for tool in tools) == CORE_TOOL_NAMES
    assert len(tools) == 7
    assert "dispatch_tool" not in CORE_TOOL_NAMES
    assert TERMINAL_TOOLS == {"shopping_summary", "chat_fallback"}
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


def test_tool_compaction_removes_private_search_fields() -> None:
    compacted = compact_tool_content(
        "item_search",
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
    assert model.model_configs[0]["run_name"] == "coordinator.think"
    assert model.model_configs[0]["metadata"]["phase"] == "think"
    assert model.model_configs[0]["metadata"]["context_message_count"] == 3


@pytest.mark.asyncio
async def test_explicit_phase_graph_runs_nested_summary_once(tmp_path: Path) -> None:
    picked = {
        **candidate("one", rank=1, price=199),
        "reasons": ["检索顺位 1"],
        "flags": [],
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
async def test_failed_summary_falls_back_without_model_retry(tmp_path: Path) -> None:
    picked = {
        **candidate("one", rank=1, price=199),
        "reasons": ["verified"],
        "flags": [],
    }
    model = ScriptedModel(
        [
            AIMessage(content="reflect"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "shopping_summary",
                        "args": {"goal": "buy", "picks": [picked]},
                        "id": "summary-once",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    loop = AgentLoop(
        model,
        tools=build_core_tools(None),
        enable_dispatch=False,
    )
    with thread_scope("summary-failure-thread", tmp_path, run_id="summary-failure-run"):
        answer, metadata = await loop.run("buy headphones", "summary-failure-thread")
        snapshot = await loop.graph.aget_state(loop._config("summary-failure-thread"))

    tool_messages = [
        message for message in snapshot.values["messages"] if isinstance(message, ToolMessage)
    ]
    assert metadata["phase"] == "done"
    assert answer
    assert [message.name for message in tool_messages].count("shopping_summary") == 1
    assert [message.name for message in tool_messages].count("chat_fallback") == 1
    assert model.responses == []


@pytest.mark.asyncio
async def test_empty_picker_goes_directly_to_fallback(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AIMessage(content="reflect"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "item_picker",
                        "args": {"items": [], "constraints": {}, "limit": 3},
                        "id": "empty-picker",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    loop = AgentLoop(model, enable_dispatch=False)
    with thread_scope("empty-picker-thread", tmp_path, run_id="empty-picker-run"):
        answer, metadata = await loop.run("buy headphones", "empty-picker-thread")
        snapshot = await loop.graph.aget_state(loop._config("empty-picker-thread"))

    tool_names = [
        message.name for message in snapshot.values["messages"] if isinstance(message, ToolMessage)
    ]
    assert metadata["phase"] == "done"
    assert answer
    assert tool_names.count("item_picker") == 1
    assert "shopping_summary" not in tool_names
    assert tool_names.count("chat_fallback") == 1
    assert model.responses == []


@pytest.mark.asyncio
async def test_prepare_node_persists_repaired_tool_group(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "chat_fallback",
                        "args": {"message": "recovered"},
                        "id": "fallback-after-repair",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    loop = AgentLoop(model, enable_dispatch=False)
    state = loop._initial_state("new turn")
    state["messages"] = [
        HumanMessage(content="old turn"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "shopping_summary",
                    "args": {"goal": "old", "picks": []},
                    "id": "old-summary",
                    "type": "tool_call",
                }
            ],
        ),
        HumanMessage(content="new turn"),
    ]
    with thread_scope("repair-thread", tmp_path, run_id="repair-run"):
        result = await loop.graph.ainvoke(state, config=loop._config("repair-thread"))

    messages = result["messages"]
    old_call_index = next(
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage)
        and any(call.get("id") == "old-summary" for call in message.tool_calls)
    )
    assert isinstance(messages[old_call_index + 1], ToolMessage)
    assert messages[old_call_index + 1].tool_call_id == "old-summary"
    assert isinstance(messages[old_call_index + 2], HumanMessage)
    assert messages[old_call_index + 2].content == "new turn"
    assert result["phase"] == "done"


@pytest.mark.asyncio
async def test_agentloop_astream_exposes_v2_graph_events(tmp_path: Path) -> None:
    loop = AgentLoop(None)
    with thread_scope("stream-thread", tmp_path, run_id="stream-run"):
        events = [event async for event in loop.astream("测试流式", "stream-thread")]
    assert events
    assert all("event" in event for event in events)
    assert any(event["event"] == "on_chain_end" and not event.get("parent_ids") for event in events)
    assert events[-1]["event"] == "globuy_final_state"
    assert events[-1]["data"]["output"]["phase"] == "done"


def test_authoritative_stream_state_cannot_be_overwritten_by_fork_end_event() -> None:
    state, authoritative = _accumulate_final_state(
        None,
        False,
        {
            "event": "on_chain_end",
            "data": {"output": {"phase": "done", "original_query": "fork"}},
        },
    )
    state, authoritative = _accumulate_final_state(
        state,
        authoritative,
        {
            "event": "globuy_final_state",
            "data": {
                "output": {
                    "phase": "done",
                    "original_query": "root",
                    "terminal_result": {"status": "complete", "picks": [{"id": 1}]},
                }
            },
        },
    )
    state, authoritative = _accumulate_final_state(
        state,
        authoritative,
        {
            "event": "on_chain_end",
            "data": {"output": {"phase": "done", "original_query": "late-fork"}},
        },
    )

    assert authoritative is True
    assert state is not None
    assert state["original_query"] == "root"
    assert state["terminal_result"]["picks"] == [{"id": 1}]
