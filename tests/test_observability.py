from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langfuse.langchain import CallbackHandler
from langfuse.types import MaskOtelSpansParams, OtelSpanData

from app.config import Settings
from app.observability import (
    ObservabilityManager,
    current_observability_config,
    trace_id_for_run,
)
from app.observability.callbacks import _raw_usage
from app.observability.metrics import (
    compression_metrics,
    context_metrics,
    normalize_generation_usage,
)
from app.observability.redaction import mask_otel_batch, sanitize, summarize


class FakeRoot:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class FakeClient:
    def __init__(self) -> None:
        self.root = FakeRoot()
        self.starts: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.shutdown_called = False

    @contextmanager
    def start_as_current_observation(self, **kwargs: Any):
        self.starts.append(kwargs)
        yield self.root

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def shutdown(self) -> None:
        self.shutdown_called = True


@contextmanager
def fake_propagate(**_kwargs: Any):
    yield


def _manager(client: FakeClient) -> ObservabilityManager:
    settings = Settings(
        observability_provider="langfuse",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        observability_hash_salt="test-salt",
    )
    return ObservabilityManager(
        settings,
        client=client,
        callback_factory=lambda **_kwargs: "callback",
        propagate_attributes_fn=fake_propagate,
    )


def test_trace_context_is_deterministic_and_scoped() -> None:
    client = FakeClient()
    manager = _manager(client)
    expected = trace_id_for_run("run-1")
    assert expected == trace_id_for_run("run-1")
    assert len(expected) == 32

    with manager.observe_run(
        run_id="run-1", thread_id="thread-1", user_id="alice@example.com", query="耳机"
    ) as observation:
        config = current_observability_config()
        assert config["callbacks"] == ["callback"]
        assert config["metadata"]["observability_trace_id"] == expected
        observation.finish("succeeded", {"final_text": "完成"})

    assert current_observability_config() == {}
    assert client.starts[0]["trace_context"] == {"trace_id": expected}
    assert client.starts[0]["input"]["kind"] == "str"
    assert "耳机" not in json.dumps(client.starts[0]["input"], ensure_ascii=False)
    assert client.root.updates[0]["metadata"]["status"] == "succeeded"


def test_redaction_and_summary_do_not_retain_sensitive_text() -> None:
    payload = {
        "authorization": "Bearer secret",
        "email": "alice@example.com",
        "phone": "13812345678",
        "url": "https://example.com/item?token=secret",
    }
    cleaned = sanitize(payload)
    encoded = json.dumps(cleaned, ensure_ascii=False)
    assert "Bearer secret" not in encoded
    assert "alice@example.com" not in encoded
    assert "13812345678" not in encoded
    assert "token=secret" not in encoded
    assert "example.com" in encoded
    assert "alice" not in json.dumps(summarize(payload), ensure_ascii=False)


def test_summary_mask_covers_langchain_otel_input_and_output() -> None:
    span = OtelSpanData(
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id=None,
        name="ChatOpenAI",
        instrumentation_scope_name="langfuse",
        instrumentation_scope_version="4",
        attributes={
            "langfuse.observation.input": json.dumps({"prompt": "raw private prompt"}),
            "langfuse.observation.output": json.dumps({"answer": "raw private answer"}),
        },
        resource_attributes={},
    )
    identifier = (span.trace_id, span.span_id)
    result = mask_otel_batch("summary")(params=MaskOtelSpansParams(spans={identifier: span}))
    patch = result.span_patches[identifier]
    encoded = json.dumps(dict(patch.set_attributes), ensure_ascii=False)
    assert "raw private" not in encoded
    assert "sha256" in encoded


def test_score_publication_is_explicit_and_fail_open() -> None:
    client = FakeClient()
    manager = _manager(client)
    assert manager.publish_score(trace_id="a" * 32, name="quality", value=0.9)
    assert client.scores[0]["trace_id"] == "a" * 32
    assert "secret" not in manager.health()


def test_manual_generation_attaches_to_existing_trace() -> None:
    client = FakeClient()
    manager = _manager(client)

    with manager.observe_generation(
        trace_id="b" * 32,
        name="eval.judge",
        model="judge-test",
        input={"kind": "summary"},
        metadata={"model_role": "judge"},
    ) as generation:
        assert generation is client.root
        generation.update(usage_details={"input": 2, "output": 1, "total": 3})

    assert client.starts[0]["trace_context"] == {"trace_id": "b" * 32}
    assert client.starts[0]["as_type"] == "generation"
    assert client.starts[0]["name"] == "eval.judge"


def test_disabled_manager_has_safe_health() -> None:
    manager = ObservabilityManager(Settings(observability_provider="none"))
    assert manager.health() == {
        "observability_provider": "none",
        "observability_configured": False,
        "observability_enabled": False,
        "observability_status": "disabled",
        "observability_capture_mode": "summary",
    }


