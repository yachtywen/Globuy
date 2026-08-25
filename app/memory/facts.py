"""Deterministic normalization and safety policy for durable memory facts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

PERSISTENCE_SCOPES = {"long_term", "session_only"}
SCOPE_TYPES = {"global", "category", "brand", "product"}
POLARITIES = {"positive", "negative"}
EVIDENCE_TYPES = {"explicit", "inferred", "imported"}

_SECRET = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\s*[:=]"
)
_PII = re.compile(
    r"(?i)(?:[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+|"
    r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d))"
)
_INSTRUCTION = re.compile(
    r"(?i)(system\s*prompt|ignore\s+(all\s+)?previous|忽略.{0,8}(指令|提示词)|调用.{0,8}工具)"
)
_SESSION_ONLY = re.compile(r"(?:这次|本次|今天|当前|临时).{0,12}(预算|颜色|品牌|要求|只要)")


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def normalized_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value).casefold()
    if isinstance(value, list):
        return [normalized_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalized_value(value[key]) for key in sorted(value)}
    return value


def build_fact_slot(
    *, subject: str | None, predicate: str | None, scope_type: str | None, scope_value: str | None
) -> str | None:
    if not subject or not predicate:
        return None
    scope = scope_type if scope_type in SCOPE_TYPES else "global"
    material = "\0".join(
        (
            normalize_text(subject).casefold(),
            normalize_text(predicate).casefold(),
            scope,
            normalize_text(scope_value or "").casefold(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def same_fact(
    *, left_value: Any, left_polarity: str | None, right_value: Any, right_polarity: str | None
) -> bool:
    return (
        json.dumps(normalized_value(left_value), sort_keys=True, ensure_ascii=False)
        == json.dumps(normalized_value(right_value), sort_keys=True, ensure_ascii=False)
        and (left_polarity or "positive") == (right_polarity or "positive")
    )


def durable_candidate_allowed(
    *, content: str, persistence_scope: str, evidence_type: str
) -> tuple[bool, str | None]:
    if persistence_scope not in PERSISTENCE_SCOPES:
        return False, "invalid_persistence_scope"
    if persistence_scope == "session_only" or _SESSION_ONLY.search(content):
        return False, "session_only"
    if evidence_type not in EVIDENCE_TYPES:
        return False, "invalid_evidence_type"
    if _SECRET.search(content):
        return False, "sensitive_secret"
    if _PII.search(content):
        return False, "sensitive_pii"
    if _INSTRUCTION.search(content):
        return False, "instruction_injection"
    return True, None


def valid_fact_fields(
    *, polarity: str | None, scope_type: str | None, evidence_type: str | None
) -> bool:
    return (
        (polarity is None or polarity in POLARITIES)
        and (scope_type is None or scope_type in SCOPE_TYPES)
        and (evidence_type is None or evidence_type in EVIDENCE_TYPES)
    )


Polarity = Literal["positive", "negative"]
ScopeType = Literal["global", "category", "brand", "product"]
EvidenceType = Literal["explicit", "inferred", "imported"]
