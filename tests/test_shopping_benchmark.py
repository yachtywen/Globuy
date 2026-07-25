from app.eval.shopping_benchmark import evaluate, markdown


def test_evaluation_report_aggregates_retrieval_memory_and_provider_metrics() -> None:
    report = evaluate(
        [
            {
                "case_id": "one",
                "duration_ms": 100,
                "cache_hit": True,
                "expected_item_ids": ["a"],
                "keyword_top3": ["b"],
                "hybrid_top3": ["a"],
                "expected_memory_keys": ["budget"],
                "recalled_memory_keys": ["budget"],
                "tool_calls": [{"status": "ok"}],
                "provider_attempts": [{"platform": "jingdong", "status": "ok"}],
                "expect_cancelled": True,
                "status": "cancelled",
            }
        ]
    )
    assert report["top3"] == {"keyword_hit_rate": 0.0, "hybrid_hit_rate": 1.0}
    assert report["memory"]["recall"] == 1.0
    assert report["platforms"]["jingdong"]["success_rate"] == 1.0
    assert "Hybrid Top-3" in markdown(report)


def test_evaluation_reports_platform_retrieval_separately_from_providers() -> None:
    report = evaluate(
        [
            {
                "case_id": "retrieval",
                "platform": "douyin",
                "keyword_top3": ["a"],
                "hybrid_top3": ["a"],
                "status": "succeeded",
            }
        ]
    )

    assert report["retrieval_platforms"]["douyin"]["success_rate"] == 1.0
    assert report["platforms"] == {}
