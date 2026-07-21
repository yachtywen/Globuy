"""Human-maintained category aliases and deterministic source normalization."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.category.schemas import NormalizedEvidence

PLATFORM_ALIASES = {
    "jd": "jingdong",
    "jingdong": "jingdong",
    "taobao": "taobao",
    "douyin": "douyin",
}


@dataclass(slots=True)
class CategoryAliases:
    version: str
    canonical_to_aliases: dict[str, tuple[str, ...]]

    def normalize(self, raw: str) -> str | None:
        query = "".join(raw.strip().lower().split())
        if not query:
            return None
        candidates: list[tuple[int, str]] = []
        for canonical, aliases in self.canonical_to_aliases.items():
            for alias in aliases:
                token = "".join(alias.strip().lower().split())
                if token and token in query:
                    candidates.append((len(token), canonical))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]


@dataclass(slots=True)
class NormalizationResult:
    evidence: list[NormalizedEvidence] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    source_rows: int = 0
    source_sha256: str = ""
    snapshot_id: str = ""
    observed_at: datetime | None = None


def load_category_aliases(path: Path) -> CategoryAliases:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    categories = payload.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("品类别名表缺少 categories")
    normalized: dict[str, tuple[str, ...]] = {}
    for canonical, aliases in categories.items():
        if not isinstance(canonical, str) or not isinstance(aliases, list):
            raise ValueError("品类别名表格式错误")
        values = tuple(dict.fromkeys([canonical, *(str(item) for item in aliases)]))
        normalized[canonical] = values
    return CategoryAliases(
        version=str(payload.get("version") or "category-aliases-unknown"),
        canonical_to_aliases=normalized,
    )


def _snapshot_metadata(dataset_path: Path) -> tuple[str, datetime]:
    state_path = dataset_path.parent.parent / "state" / "collection_state.json"
    if not state_path.is_file():
        raise ValueError(f"缺少源快照时间文件: {state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    value = payload.get("updated_at")
    if not isinstance(value, str):
        raise ValueError("collection_state.json 缺少 updated_at")
    observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if observed_at.tzinfo is None:
        raise ValueError("源快照 updated_at 必须包含时区")
    snapshot_id = f"{dataset_path.parent.parent.name}:{observed_at.isoformat()}"
    return snapshot_id, observed_at


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


def normalize_dataset(
    dataset_path: Path,
    *,
    canonical_category: str,
) -> NormalizationResult:
    """Normalize one declared-category JSONL snapshot without LLM inference."""

    raw_bytes = dataset_path.read_bytes()
    result = NormalizationResult(source_sha256=hashlib.sha256(raw_bytes).hexdigest())
    result.snapshot_id, result.observed_at = _snapshot_metadata(dataset_path)

    for line_number, line in enumerate(raw_bytes.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        result.source_rows += 1
        try:
            item = json.loads(line)
            platform = PLATFORM_ALIASES.get(str(item.get("platform") or "").lower())
            if platform is None:
                raise ValueError("platform 不受支持")
            currency = str(item.get("currency") or "").upper()
            if currency != "CNY":
                raise ValueError("currency 不是 CNY")
            price = float(item["price"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError("price 必须为正有限数字")
            attributes = item.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            result.evidence.append(
                NormalizedEvidence(
                    canonical_category=canonical_category,
                    source_kind="product_snapshot",
                    source_snapshot=result.snapshot_id,
                    platform=platform,
                    item_id=str(item["item_id"]),
                    title=str(item["title"]).strip(),
                    price_cny=price,
                    rating=_optional_float(item.get("rating")),
                    sales=_optional_int(item.get("sales")),
                    attributes=attributes,
                    observed_at=result.observed_at,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            result.rejected.append(f"line {line_number}: {exc}")
    return result
