"""Offline CategoryCard ETL, audit artifact generation, and index publication."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from opensearchpy import OpenSearch, helpers

from app.agent.llm import get_chat_model
from app.category.cards import build_category_card_drafts
from app.category.extractor import (
    CardExtractor,
    DeepSeekCategoryExtractor,
    PassthroughCardExtractor,
)
from app.category.normalization import load_category_aliases, normalize_dataset
from app.category.opensearch import (
    category_embedding_metadata,
    category_index_body,
    category_pipeline_body,
)
from app.category.schemas import CategoryBuildManifest, CategoryCard
from app.config import Settings, get_settings
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import EmbeddingEncoder, EmbeddingMetadata, get_embedding_encoder


def _pipeline_names(settings: Settings) -> dict[str, str]:
    return {
        "exact": settings.opensearch_category_pipeline_exact,
        "balanced": settings.opensearch_category_pipeline_balanced,
        "semantic": settings.opensearch_category_pipeline_semantic,
    }


class CategoryIndexManager:
    def __init__(
        self,
        client: OpenSearch,
        encoder: EmbeddingEncoder,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.encoder = encoder
        self.settings = settings or get_settings()

    def ensure_pipelines(self) -> None:
        specs = {
            self.settings.opensearch_category_pipeline_exact: (0.5, 0.5),
            self.settings.opensearch_category_pipeline_balanced: (0.7, 0.3),
            self.settings.opensearch_category_pipeline_semantic: (0.9, 0.1),
        }
        for name, weights in specs.items():
            self.client.transport.perform_request(
                "PUT", f"/_search/pipeline/{name}", body=category_pipeline_body(weights)
            )

    def index_cards(
        self,
        cards: list[CategoryCard],
        *,
        index_name: str,
        build_id: str,
        aliases_version: str,
        source_sha256: str,
    ) -> int:
        if self.client.indices.exists(index=index_name):
            raise ValueError(f"Category 物理索引已存在: {index_name}")
        self.client.indices.create(
            index=index_name,
            body=category_index_body(
                self.encoder.metadata,
                aliases_version=aliases_version,
                source_sha256=source_sha256,
                build_id=build_id,
                pipeline_names=_pipeline_names(self.settings),
            ),
        )
        texts = [f"{card.category}\n{card.card_type}\n{card.summary}" for card in cards]
        vectors = self.encoder.encode_documents(texts)
        actions = []
        for card, text, vector in zip(cards, texts, vectors, strict=True):
            source = card.model_dump(mode="json")
            source.update(
                {
                    "category_key": card.category,
                    "semantic_text": text,
                    "content_vector": vector,
                }
            )
            actions.append(
                {
                    "_op_type": "index",
                    "_index": index_name,
                    "_id": card.card_id,
                    "_source": source,
                }
            )
        succeeded, _ = helpers.bulk(self.client, actions, raise_on_error=True)
        self.client.indices.refresh(index=index_name)
        return succeeded

    def verify_and_publish(self, cards: list[CategoryCard], *, index_name: str) -> None:
        expected_count = len(cards)
        actual_count = self.client.count(index=index_name)["count"]
        if actual_count != expected_count:
            raise ValueError(
                f"Category 索引数量错误: expected={expected_count}, actual={actual_count}"
            )
        response = self.client.search(
            index=index_name,
            body={
                "size": 0,
                "aggs": {"card_types": {"terms": {"field": "card_type", "size": 10}}},
            },
        )
        actual_types = {
            bucket["key"]: bucket["doc_count"]
            for bucket in response["aggregations"]["card_types"]["buckets"]
        }
        expected_types = dict(sorted(Counter(card.card_type for card in cards).items()))
        if actual_types != expected_types or set(actual_types) != {
            "bestseller",
            "attribute",
            "price_range",
        }:
            raise ValueError(
                f"Category 卡片类型错误: expected={expected_types}, actual={actual_types}"
            )
        sample = self.client.search(
            index=index_name,
            body={"size": 1, "query": {"match": {"category": cards[0].category}}},
        )
        if not sample.get("hits", {}).get("hits"):
            raise ValueError("Category 索引抽样查询没有命中")

        alias = self.settings.opensearch_category_alias
        try:
            aliases = self.client.indices.get_alias(name=alias)
        except Exception:
            aliases = {}
        actions: list[dict[str, Any]] = [
            {"remove": {"index": old_index, "alias": alias}}
            for old_index in aliases
            if old_index != index_name
        ]
        if index_name not in aliases:
            actions.append({"add": {"index": index_name, "alias": alias}})
        if actions:
            self.client.indices.update_aliases(body={"actions": actions})

    def build(
        self,
        cards: list[CategoryCard],
        *,
        build_id: str,
        aliases_version: str,
        source_sha256: str,
    ) -> tuple[str, int]:
        self.ensure_pipelines()
        index_name = f"{self.settings.opensearch_category_index_prefix}-{build_id.lower()}"
        indexed = self.index_cards(
            cards,
            index_name=index_name,
            build_id=build_id,
            aliases_version=aliases_version,
            source_sha256=source_sha256,
        )
        self.verify_and_publish(cards, index_name=index_name)
        return index_name, indexed


async def build_category_artifacts(
    *,
    settings: Settings,
    extractor: CardExtractor,
    publish: bool,
    client: OpenSearch | None = None,
    encoder: EmbeddingEncoder | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    aliases = load_category_aliases(settings.category_aliases_path)
    if aliases.normalize(settings.category_source_category) != settings.category_source_category:
        raise ValueError("category_source_category 必须是别名表中的规范品类")
    normalized = normalize_dataset(
        settings.category_dataset_path,
        canonical_category=settings.category_source_category,
    )
    drafts = build_category_card_drafts(
        normalized.evidence, min_confidence=settings.category_min_confidence
    )
    rejection_reasons: Counter[str] = Counter()
    rejection_samples: list[str] = []
    cards: list[CategoryCard] = []
    for draft in drafts:
        try:
            card = await extractor.extract_card(draft)
            if card.confidence < settings.category_min_confidence:
                raise ValueError("confidence_below_threshold")
            cards.append(card)
        except Exception as exc:
            rejection_reasons[type(exc).__name__] += 1
            if len(rejection_samples) < 3:
                rejection_samples.append(f"{type(exc).__name__}: {exc}")

    if not cards:
        detail = " | ".join(rejection_samples)
        raise ValueError(f"没有卡片通过入库门禁: {detail}")
    build_id = f"{started_at:%Y%m%d%H%M%S}-{normalized.source_sha256[:8]}"
    index_name: str | None = None
    indexed = 0
    resolved_metadata = EmbeddingMetadata(
        model_id=settings.embedding_model_name,
        revision=settings.embedding_model_revision,
        dimensions=settings.embedding_dimensions,
        semantic_text_version="category-card-v1",
    )
    if publish:
        encoder = encoder or get_embedding_encoder()
        manager = CategoryIndexManager(
            client or build_opensearch_client(settings), encoder, settings
        )
        index_name, indexed = manager.build(
            cards,
            build_id=build_id,
            aliases_version=aliases.version,
            source_sha256=normalized.source_sha256,
        )
        resolved_metadata = category_embedding_metadata(encoder.metadata)

    output_dir = settings.category_build_output_dir / build_id
    output_dir.mkdir(parents=True, exist_ok=False)
    cards_path = output_dir / "category_cards.jsonl"
    cards_path.write_text(
        "\n".join(card.model_dump_json() for card in cards) + "\n", encoding="utf-8"
    )
    manifest = CategoryBuildManifest(
        build_id=build_id,
        source_path=str(settings.category_dataset_path),
        source_sha256=normalized.source_sha256,
        source_rows=normalized.source_rows,
        normalized_rows=len(normalized.evidence),
        rejected_rows=len(normalized.rejected),
        cards_generated=len(cards),
        cards_rejected=len(drafts) - len(cards),
        card_counts=dict(sorted(Counter(card.card_type for card in cards).items())),
        rejection_reasons=dict(sorted(rejection_reasons.items())),
        extractor=extractor.name,
        prompt_version=extractor.prompt_version,
        embedding_model=resolved_metadata.model_id,
        embedding_revision=resolved_metadata.revision,
        embedding_dimensions=resolved_metadata.dimensions,
        semantic_text_version=resolved_metadata.semantic_text_version,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        index_name=index_name,
    )
    manifest_path = output_dir / "audit_manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "build_id": build_id,
        "cards": len(cards),
        "card_counts": manifest.card_counts,
        "indexed": indexed,
        "index_name": index_name,
        "cards_path": str(cards_path),
        "manifest_path": str(manifest_path),
    }


async def build_default_category_index(
    *, deterministic: bool = False, publish: bool = True
) -> dict[str, Any]:
    settings = get_settings()
    extractor: CardExtractor = (
        PassthroughCardExtractor()
        if deterministic
        else DeepSeekCategoryExtractor(get_chat_model())
    )
    return await build_category_artifacts(
        settings=settings,
        extractor=extractor,
        publish=publish,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the CategoryInsight knowledge index")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="skip the paid LLM; intended only for local dry runs and tests",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="write audited card artifacts without creating or switching an index",
    )
    args = parser.parse_args()
    result = asyncio.run(
        build_default_category_index(
            deterministic=args.deterministic,
            publish=not args.no_publish,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
