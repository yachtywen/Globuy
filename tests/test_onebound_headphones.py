import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from datasets.onebound_headphones.collector import (
    AuthenticationError,
    BudgetExceeded,
    CollectionConfig,
    OneBoundClient,
    OneBoundCollector,
    RequestLedger,
    classify_api_error,
    detail_attributes,
    dry_run,
    normalize_search_item,
    redact,
    search_items,
)

FIXTURES = Path("datasets/onebound_headphones/fixtures")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("platform", "filename", "expected_id"),
    [("taobao", "taobao_search.json", "taobao:10001"), ("jd", "jd_search.json", "jd:20001")],
)
def test_search_mapping_filters_accessories(platform: str, filename: str, expected_id: str) -> None:
    items, _ = search_items(fixture(filename))
    normalized = [
        normalize_search_item(
            platform, item, keyword="耳机", page=1, captured_at="2026-07-17T00:00:00+00:00"
        )
        for item in items
    ]
    accepted = [item for item in normalized if item is not None]
    assert [item["item_id"] for item in accepted] == [expected_id]
    assert accepted[0]["currency"] == "CNY"
    assert accepted[0]["rating"] is None


def test_detail_mapping_keeps_only_returned_attributes() -> None:
    attributes = detail_attributes(fixture("taobao_detail.json"))
    assert attributes["brand"] == "示例品牌"
    assert attributes["佩戴方式"] == "头戴式"
    assert attributes["sku_count"] == 2


def test_redaction_removes_credentials_recursively() -> None:
    source = {
        "key": "sensitive-key",
        "nested": {"secret": "sensitive-secret"},
        "url": "https://example.test/?key=sensitive-key&secret=sensitive-secret&q=耳机",
        "provider_error": "Key[sensitive-key] 已超量",
    }
    encoded = json.dumps(redact(source), ensure_ascii=False)
    assert "sensitive-key" not in encoded
    assert "sensitive-secret" not in encoded
    assert encoded.count("[REDACTED]") == 5


def test_provider_error_classification_does_not_need_credential_value() -> None:
    assert (
        classify_api_error({"error_code": "4005", "reason": "Key[REDACTED]已超量"})
        == "quota_exceeded"
    )
    assert (
        classify_api_error({"error_code": "4005", "reason": "jd无权访问, 请开通接口"})
        == "interface_not_enabled"
    )
    assert (
        classify_api_error({"error_code": "4013", "reason": "Key[REDACTED]已超量"})
        == "quota_exceeded"
    )


def test_ledger_reserves_before_call_and_blocks_budget(tmp_path: Path) -> None:
    config = CollectionConfig(
        root=tmp_path,
        max_search_calls=1,
        max_detail_calls=0,
        budget_cny=Decimal("0.022"),
    )
    ledger = RequestLedger(tmp_path / "ledger.jsonl", config)
    ledger.reserve("first", "taobao", "item_search", {"q": "耳机", "page": 1}, Decimal("0.022"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", "jd", "item_search", {"q": "耳机", "page": 1}, Decimal("0.022"))
    assert ledger.search_calls == 1


def test_client_reuses_raw_response_without_second_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=fixture("taobao_search.json"))

    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = OneBoundClient(
        "test-key",
        "test-secret",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
    )
    try:
        params = {"q": "耳机", "page": 1, "page_size": 40}
        first = client.call("taobao", "item_search", params)
        second = client.call("taobao", "item_search", params)
    finally:
        client.close()
    assert first == second
    assert calls == 1
    persisted = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "test-key" not in persisted
    assert "test-secret" not in persisted


def test_client_skips_cached_api_error_without_second_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error_code": "5000", "reason": "data error"})

    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = OneBoundClient(
        "test-key",
        "test-secret",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
    )
    try:
        params = {"q": "耳机", "page": 7, "page_size": 40}
        assert client.call("taobao", "item_search", params) is None
        assert client.call("taobao", "item_search", params) is None
    finally:
        client.close()
    assert calls == 1
    assert ledger.summary()["failed_calls"] == 1
    assert ledger.summary()["error_codes"] == {"5000": 1}


def test_cached_permission_error_retries_once_only_when_explicit(tmp_path: Path) -> None:
    calls = 0
    sent_retry_parameter = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, sent_retry_parameter
        calls += 1
        sent_retry_parameter = sent_retry_parameter or "_provider_retry" in request.url.params
        if calls == 1:
            return httpx.Response(200, json={"error_code": "4005", "reason": "请开通接口"})
        return httpx.Response(200, json=fixture("jd_search.json"))

    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    params = {"q": "耳机", "page": 1, "page_size": 40}
    first_client = OneBoundClient(
        "test-key",
        "test-secret",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AuthenticationError):
            first_client.call("jd", "item_search", params)
    finally:
        first_client.close()

    retry_client = OneBoundClient(
        "test-key",
        "test-secret",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
        retry_cached_provider_errors=True,
    )
    try:
        first_retry = retry_client.call("jd", "item_search", params)
        cached_retry = retry_client.call("jd", "item_search", params)
    finally:
        retry_client.close()
    assert first_retry == cached_retry
    assert calls == 2
    assert ledger.search_calls == 2
    assert not sent_retry_parameter


def test_dry_run_cost_matches_hard_cap() -> None:
    plan = dry_run(CollectionConfig())
    assert plan["max_search_calls"] == 70
    assert plan["max_detail_calls"] == 20
    assert Decimal(plan["calculated_maximum_cny"]) <= Decimal(plan["budget_cny"])


def test_collector_runs_balanced_end_to_end_with_mock_transport(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        platform = "taobao" if "/taobao/" in request.url.path else "jd"
        if request.url.path.endswith("/item_search/"):
            if platform == "jd":
                assert request.url.params["cat"] == "0"
                assert request.url.params["seller_info"] == "no"
            page = int(request.url.params["page"])
            prefix = "1" if platform == "taobao" else "2"
            return httpx.Response(
                200,
                json={
                    "items": {
                        "page": page,
                        "page_size": 1,
                        "pagecount": 2,
                        "item": [
                            {
                                "title": f"{platform} 第{page}款蓝牙耳机",
                                "pic_url": "//img.example.com/headphone.jpg",
                                "price": str(100 + page),
                                "sales": page,
                                "num_iid": f"{prefix}000{page}",
                                "detail_url": f"https://example.com/{prefix}000{page}",
                            }
                        ],
                    },
                    "error_code": "0000",
                },
            )
        detail = fixture(f"{platform}_detail.json")
        return httpx.Response(200, json=detail)

    config = CollectionConfig(
        root=tmp_path,
        target_per_platform=2,
        minimum_per_platform=2,
        detail_per_platform=1,
        max_search_calls=4,
        max_detail_calls=2,
        budget_cny=Decimal("0.20"),
        request_interval_seconds=0,
    )
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = OneBoundClient(
        "test-key",
        "test-secret",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
    )
    try:
        manifest = OneBoundCollector(client, config).run()
    finally:
        client.close()
    assert manifest["status"] == "complete"
    assert manifest["counts"] == {"taobao": 2, "jd": 2}
    assert manifest["detail_counts"] == {"taobao": 1, "jd": 1}
    assert manifest["requests"]["search_calls"] == 4
    assert manifest["provider_failures"] == []
    quality = json.loads(
        (tmp_path / "reports" / "quality_report.json").read_text(encoding="utf-8")
    )
    assert quality["raw_search_audit"]["successful_responses"] == 4
    assert quality["raw_search_audit"]["duplicate_occurrences"] == 0
    assert (tmp_path / "normalized" / "headphones.jsonl").exists()
