import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.category.build_index import build_category_artifacts
from app.category.cards import build_category_card_drafts
from app.category.extractor import PassthroughCardExtractor
from app.category.normalization import load_category_aliases, normalize_dataset
from app.category.schemas import CategoryCard
from app.config import Settings


def _write_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    structured = root / "structured"
    state = root / "state"
    structured.mkdir(parents=True)
    state.mkdir()
    (state / "collection_state.json").write_text(
        json.dumps({"updated_at": "2026-01-02T03:04:05+00:00"}),
        encoding="utf-8",
    )
    rows = [
        {
            "item_id": f"jd:{index}",
            "platform": "jd",
            "title": title,
            "price": price,
            "currency": "CNY",
            "rating": 4.5 + index / 100,
            "sales": 1000 - index,
            "attributes": {"连接方式": "蓝牙", "功能": "主动降噪"},
        }
        for index, (title, price) in enumerate(
            [
                ("蓝牙主动降噪入耳式耳机", 99),
                ("蓝牙主动降噪头戴式耳机", 199),
                ("无线主动降噪入耳式耳机", 299),
                ("无线主动降噪头戴式耳机", 499),
                ("蓝牙主动降噪运动耳机", 799),
                ("无线主动降噪游戏耳机", 1299),
            ]
        )
    ]
    dataset = structured / "items.jsonl"
    dataset.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return dataset


def test_alias_normalization_is_human_controlled() -> None:
    aliases = load_category_aliases(Path("app/category/category_aliases.yml"))

    assert aliases.normalize("帮我看看降噪耳机") == "耳机"
    assert aliases.normalize("洗衣机") is None


def test_normalization_and_card_drafts_are_auditable(tmp_path: Path) -> None:
    dataset = _write_snapshot(tmp_path)
    normalized = normalize_dataset(dataset, canonical_category="耳机")
    cards = build_category_card_drafts(normalized.evidence, min_confidence=0.5)

    assert normalized.source_rows == 6
    assert normalized.rejected == []
    assert {item.platform for item in normalized.evidence} == {"jingdong"}
    assert {card.card_type for card in cards} == {
        "bestseller",
        "attribute",
        "price_range",
    }
    assert all(1 <= len(card.raw_evidence) <= 3 for card in cards)
    assert all(card.last_updated == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC) for card in cards)


def test_category_card_rejects_future_or_extra_fields() -> None:
    with pytest.raises(ValueError):
        CategoryCard.model_validate(
            {
                "card_id": "future",
                "category": "耳机",
                "card_type": "bestseller",
                "summary": "test",
                "raw_evidence": ["source"],
                "last_updated": "2999-01-01T00:00:00+00:00",
                "confidence": 1,
                "unknown": True,
            }
        )


@pytest.mark.asyncio
async def test_deterministic_build_writes_cards_and_manifest_without_index(
    tmp_path: Path,
) -> None:
    dataset = _write_snapshot(tmp_path)
    settings = Settings(
        _env_file=None,
        category_dataset_path=dataset,
        category_build_output_dir=tmp_path / "output",
        category_aliases_path=Path("app/category/category_aliases.yml"),
        category_source_category="耳机",
    )

    result = await build_category_artifacts(
        settings=settings,
        extractor=PassthroughCardExtractor(),
        publish=False,
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert result["indexed"] == 0
    assert result["index_name"] is None
    assert manifest["extractor"] == "deterministic-passthrough"
    assert manifest["source_rows"] == 6
    assert manifest["cards_generated"] == result["cards"]
