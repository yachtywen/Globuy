import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.server import _allowed_product_image_url, create_app
from app.config import Settings
from app.products.identity import offer_id, product_id
from app.products.realtime import _jd_image_url
from app.search.catalog_images import enrich_product_images, enrich_task_result


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


def test_jd_jfs_image_paths_are_normalized_to_https() -> None:
    assert _jd_image_url("jfs/t1/example.jpg") == (
        "https://img10.360buyimg.com/n1/jfs/t1/example.jpg"
    )
    assert _jd_image_url("//img10.360buyimg.com/n1/jfs/example.jpg") == (
        "https://img10.360buyimg.com/n1/jfs/example.jpg"
    )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://img10.360buyimg.com/n1/jfs/example.jpg", True),
        ("https://img.alicdn.com/example.jpg", True),
        ("http://img10.360buyimg.com/n1/jfs/example.jpg", False),
        ("https://360buyimg.com.attacker.example/image.jpg", False),
        ("https://example.com/image.jpg", False),
    ],
)
def test_product_image_proxy_allowlist(url: str, allowed: bool) -> None:
    assert _allowed_product_image_url(url) is allowed


def test_product_image_proxy_rejects_non_image_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "text/html"}, text="no")
    )
    monkeypatch.setattr(
        "app.api.server.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )
    app = create_app(
        settings=Settings(
            database_url=None,
            legacy_sqlite_enabled=True,
            session_db_path=tmp_path / "sessions.sqlite3",
            output_dir=tmp_path / "output",
            uploaded_dir=tmp_path / "uploaded",
            model_provider="mock",
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/product-image",
            params={"image_url": "https://img10.360buyimg.com/n1/jfs/example.jpg"},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PRODUCT_IMAGE_UNAVAILABLE"
