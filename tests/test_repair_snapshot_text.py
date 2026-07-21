import json
from pathlib import Path

from datasets.justone_headphones.repair_snapshot_text import repair_rows


def _write_raw(root: Path, platform: str, payload: dict) -> None:
    target = root / platform / "search"
    target.mkdir(parents=True)
    (target / "response.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_repair_rows_restores_title_and_attributes_from_raw(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        "jd",
        {
            "code": 0,
            "data": {
                "products": [
                    {
                        "id": "20001",
                        "title": "主动降噪蓝牙耳机",
                        "price": 299,
                        "imageUrl": "//example.com/a.jpg",
                        "shopName": "京东耳机店",
                    }
                ]
            },
        },
    )
    rows = [
        {
            "item_id": "jd:20001",
            "title": "涓诲姩闄嶅櫔",
            "attributes": {"shop_name": "浜东"},
            "price": 299,
        }
    ]

    repaired, report = repair_rows(rows, tmp_path)

    assert report.complete is True
    assert repaired[0]["title"] == "主动降噪蓝牙耳机"
    assert repaired[0]["attributes"]["shop_name"] == "京东耳机店"
    assert repaired[0]["price"] == 299


def test_repair_rows_reports_missing_ids_without_partial_changes(tmp_path: Path) -> None:
    rows = [{"item_id": "taobao:missing", "title": "乱码", "attributes": {}}]

    repaired, report = repair_rows(rows, tmp_path)

    assert repaired == rows
    assert report.complete is False
    assert report.missing_item_ids == ("taobao:missing",)
