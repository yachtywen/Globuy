"""Collect auditable BM25 and hybrid retrieval records from the product index.

This command does not call shopping providers or a language model. Relevance labels
are derived from explicit title anchors over the real documents already indexed from
MySQL, so every expected item can be inspected and reproduced.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensearchpy import OpenSearch

from app.config import Settings, get_settings
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import EmbeddingEncoder, get_embedding_encoder
from app.search.schemas import Platform
from app.search.service import ProductSearchService


@dataclass(frozen=True, slots=True)
class QueryFamily:
    key: str
    query: str
    title_anchors: tuple[str, ...]


QUERY_FAMILIES = (
    QueryFamily("noise-cancelling-headphones", "主动降噪耳机", ("降噪", "耳机")),
    QueryFamily("wireless-headphones", "无线蓝牙耳机", ("无线", "蓝牙", "耳机")),
    QueryFamily("in-ear-headphones", "入耳式耳机", ("入耳", "耳机")),
    QueryFamily("over-ear-headphones", "头戴式耳机", ("头戴", "耳机")),
    QueryFamily("sports-headphones", "运动耳机", ("运动", "耳机")),
    QueryFamily("long-battery-headphones", "长续航耳机", ("续航", "耳机")),
    QueryFamily("mechanical-keyboard", "机械键盘", ("机械", "键盘")),
    QueryFamily("tri-mode-keyboard", "三模机械键盘", ("三模", "机械", "键盘")),
    QueryFamily("wireless-keyboard", "无线机械键盘", ("无线", "机械", "键盘")),
    QueryFamily("hot-swap-keyboard", "热插拔键盘", ("热插拔", "键盘")),
)
PLATFORMS: tuple[Platform, ...] = ("taobao", "jingdong", "douyin")


def _keyword_top3(
    client: OpenSearch, settings: Settings, query: str, platform: Platform
) -> list[str]:
    response = client.search(
        index=settings.opensearch_product_alias,
        body={
            "size": 3,
            "_source": ["item_id"],
            "query": {
                "bool": {
                    "must": [{"match": {"title": {"query": query}}}],
                    "filter": [{"term": {"platform": platform}}],
                }
            },
        },
    )
    return [str(hit["_source"]["item_id"]) for hit in response["hits"]["hits"]]


def _expected_item_ids(
    documents: Iterable[dict[str, Any]], family: QueryFamily, platform: Platform
) -> list[str]:
    anchors = tuple(anchor.casefold() for anchor in family.title_anchors)
    return sorted(
        str(document["item_id"])
        for document in documents
        if document.get("platform") == platform
        and all(anchor in str(document.get("title", "")).casefold() for anchor in anchors)
    )


def _load_documents(client: OpenSearch, settings: Settings) -> list[dict[str, Any]]:
    response = client.search(
        index=settings.opensearch_product_alias,
        body={
            "size": 10_000,
            "sort": [{"item_id": "asc"}],
            "_source": ["item_id", "platform", "title"],
            "query": {"match_all": {}},
        },
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


def collect_records(
    client: OpenSearch,
    encoder: EmbeddingEncoder,
    settings: Settings,
) -> list[dict[str, Any]]:
    documents = _load_documents(client, settings)
    service = ProductSearchService(client, encoder, settings)
    records: list[dict[str, Any]] = []
    for family in QUERY_FAMILIES:
        for platform in PLATFORMS:
            started = time.perf_counter()
            keyword_top3 = _keyword_top3(client, settings, family.query, platform)
            hybrid = service.search(family.query, platform, top_k=3)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            records.append(
                {
                    "case_id": f"{family.key}-{platform}",
                    "query": family.query,
                    "platform": platform,
                    "label_method": "all-title-anchors",
                    "title_anchors": list(family.title_anchors),
                    "duration_ms": duration_ms,
                    "cache_hit": False,
                    "expected_item_ids": _expected_item_ids(documents, family, platform),
                    "keyword_top3": keyword_top3,
                    "hybrid_top3": [item.item_id for item in hybrid.candidates],
                    "tool_calls": [{"name": "item_search", "status": hybrid.status}],
                    "status": "succeeded" if hybrid.status == "ok" else "failed",
                }
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/eval/records.jsonl"),
        help="destination JSONL file",
    )
    args = parser.parse_args()
    settings = get_settings()
    records = collect_records(
        build_opensearch_client(settings), get_embedding_encoder(), settings
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    labelled = sum(bool(record["expected_item_ids"]) for record in records)
    print(json.dumps({"records": len(records), "labelled": labelled}, ensure_ascii=False))


if __name__ == "__main__":
    main()
