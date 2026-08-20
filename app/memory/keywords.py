"""Deterministic local keyword extraction for long-term-memory hybrid recall."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{1,63}|[\u4e00-\u9fff]{2,32}")
_STOPWORDS = {
    "\u4e00\u4e2a",
    "\u4e00\u4e9b",
    "\u8fd9\u4e2a",
    "\u90a3\u4e2a",
    "\u6bd4\u8f83",
    "\u5e0c\u671b",
    "\u559c\u6b22",
    "\u4e0d\u559c\u6b22",
    "\u7528\u6237",
    "\u5546\u54c1",
    "\u4e1c\u897f",
    "\u9700\u8981",
}


def extract_keywords(text: str, *, limit: int = 24) -> list[str]:
    """Return stable, de-duplicated Chinese/domain tokens without a model call."""

    normalized = " ".join(text.strip().split())
    tokens: list[str] = []
    for match in _TOKEN.finditer(normalized):
        token = match.group(0).casefold()
        candidates = [token]
        if token[0] >= "\u4e00" and len(token) > 2:
            candidates.extend(
                token[start : start + size]
                for size in (2, 3, 4)
                for start in range(0, len(token) - size + 1)
            )
        for candidate in candidates:
            if candidate in _STOPWORDS or candidate in tokens:
                continue
            tokens.append(candidate)
            if len(tokens) >= limit:
                return tokens
    return tokens
