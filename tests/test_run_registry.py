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
