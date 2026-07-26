from pathlib import Path

from app.api.run_registry import RunRegistry


def test_final_result_preserves_state_memory_candidates(tmp_path: Path) -> None:
    registry = object.__new__(RunRegistry)
    registry.product_image_catalog_path = tmp_path / "missing-catalog.jsonl"
    candidate = {
        "key": "budget_max_cny",
        "category": "preference",
        "content": "购物预算不超过 500 元",
        "confidence": 1.0,
    }
    state = {
        "phase": "done",
        "iteration": 2,
        "learned_preferences": [candidate],
        "terminal_result": {
            "status": "complete",
            "final_text": "已完成推荐。",
            "picks": [],
            "unresolved": [],
            "learned_preferences": [],
        },
    }

    _, result, _ = registry._final_result(state, "", {"streaming": True})

    assert result["learned_preferences"] == [candidate]


def test_final_result_preserves_product_review_sources(tmp_path: Path) -> None:
    registry = object.__new__(RunRegistry)
    registry.product_image_catalog_path = tmp_path / "missing-catalog.jsonl"
    review = {
        "rank": 1,
        "source": "小红书",
        "title": "商品体验",
        "url": "https://www.xiaohongshu.com/explore/1",
        "content": "真实使用摘要",
        "score": 0.9,
        "published_date": None,
    }
    state = {
        "phase": "done",
        "iteration": 1,
        "terminal_result": {
            "status": "incomplete",
            "final_text": "找到一条测评。",
            "source_kind": "content_review",
            "review_results": [review],
            "discarded_irrelevant_count": 2,
            "picks": [],
            "unresolved": ["仅找到一条"],
        },
    }

    _, result, _ = registry._final_result(state, "", {"streaming": True})

    assert result["status"] == "incomplete"
    assert result["source_kind"] == "content_review"
    assert result["review_results"] == [review]
    assert result["discarded_irrelevant_count"] == 2
