from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from langfuse.types import MaskOtelSpansParams, OtelSpanData

from app.config import Settings
from app.observability import (
    ObservabilityManager,
    current_observability_config,
    trace_id_for_run,
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
