from datasets.justone_headphones.structure_itemsearch_dataset import structure_candidate


def test_taobao_candidate_keeps_only_schema_fields_and_relevant_attributes() -> None:
    result = structure_candidate(
        {
            "platform": "taobao",
            "source_item_id": "1001",
            "title": "无线蓝牙耳机",
            "price": 99.0,
            "currency": "CNY",
            "rating": 4.9,
            "sales": 200,
            "image_url": "https://example.com/image.jpg",
            "source_url": "https://example.com/item",
            "captured_at": "2026-07-18T00:00:00+00:00",
            "attributes": {
                "spu_id": "spu-1",
                "shop_id": "shop-1",
                "shop_name": "示例店",
                "seller_location": "杭州",
                "seller_level": 20,
                "tags": ["蓝牙"],
            },
        }
    )

    assert list(result) == [
        "item_id",
        "platform",
        "title",
        "price",
        "currency",
        "rating",
        "sales",
        "image_url",
        "attributes",
        "product_url",
    ]
    assert result["item_id"] == "taobao:1001"
    assert result["product_url"] == "https://example.com/item"
    assert result["attributes"] == {
        "spu_id": "spu-1",
        "shop_id": "shop-1",
        "shop_name": "示例店",
        "seller_location": "杭州",
        "tags": ["蓝牙"],
    }


def test_jd_is_mapped_to_jingdong_and_price_duplicates_are_removed() -> None:
    result = structure_candidate(
        {
            "platform": "jd",
            "source_item_id": "2001",
            "title": "电脑耳机",
            "price": 128.0,
            "currency": "CNY",
            "rating": None,
            "sales": None,
            "image_url": None,
            "attributes": {
                "shop_name": "京东示例店",
                "category_1": "652",
                "category_2": "828",
                "category_3": "31012",
                "lowest_price": 99.0,
                "month_sales": 300,
            },
        }
    )

    assert result["item_id"] == "jingdong:2001"
    assert result["platform"] == "jingdong"
    assert result["attributes"] == {
        "shop_name": "京东示例店",
        "category_ids": ["652", "828", "31012"],
    }


def test_douyin_keeps_category_and_rating_semantics_without_promotion_state() -> None:
    result = structure_candidate(
        {
            "platform": "douyin",
            "source_item_id": "3001",
            "title": "开放式耳机",
            "price": 199.0,
            "currency": "CNY",
            "rating": 96.5,
            "sales": 500,
            "image_url": "https://example.com/douyin.jpg",
            "attributes": {
                "shop_id": "shop-3",
                "category_path": ["3C数码", "耳机"],
                "tag_codes": ["spot_goods"],
                "rating_type": "good_ratio_percent",
                "promotion_status": 2,
                "regular_price": 299.0,
            },
        }
    )

    assert result["platform"] == "douyin"
    assert result["attributes"] == {
        "shop_id": "shop-3",
        "category_path": ["3C数码", "耳机"],
        "tags": ["spot_goods"],
        "rating_type": "good_ratio_percent",
    }
