"""Privacy filters for data exported to an observability provider."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token|credential|database.*url|reasoning_content)",
    re.I,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]+", re.I)
_MAX_STRING = 2_000
_SAFE_TOKEN_METRICS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_miss_input_tokens",
        "reasoning_tokens",
        "estimated_tokens",
        "system_estimated_tokens",
        "history_estimated_tokens",
        "tool_result_estimated_tokens",
        "context_estimated_tokens",
        "before_estimated_tokens",
        "after_estimated_tokens",
    }
)
_SUMMARY_SCALARS = frozenset(
    {
        "status",
        "duration_ms",
        "phase",
        "model_role",
        "fork_depth",
        "cache_type",
        "cache_name",
        "cache_hit",
        "cache_hit_ratio",
        "cache_key_hash",
        "cache_ttl_seconds",
        "partial",
        "degraded_reason",
        "result_count",
        "candidate_count",
        "message_count",
        "character_count",
        "context_message_count",
        "context_character_count",
        "tool_message_count",
        "compression_triggered",
        "before_estimated_tokens",
        "after_estimated_tokens",
        "removed_message_count",
        "retained_tool_group_count",
        *_SAFE_TOKEN_METRICS,
    }
)


def _safe_text(value: str) -> str:
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _PHONE.sub("[REDACTED_PHONE]", value)
    value = _URL_QUERY.sub(r"\1?[REDACTED_QUERY]", value)
    return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…[TRUNCATED]"


def sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-safe value with common secrets and PII removed."""

    if _SENSITIVE_KEY.search(key) and key not in _SAFE_TOKEN_METRICS:
        return "[REDACTED]"
    if depth >= 8:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key)[:128]: sanitize(item, key=str(item_key), depth=depth + 1)
            for item_key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, depth=depth + 1) for item in list(value)[:100]]
    return _safe_text(str(value))


def summarize(value: Any) -> dict[str, Any]:
    """Describe payload shape without retaining business text or raw identifiers."""

    cleaned = sanitize(value)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)
    result: dict[str, Any] = {
        "kind": type(value).__name__,
        "bytes": len(encoded.encode("utf-8")),
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
    }
    if isinstance(value, Mapping):
        result["keys"] = sorted(str(key)[:64] for key in list(value)[:50])
        result["item_count"] = len(value)
        metrics = {
            str(key): cleaned[key]
            for key in value
            if key in _SUMMARY_SCALARS
            and key in cleaned
            and isinstance(cleaned[key], (str, int, float, bool, type(None)))
        }
        nested = cleaned.get("compression_metrics")
        if isinstance(nested, Mapping):
            metrics.update(
                {
                    str(key): nested[key]
                    for key in nested
                    if key in _SUMMARY_SCALARS
                    and isinstance(nested[key], (str, int, float, bool, type(None)))
                }
            )
        result["metrics"] = metrics
    elif isinstance(value, (list, tuple, set)):
        result["item_count"] = len(value)
    elif isinstance(value, str):
        result["character_count"] = len(value)
    return result


def query_summary(query: str) -> dict[str, Any]:
    return summarize(query)


def output_summary(output: Any, *, status: str, duration_ms: int) -> dict[str, Any]:
    return {"status": status, "duration_ms": duration_ms, "payload": summarize(output)}


def mask_otel_batch(capture_mode: str):
    """Build an export-stage filter that also covers LangChain-created spans."""

    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    io_keys = ("langfuse.observation.input", "langfuse.observation.output")

    def mask(*, params: Any) -> Any:
        patches: dict[Any, Any] = {}
        for identifier, span in params.spans.items():
            present = [key for key in io_keys if key in span.attributes]
            if not present:
                continue
            if capture_mode == "none":
                patches[identifier] = OtelSpanPatch(delete_attributes=tuple(present))
                continue
            replacements: dict[str, str] = {}
            for key in present:
                raw = span.attributes[key]
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    parsed = raw
                filtered = (
                    sanitize(parsed)
                    if capture_mode == "full" or span.name == "globuy.agent_run"
                    else summarize(parsed)
                )
                replacements[key] = json.dumps(filtered, ensure_ascii=False, default=str)
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
        return MaskOtelSpansResult(span_patches=patches)

    return mask


__all__ = ["mask_otel_batch", "output_summary", "query_summary", "sanitize", "summarize"]
