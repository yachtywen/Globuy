"""User-visible shopping-result filtering for fields absent from the catalog."""

from __future__ import annotations

import re
from collections.abc import Iterable

_HIDDEN_RESULT_PATTERN = re.compile(
    r"运费|邮费|包邮|shipping[_ ]?fee|离线快照|离线数据快照|数据说明",
    re.IGNORECASE,
)


def is_hidden_result_text(value: str) -> bool:
    return bool(_HIDDEN_RESULT_PATTERN.search(value))


def visible_unresolved(values: Iterable[object] | None) -> list[str]:
    return [
        value
        for value in (values or [])
        if isinstance(value, str) and value and not is_hidden_result_text(value)
    ]


def sanitize_shopping_markdown(value: str) -> str:
    lines = [line for line in value.splitlines() if not is_hidden_result_text(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


__all__ = ["is_hidden_result_text", "sanitize_shopping_markdown", "visible_unresolved"]
