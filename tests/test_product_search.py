from dataclasses import dataclass

import pytest

from app.config import Settings
from app.search.documents import flatten_attribute_terms, semantic_text
from app.search.encoder import EmbeddingMetadata, _load_local_first
from app.search.opensearch import hybrid_search_body, product_index_body, search_pipeline_body
from app.search.schemas import SearchFilters
from app.search.service import ProductSearchService, SearchNotConfiguredError


@dataclass
class FakeEncoder:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        model_id="fake/bge-m3",
        revision="abc123",
        dimensions=3,
    )

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def encode_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class FakeIndices:
    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists

    def exists(self, *, index: str) -> bool:
        return self._exists

    def get_mapping(self, *, index: str) -> dict:
        return {
            "globuy-products-v1": {
                "mappings": {
                    "_meta": {
                        "embedding_model": "fake/bge-m3",
                        "embedding_revision": "abc123",
                        "embedding_dimensions": 3,
                        "embedding_normalized": True,
                        "semantic_text_version": "product-title-stable-attrs-v1",
                    }
                }
            }
        }


class FakeClient:
    def __init__(self, *, available: bool = True, exists: bool = True) -> None:
        self.available = available
        self.indices = FakeIndices(exists=exists)
        self.last_search: dict | None = None

    def ping(self) -> bool:
        return self.available

    def search(self, **kwargs) -> dict:
        self.last_search = kwargs
        return {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "item_id": "jingdong:1",
                            "platform": "jingdong",
                            "title": "主动降噪蓝牙耳机",
                            "price": 299.0,
                            "currency": "CNY",
                            "rating": 4.8,
                            "sales": 100,
                            "image_url": "https://example.test/image.jpg",
                            "attributes": {"is_self_operated": True},
                            "product_url": "https://item.jd.com/1.html",
                            "category_key": "headphones",
                            "captured_at": "2026-08-19T00:00:00Z",
                            "semantic_hash": "not-a-public-candidate-field",
                        }
                    }
                ]
            }
        }


def settings() -> Settings:
    return Settings(
        embedding_model_name="fake/bge-m3",
        embedding_model_revision="abc123",
        embedding_dimensions=3,
        item_search_pool_floor=2,
        item_search_pool_max=6,
    )


def test_document_text_excludes_offer_fields_and_flattens_attributes() -> None:
    item = {
        "title": "旅行主动降噪耳机",
        "price": 999,
        "sales": 20,
        "attributes": {
            "brand": "示例品牌",
            "category_path": ["数码", "耳机"],
            "shop_name": "不进入语义文本",
            "is_self_operated": True,
        },
    }

    text = semantic_text(item)

    assert "旅行主动降噪耳机" in text
    assert "示例品牌" in text
    assert "数码" in text
    assert "999" not in text
    assert "不进入语义文本" not in text
    assert flatten_attribute_terms(item["attributes"]) == [
        "brand=示例品牌",
        "category_path=数码",
        "category_path=耳机",
        "is_self_operated=true",
        "shop_name=不进入语义文本",
    ]


def test_index_and_pipeline_use_lucene_cosine_and_unweighted_rrf() -> None:
    body = product_index_body(FakeEncoder().metadata)
    vector = body["mappings"]["properties"]["content_vector"]
    combination = search_pipeline_body()["phase_results_processors"][0][
        "score-ranker-processor"
    ]["combination"]

    assert vector["dimension"] == 3
    assert vector["method"] == {
        "name": "hnsw",
        "engine": "lucene",
        "space_type": "cosinesimil",
    }
    assert combination == {"technique": "rrf"}


def test_hybrid_query_prefilters_platform_and_postfilters_offer_constraints() -> None:
    body = hybrid_search_body(
        "主动降噪耳机",
        [1.0, 0.0, 0.0],
        "jingdong",
        60,
        SearchFilters(
            min_price=100,
            max_price=500,
            currency="cny",
            min_sales=20,
            attribute_equals={"is_self_operated": True},
        ),
    )
    hybrid = body["query"]["hybrid"]

    assert len(hybrid["queries"]) == 2
    assert hybrid["queries"][1]["knn"]["content_vector"]["k"] == 60
    assert hybrid["filter"]["bool"]["filter"] == [
        {"term": {"platform": "jingdong"}},
        {"term": {"is_active": True}},
    ]
    assert body["post_filter"]["bool"]["filter"] == [
        {"range": {"price": {"gte": 100.0, "lte": 500.0}}},
        {"term": {"currency": "CNY"}},
        {"range": {"sales": {"gte": 20}}},
        {"term": {"attribute_terms": "is_self_operated=true"}},
    ]


def test_hybrid_query_omits_empty_post_filter() -> None:
    body = hybrid_search_body(
        "主动降噪耳机",
        [1.0, 0.0, 0.0],
        "taobao",
        60,
    )

    assert "post_filter" not in body
    assert body["query"]["hybrid"]["filter"] == {
        "bool": {
            "filter": [
                {"term": {"platform": "taobao"}},
                {"term": {"is_active": True}},
            ]
        }
    }


def test_search_service_returns_ranked_candidates_and_pipeline_parameter() -> None:
    client = FakeClient()
    service = ProductSearchService(client, FakeEncoder(), settings())

    result = service.search("降噪耳机", "jingdong", top_k=1)

    assert result.status == "ok"
    assert result.candidates[0].retrieval_rank == 1
    assert result.candidates[0].product_url == "https://item.jd.com/1.html"
    assert "category_key" not in result.candidates[0].model_dump()
    assert client.last_search["params"] == {"search_pipeline": "globuy-products-rrf"}
    assert client.last_search["body"]["size"] == 3


def test_search_service_reports_unavailable_resources_as_not_configured() -> None:
    service = ProductSearchService(FakeClient(available=False), FakeEncoder(), settings())

    with pytest.raises(SearchNotConfiguredError, match="OpenSearch 当前不可用"):
        service.search("耳机", "taobao")


def test_search_filters_reject_inverted_price_range() -> None:
    with pytest.raises(ValueError, match="min_price"):
        SearchFilters(min_price=500, max_price=100)


def test_embedding_loader_prefers_cache_and_downloads_only_when_missing() -> None:
    calls: list[dict] = []

    def factory(model_name: str, **kwargs):
        calls.append({"model_name": model_name, **kwargs})
        if kwargs.get("local_files_only"):
            raise OSError("not cached")
        return "downloaded-model"

    model = _load_local_first(factory, "BAAI/bge-m3", revision="main", device="cpu")

    assert model == "downloaded-model"
    assert calls == [
        {
            "model_name": "BAAI/bge-m3",
            "revision": "main",
            "device": "cpu",
            "local_files_only": True,
        },
        {"model_name": "BAAI/bge-m3", "revision": "main", "device": "cpu"},
    ]
