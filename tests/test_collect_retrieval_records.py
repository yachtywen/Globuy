from app.eval.collect_retrieval_records import QueryFamily, _expected_item_ids


def test_expected_item_ids_require_every_auditable_title_anchor() -> None:
    documents = [
        {"item_id": "jingdong:1", "platform": "jingdong", "title": "无线蓝牙耳机"},
        {"item_id": "jingdong:2", "platform": "jingdong", "title": "无线机械键盘"},
        {"item_id": "taobao:1", "platform": "taobao", "title": "无线蓝牙耳机"},
    ]
    family = QueryFamily("wireless", "无线蓝牙耳机", ("无线", "蓝牙", "耳机"))

    assert _expected_item_ids(documents, family, "jingdong") == ["jingdong:1"]


def test_query_matrix_contains_exactly_thirty_cases() -> None:
    from app.eval.collect_retrieval_records import PLATFORMS, QUERY_FAMILIES

    assert len(QUERY_FAMILIES) * len(PLATFORMS) == 30
