"""Privacy filters for data exported to an observability provider."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token|credential)", re.I
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]+", re.I)
_MAX_STRING = 2_000


def _safe_text(value: str) -> str:
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _PHONE.sub("[REDACTED_PHONE]", value)
    value = _URL_QUERY.sub(r"\1?[REDACTED_QUERY]", value)
    return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…[TRUNCATED]"


def sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-safe value with common secrets and PII removed."""

    if _SENSITIVE_KEY.search(key):
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
