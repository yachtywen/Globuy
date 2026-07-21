import json
from pathlib import Path

from app.search.catalog_images import enrich_product_images, enrich_task_result
from app.products.identity import offer_id, product_id


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "headphones.jsonl"
    path.write_text(
        json.dumps(
            {
                "item_id": "jd:10184615087415",
                "source_item_id": "10184615087415",
                "platform": "jd",
                "image_url": "https://img10.360buyimg.com/example/q45.jpg",
                "source_url": "https://item.jd.com/10184615087415.html",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_enrich_product_images_matches_jd_and_jingdong_item_ids(tmp_path: Path) -> None:
    result = enrich_product_images(
        [
            {
                "item_id": "jingdong:10184615087415",
                "platform": "jingdong",
                "title": "声阔 Space Q45",
                "image_url": None,
            }
        ],
        _catalog(tmp_path),
    )

    assert result[0]["image_url"] == "https://img10.360buyimg.com/example/q45.jpg"
    assert result[0]["product_id"] == product_id("jingdong:10184615087415")
    assert result[0]["offer_id"] == offer_id("jingdong:10184615087415")


def test_enrichment_preserves_existing_image_and_unknown_products(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    result = enrich_product_images(
        [
            {
                "item_id": "jingdong:10184615087415",
                "platform": "jingdong",
                "image_url": "https://cdn.example.com/kept.jpg",
            },
            {"item_id": "jingdong:unknown", "platform": "jingdong"},
        ],
        catalog,
    )

    assert result[0]["image_url"] == "https://cdn.example.com/kept.jpg"
    assert "image_url" not in result[1]


def test_enrich_task_result_repairs_legacy_picks_without_mutating_input(
    tmp_path: Path,
) -> None:
    original = {
        "status": "complete",
        "picks": [
            {
                "item_id": "jingdong:10184615087415",
                "platform": "jingdong",
                "image_url": None,
            }
        ],
    }

    enriched = enrich_task_result(original, _catalog(tmp_path))

    assert original["picks"][0]["image_url"] is None
    assert enriched is not None
    assert enriched["picks"][0]["image_url"].endswith("/q45.jpg")
    assert enriched["picks"][0]["offer_id"] == offer_id(
        "jingdong:10184615087415"
    )
