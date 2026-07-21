from app.category.opensearch import (
    category_hybrid_body,
    category_index_body,
    category_pipeline_body,
    select_retrieval_profile,
)
from app.search.encoder import EmbeddingMetadata


def test_category_index_has_separate_schema_and_build_metadata() -> None:
    body = category_index_body(
        EmbeddingMetadata("fake/bge-m3", "abc123", 3),
        aliases_version="aliases-v1",
        source_sha256="deadbeef",
        build_id="build-1",
        pipeline_names={"exact": "p1", "balanced": "p2", "semantic": "p3"},
    )
    mappings = body["mappings"]

    assert mappings["_meta"]["semantic_text_version"] == "category-card-v1"
    assert mappings["_meta"]["build_id"] == "build-1"
    assert mappings["properties"]["content_vector"]["dimension"] == 3
    assert mappings["properties"]["raw_evidence"]["index"] is False


def test_category_pipeline_uses_weighted_min_max_fusion() -> None:
    processor = category_pipeline_body((0.7, 0.3))["phase_results_processors"][0][
        "normalization-processor"
    ]

    assert processor["normalization"] == {"technique": "min_max"}
    assert processor["combination"]["technique"] == "arithmetic_mean"
    assert processor["combination"]["parameters"]["weights"] == [0.7, 0.3]


def test_category_hybrid_query_keeps_knn_first_and_filters_card_scope() -> None:
    body = category_hybrid_body(
        "耳机价格区间",
        [1.0, 0.0, 0.0],
        category_key="耳机",
        depth="quick",
        coarse_k=30,
    )
    hybrid = body["query"]["hybrid"]

    assert "knn" in hybrid["queries"][0]
    assert "multi_match" in hybrid["queries"][1]
    assert hybrid["filter"]["bool"]["filter"] == [
        {"term": {"category_key": "耳机"}},
        {"terms": {"card_type": ["bestseller", "price_range"]}},
    ]


def test_retrieval_profile_is_query_sensitive() -> None:
    assert select_retrieval_profile("耳机", "耳机") == "exact"
    assert select_retrieval_profile("蓝牙耳机", "耳机") == "balanced"
    assert select_retrieval_profile("适合送礼的耳机", "耳机") == "semantic_only"
    assert select_retrieval_profile("适合运动的耳机", "耳机") == "semantic"
