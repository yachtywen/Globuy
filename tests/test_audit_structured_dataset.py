from datasets.justone_headphones.audit_structured_dataset import (
    catalog_labels,
    price_band,
)


def test_price_bands_cover_low_to_premium_ranges() -> None:
    assert price_band(99.99) == "low_under_100"
    assert price_band(100) == "budget_100_to_299"
    assert price_band(300) == "mid_300_to_999"
    assert price_band(1000) == "high_1000_to_2999"
    assert price_band(3000) == "premium_3000_plus"


def test_catalog_labels_detect_multiple_headphone_dimensions() -> None:
    labels = catalog_labels({"title": "高端头戴式蓝牙主动降噪游戏耳机"})

    assert labels == {
        "over_ear",
        "bluetooth_or_wireless",
        "noise_cancelling",
        "gaming",
    }


def test_catalog_labels_detect_open_ear_and_wired_products() -> None:
    labels = catalog_labels({"title": "开放式骨传导有线挂耳耳机"})

    assert labels == {"open_ear_or_bone_conduction", "wired"}
