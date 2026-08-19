import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.agent.llm import build_chat_model
from app.agent.main_agent import (
    AgentLoop,
    _decision_budget_exhausted,
    _forced_termination_response,
    _normalize_phase_tool_calls,
)
from app.agent.middleware import (
    cache_breakpoint_update,
    compact_tool_content,
    loop_detected,
    tool_records,
)
from app.api.run_registry import _accumulate_final_state
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


def test_decision_budget_stops_before_graph_recursion_limit() -> None:
    assert _decision_budget_exhausted(7, repeat_threshold=4) is False
    assert _decision_budget_exhausted(8, repeat_threshold=4) is True


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
            "learned_preferences": [],
        }
    )

    assert summary_call.tool_calls[0]["name"] == "shopping_summary"
    assert summary_call.tool_calls[0]["args"]["picks"][0]["item_id"] == "jeans"


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
async def test_shopping_summary_calls_shared_model_once_and_preserves_facts(
    tmp_path: Path,
) -> None:
    model = ScriptedModel()
    model.summary_text = (
        "## 精选清单\n\n| 价格 | ¥199 |\n| 运费 | 待确认 |"
        "\n\n> 数据说明：来自离线快照。"
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
async def test_agentloop_astream_exposes_v2_graph_events(tmp_path: Path) -> None:
    loop = AgentLoop(None)
    with thread_scope("stream-thread", tmp_path, run_id="stream-run"):
        events = [event async for event in loop.astream("测试流式", "stream-thread")]
    assert events
    assert all("event" in event for event in events)
    assert any(
        event["event"] == "on_chain_end" and not event.get("parent_ids")
        for event in events
    )
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
