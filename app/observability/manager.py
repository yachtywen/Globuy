"""LangFuse lifecycle, trace context propagation, and score publishing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.observability.redaction import mask_otel_batch, output_summary, query_summary, sanitize

logger = logging.getLogger(__name__)

_current_callback: ContextVar[Any | None] = ContextVar(
    "globuy_observability_callback", default=None
)
_current_trace_id: ContextVar[str | None] = ContextVar(
    "globuy_observability_trace_id", default=None
)


def trace_id_for_run(run_id: str) -> str:
    """Return a deterministic W3C-compatible 128-bit trace ID."""

    return hashlib.sha256(f"globuy-run:{run_id}".encode()).hexdigest()[:32]


def current_observability_config() -> dict[str, Any]:
    """Return LangChain config fields for the current run/fork."""

    callback = _current_callback.get()
    trace_id = _current_trace_id.get()
    result: dict[str, Any] = {}
    if callback is not None:
        result["callbacks"] = [callback]
    if trace_id:
        result["metadata"] = {"observability_trace_id": trace_id}
    return result


@dataclass
class RunObservation:
    manager: ObservabilityManager
    run_id: str
    thread_id: str
    user_id: str
    query: str
    trace_id: str
    started: float
    _stack: ExitStack | None = None
    _callback_token: Token[Any | None] | None = None
    _trace_token: Token[str | None] | None = None
    _root: Any = None
    _closed: bool = False

    def __enter__(self) -> RunObservation:
        self._trace_token = _current_trace_id.set(self.trace_id)
        if not self.manager.enabled or self.manager.client is None:
            return self
        stack = ExitStack()
        try:
            root = stack.enter_context(
                self.manager.client.start_as_current_observation(
                    trace_context={"trace_id": self.trace_id},
                    name="globuy.agent_run",
                    as_type="agent",
                    input=(
                        None
                        if self.manager.capture_mode == "none"
                        else sanitize(self.query)
                        if self.manager.capture_mode == "full"
                        else query_summary(self.query)
                    ),
                    metadata={
                        "run_id": self.run_id,
                        "thread_id": self.thread_id,
                        "capture_mode": self.manager.capture_mode,
                    },
                )
            )
            stack.enter_context(
                self.manager.propagate_attributes(
                    user_id=self.manager.hash_user_id(self.user_id),
                    session_id=self.thread_id,
                    trace_name="globuy.agent_run",
                    environment=self.manager.environment,
                    tags=["globuy", "agent-run"],
                )
            )
            callback = self.manager.callback_factory(public_key=self.manager.public_key)
            self._callback_token = _current_callback.set(callback)
            self._root = root
            self._stack = stack
        except Exception:  # noqa: BLE001 - telemetry must never fail the business run
            stack.close()
            logger.warning("LangFuse trace initialization failed", exc_info=True)
        return self

    def finish(self, status: str, output: Any = None, error: Exception | None = None) -> None:
        if self._root is None:
            return
        duration_ms = int((time.perf_counter() - self.started) * 1000)
        try:
            safe_output = (
                None
                if self.manager.capture_mode == "none"
                else {
                    "status": status,
                    "duration_ms": duration_ms,
                    "payload": sanitize(output),
                }
                if self.manager.capture_mode == "full"
                else output_summary(output, status=status, duration_ms=duration_ms)
            )
            self._root.update(
                output=safe_output,
                level="ERROR" if error is not None or status == "failed" else "DEFAULT",
                status_message=(type(error).__name__ if error is not None else status),
                metadata={"status": status, "duration_ms": duration_ms},
            )
        except Exception:  # noqa: BLE001
            logger.warning("LangFuse trace finalization failed", exc_info=True)

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        if not self._closed:
            self._closed = True
            if self._callback_token is not None:
                _current_callback.reset(self._callback_token)
            if self._stack is not None:
                try:
                    self._stack.close()
                except Exception:  # noqa: BLE001
                    logger.warning("LangFuse trace close failed", exc_info=True)
            if self._trace_token is not None:
                _current_trace_id.reset(self._trace_token)
        return False


class ObservabilityManager:
    """Own the optional provider client without exposing credentials."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        callback_factory: Callable[..., Any] | None = None,
        propagate_attributes_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.provider = settings.observability_provider
        self.capture_mode = settings.langfuse_capture_mode
        self.environment = settings.app_env
        self.public_key = (
            settings.langfuse_public_key.get_secret_value()
            if settings.langfuse_public_key is not None
            else None
        )
        secret_key = (
            settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key is not None
            else None
        )
        salt = (
            settings.observability_hash_salt.get_secret_value()
            if settings.observability_hash_salt is not None
            else secret_key or f"{settings.app_name}:{settings.app_env}"
        )
        self._hash_salt = salt.encode()
        self.flush_timeout = settings.observability_flush_timeout_seconds
        self.configured = bool(self.provider == "langfuse" and self.public_key and secret_key)
        self.enabled = self.configured and settings.langfuse_sample_rate > 0
        self.client = client
        self.callback_factory = callback_factory
        self.propagate_attributes = propagate_attributes_fn
        self.initialization_error: str | None = None
        if not self.enabled or self.client is not None:
            return
        try:
            from langfuse import Langfuse, propagate_attributes
            from langfuse.langchain import CallbackHandler

            self.client = Langfuse(
                public_key=self.public_key,
                secret_key=secret_key,
                base_url=settings.langfuse_base_url,
                environment=settings.app_env,
                sample_rate=settings.langfuse_sample_rate,
                mask=lambda *, data, **_kwargs: sanitize(data),
                mask_otel_spans=mask_otel_batch(settings.langfuse_capture_mode),
            )
            self.callback_factory = CallbackHandler
            self.propagate_attributes = propagate_attributes
        except Exception as exc:  # noqa: BLE001
            self.enabled = False
            self.initialization_error = type(exc).__name__
            logger.warning("LangFuse initialization failed; observability disabled", exc_info=True)

    def hash_user_id(self, user_id: str) -> str:
        digest = hmac.new(self._hash_salt, user_id.encode(), hashlib.sha256).hexdigest()
        return f"usr_{digest[:32]}"

    def observe_run(
        self, *, run_id: str, thread_id: str, user_id: str, query: str
    ) -> RunObservation:
        return RunObservation(
            manager=self,
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            query=query,
            trace_id=trace_id_for_run(run_id),
            started=time.perf_counter(),
        )

    def health(self) -> dict[str, str | bool]:
        if self.provider == "none":
            status = "disabled"
        elif not self.configured:
            status = "misconfigured"
        elif self.initialization_error:
            status = "unavailable"
        elif not self.enabled:
            status = "sampling_disabled"
        else:
            status = "ready"
        return {
            "observability_provider": self.provider,
            "observability_configured": self.configured,
            "observability_enabled": self.enabled,
            "observability_status": status,
            "observability_capture_mode": self.capture_mode,
        }

    def publish_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float | str,
        data_type: str = "NUMERIC",
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled or self.client is None:
            return False
        try:
            self.client.create_score(
                trace_id=trace_id,
                name=name,
                value=value,
                data_type=data_type,
                comment=comment,
                metadata=sanitize(metadata or {}),
            )
            return True
        except Exception:  # noqa: BLE001
            logger.warning("LangFuse score publishing failed", exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.client is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self.client.shutdown), self.flush_timeout)
        except Exception:  # noqa: BLE001
            logger.warning("LangFuse shutdown timed out or failed", exc_info=True)


__all__ = ["ObservabilityManager", "current_observability_config", "trace_id_for_run"]
