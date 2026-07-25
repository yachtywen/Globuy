"""Product indexing and hybrid-search application services."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from opensearchpy import OpenSearch, helpers
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database.models import Offer, OutboxEvent, Product
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.search.documents import index_document, semantic_text
from app.search.encoder import EmbeddingEncoder, EmbeddingMetadata, get_embedding_encoder
from app.search.opensearch import hybrid_search_body, product_index_body, search_pipeline_body
from app.search.schemas import Candidate, ItemSearchOutput, Platform, SearchFilters


class SearchNotConfiguredError(RuntimeError):
    """Raised when local model or OpenSearch product resources are unavailable."""


def _mapping_metadata(mapping: dict[str, Any]) -> dict[str, Any] | None:
    for value in mapping.values():
        meta = value.get("mappings", {}).get("_meta")
        if isinstance(meta, dict):
            return meta
    return None


def _metadata_matches(actual: dict[str, Any] | None, expected: EmbeddingMetadata) -> bool:
    return bool(
        actual
        and actual.get("embedding_model") == expected.model_id
        and actual.get("embedding_revision") == expected.revision
        and actual.get("embedding_dimensions") == expected.dimensions
        and actual.get("embedding_normalized") is expected.normalized
        and actual.get("semantic_text_version") == expected.semantic_text_version
    )


class ProductIndexManager:
    def __init__(
        self,
        client: OpenSearch,
        encoder: EmbeddingEncoder,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.encoder = encoder
        self.settings = settings or get_settings()

    def ensure_pipeline(self) -> None:
        self.client.transport.perform_request(
            "PUT",
            f"/_search/pipeline/{self.settings.opensearch_product_pipeline}",
            body=search_pipeline_body(),
        )

    def ensure_index(self) -> None:
        index = self.settings.opensearch_product_index
        if not self.client.indices.exists(index=index):
            self.client.indices.create(index=index, body=product_index_body(self.encoder.metadata))

    def index_items(self, items: list[dict[str, Any]]) -> int:
        texts = [semantic_text(item) for item in items]
        vectors = self.encoder.encode_documents(texts)
        actions = [
            {
                "_op_type": "index",
                "_index": self.settings.opensearch_product_index,
                "_id": item["item_id"],
                "_source": index_document(item, vector),
            }
            for item, vector in zip(items, vectors, strict=True)
        ]
        succeeded, _ = helpers.bulk(self.client, actions, raise_on_error=True)
        self.client.indices.refresh(index=self.settings.opensearch_product_index)
        return succeeded

    def verify_and_publish(self, expected_counts: dict[str, int]) -> None:
        index = self.settings.opensearch_product_index
        count = self.client.count(index=index)["count"]
        if count != sum(expected_counts.values()):
            expected_total = sum(expected_counts.values())
            raise ValueError(f"商品索引数量错误: expected={expected_total}, actual={count}")
        response = self.client.search(
            index=index,
            body={
                "size": 0,
                "aggs": {"platforms": {"terms": {"field": "platform", "size": 10}}},
            },
        )
        actual_counts = {
            bucket["key"]: bucket["doc_count"]
            for bucket in response["aggregations"]["platforms"]["buckets"]
        }
        if actual_counts != expected_counts:
            raise ValueError(f"平台数量错误: expected={expected_counts}, actual={actual_counts}")

        alias = self.settings.opensearch_product_alias
        actions: list[dict[str, Any]] = []
        try:
            aliases = self.client.indices.get_alias(name=alias)
        except Exception:
            aliases = {}
        for old_index in aliases:
            if old_index != index:
                actions.append({"remove": {"index": old_index, "alias": alias}})
        if index not in aliases:
            actions.append({"add": {"index": index, "alias": alias}})
        if actions:
            self.client.indices.update_aliases(body={"actions": actions})

    def build(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        counts = dict(sorted(Counter(item["platform"] for item in items).items()))
        self.ensure_pipeline()
        self.ensure_index()
        indexed = self.index_items(items)
        self.verify_and_publish(counts)
        return {"indexed": indexed, "platform_counts": counts}


class ProductSearchService:
    def __init__(
        self,
        client: OpenSearch,
        encoder: EmbeddingEncoder,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.encoder = encoder
        self.settings = settings or get_settings()
        self._validated = False

    def _ensure_ready(self) -> None:
        if self._validated:
            return
        try:
            if not self.client.ping():
                raise SearchNotConfiguredError("OpenSearch 当前不可用")
            alias = self.settings.opensearch_product_alias
            if not self.client.indices.exists(index=alias):
                raise SearchNotConfiguredError("OpenSearch 商品索引尚未构建")
            mapping = self.client.indices.get_mapping(index=alias)
        except SearchNotConfiguredError:
            raise
        except Exception as exc:
            raise SearchNotConfiguredError(f"无法检查 OpenSearch 商品索引: {exc}") from exc
        if not _metadata_matches(_mapping_metadata(mapping), self.encoder.metadata):
            raise SearchNotConfiguredError("商品索引与当前 Embedding 模型元数据不一致")
        self._validated = True

    def search(
        self,
        query: str,
        platform: Platform,
        top_k: int = 20,
        filters: SearchFilters | None = None,
    ) -> ItemSearchOutput:
        self._ensure_ready()
        pool_size = min(
            self.settings.item_search_pool_max,
            max(self.settings.item_search_pool_floor, top_k * 3),
        )
        body = hybrid_search_body(
            query,
            self.encoder.encode_query(query),
            platform,
            pool_size,
            filters,
        )
        response = self.client.search(
            index=self.settings.opensearch_product_alias,
            params={"search_pipeline": self.settings.opensearch_product_pipeline},
            body=body,
        )
        hits = response.get("hits", {}).get("hits", [])
        candidates = [
            Candidate.model_validate({**hit["_source"], "retrieval_rank": rank})
            for rank, hit in enumerate(hits[:top_k], start=1)
        ]
        return ItemSearchOutput(
            status="ok",
            platform=platform,
            candidates=candidates,
            total_recall=len(hits),
            truncated=len(hits) > len(candidates),
        )


def load_catalog(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def load_mysql_catalog(database: Database) -> list[dict[str, Any]]:
    """Project active, priced MySQL offers into the stable search document input."""

    async with database.sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(Product, Offer)
                    .join(Offer, Offer.product_id == Product.product_id)
                    .where(
                        Product.status == "active",
                        Offer.is_active.is_(True),
                        Offer.current_price.is_not(None),
                    )
                    .order_by(Offer.platform, Offer.source_item_id, Offer.offer_id)
                )
            ).all()
        )
    return [catalog_item(product, offer) for product, offer in rows]


def catalog_item(product: Product, offer: Offer) -> dict[str, Any]:
    return {
        "item_id": f"{offer.platform}:{offer.source_item_id}",
        "product_id": product.product_id,
        "offer_id": offer.offer_id,
        "platform": offer.platform,
        "title": product.title,
        "price": float(offer.current_price) if offer.current_price is not None else None,
        "currency": offer.currency,
        "rating": float(offer.rating_value) if offer.rating_value is not None else None,
        "sales": offer.sales_value,
        "image_url": offer.image_url,
        "attributes": product.attributes_json or {},
        "product_url": offer.product_url,
        "wishlist_eligible": True,
    }


async def _mark_product_outbox_published(database: Database) -> None:
    from app.auth.service import utc_naive

    async with database.sessions.begin() as session:
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_type == "product",
                        OutboxEvent.published_at.is_(None),
                    )
                )
            ).all()
        )
        for event in events:
            event.published_at = utc_naive()
            event.attempts += 1
            event.last_error_code = None


async def _build_default_index() -> dict[str, Any]:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required to build the product index")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    try:
        items = await load_mysql_catalog(database)
        if not items:
            raise RuntimeError("MySQL has no active priced offers to index")
        manager = ProductIndexManager(
            build_opensearch_client(settings), get_embedding_encoder(), settings
        )
        result = manager.build(items)
        await _mark_product_outbox_published(database)
        return result
    finally:
        await database.close()


def build_default_index() -> dict[str, Any]:
    return asyncio.run(_build_default_index())