def test_callback_failure_does_not_escape_business_scope() -> None:
    client = FakeClient()
    settings = Settings(
        observability_provider="langfuse",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )

    def fail_callback(**_kwargs: Any) -> Any:
        raise RuntimeError("telemetry unavailable")

    manager = ObservabilityManager(
        settings,
        client=client,
        callback_factory=fail_callback,
        propagate_attributes_fn=fake_propagate,
    )
    with manager.observe_run(
        run_id="run-fail", thread_id="thread-fail", user_id="user", query="query"
    ):
        assert current_observability_config()["metadata"]["observability_trace_id"]
    assert current_observability_config() == {}


def test_context_metrics_separate_system_history_and_tool_results() -> None:
    messages = [
        SystemMessage(content="system" * 8),
        HumanMessage(content="history" * 8),
        ToolMessage(content='{"status":"ok"}', tool_call_id="tool-1"),
    ]

    metrics = context_metrics(messages)

    assert metrics.message_count == 3
    assert metrics.tool_message_count == 1
    assert metrics.estimated_tokens == (
        metrics.system_estimated_tokens
        + metrics.history_estimated_tokens
        + metrics.tool_result_estimated_tokens
    )
    assert "input_tokens" not in metrics.metadata()
    assert metrics.metadata()["context_estimated_tokens"] == metrics.estimated_tokens
    assert metrics.metadata()["system_estimated_tokens"] == metrics.system_estimated_tokens
    assert "context_system_estimated_tokens" not in metrics.metadata()


def test_deepseek_usage_is_normalized_into_exclusive_langfuse_buckets() -> None:
    usage = normalize_generation_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
    )

    assert usage.cache_hit is True
    assert usage.cache_hit_ratio == 0.6
    assert usage.cache_read_input_tokens == 60
    assert usage.cache_miss_input_tokens == 40
    parser = CallbackHandler.on_llm_end.__globals__["_parse_usage_model"]
    buckets = parser(usage.langchain_usage_metadata())
    assert buckets == {
        "total": 120,
        "input": 40,
        "output": 15,
        "input_cache_read": 60,
        "output_reasoning": 5,
    }
    assert usage.langfuse_usage_details() == buckets


def test_missing_provider_cache_fields_remain_unknown() -> None:
    usage = normalize_generation_usage(
        {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    )

    assert usage.cache_hit is None
    assert usage.cache_hit_ratio is None
    assert usage.cache_read_input_tokens is None
    assert usage.metadata()["cache_hit"] is None
    assert usage.metadata()["cache_hit_ratio"] is None


def test_usage_falls_back_to_chat_message_response_metadata() -> None:
    response = SimpleNamespace(
        llm_output=None,
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        response_metadata={
                            "token_usage": {
                                "prompt_tokens": 12,
                                "completion_tokens": 3,
                                "total_tokens": 15,
                                "prompt_cache_hit_tokens": 7,
                                "prompt_cache_miss_tokens": 5,
                            }
                        },
                        usage_metadata=None,
                    )
                )
            ]
        ],
    )

    usage = normalize_generation_usage(_raw_usage(response))

    assert usage.cache_read_input_tokens == 7
    assert usage.cache_miss_input_tokens == 5


def test_compression_metrics_distinguish_breakpoint_from_prompt_cache() -> None:
    old = HumanMessage(content="x" * 100)
    retained_call = AIMessage(
        content="",
        tool_calls=[{"name": "search", "args": {}, "id": "call-1", "type": "tool_call"}],
    )
    retained_result = ToolMessage(content="{}", tool_call_id="call-1")
    before = [old, retained_call, retained_result]

    unchanged = compression_metrics(before, None)
    compressed = compression_metrics(
        before,
        [HumanMessage(content="summary"), retained_call, retained_result],
    )

    assert unchanged.metadata()["cache_type"] == "context_breakpoint"
    assert unchanged.compression_triggered is False
    assert compressed.compression_triggered is True
    assert compressed.removed_message_count == 1
    assert compressed.retained_tool_group_count == 1


def test_summary_retains_safe_metrics_without_exposing_token_values() -> None:
    payload = {
        "status": "ok",
        "cache_type": "application",
        "cache_hit": True,
        "tool_result_estimated_tokens": 42,
        "token": "private-provider-token",
        "reasoning_content": "private reasoning",
    }

    result = summarize(payload)
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["metrics"]["cache_hit"] is True
    assert result["metrics"]["tool_result_estimated_tokens"] == 42
    assert "private-provider-token" not in encoded
    assert "private reasoning" not in encoded


def test_summary_exposes_nested_cache_breakpoint_metrics() -> None:
    result = summarize(
        {
            "compression_metrics": {
                "cache_type": "context_breakpoint",
                "compression_triggered": False,
                "before_estimated_tokens": 120,
                "after_estimated_tokens": 120,
            }
        }
    )

    assert result["metrics"] == {
        "cache_type": "context_breakpoint",
        "compression_triggered": False,
        "before_estimated_tokens": 120,
        "after_estimated_tokens": 120,
    }
