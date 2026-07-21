import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from datasets.justone_headphones.collector import (
    BudgetExceeded,
    CollectionConfig,
    JustOneClient,
    JustOneCollector,
    ProviderResponseError,
    RequestLedger,
    dry_run,
    normalize_search_item,
    redact,
    search_items,
)

FIXTURES = Path("datasets/justone_headphones/fixtures")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("platform", "filename", "expected_id"),
    [
        ("taobao", "taobao_search.json", "taobao:10001"),
        ("jd", "jd_search.json", "jd:20001"),
        ("douyin", "douyin_search.json", "douyin:30001"),
    ],
)
def test_search_mapping_filters_accessories(
    platform: str, filename: str, expected_id: str
) -> None:
    items, _ = search_items(platform, fixture(filename))
    normalized = [
        normalize_search_item(
            platform,
            item,
            keyword="耳机",
            page=1,
            captured_at="2026-07-17T00:00:00+00:00",
        )
        for item in items
    ]
    accepted = [item for item in normalized if item is not None]
    assert [item["item_id"] for item in accepted] == [expected_id]
    assert accepted[0]["currency"] == "CNY"
    assert accepted[0]["price"] > 0
    if platform == "jd":
        assert accepted[0]["image_url"].startswith("https://img10.360buyimg.com/n1/jfs/")
    if platform == "douyin":
        assert accepted[0]["price"] == 159.0
        assert accepted[0]["rating"] == 96.5
        assert accepted[0]["sales"] == 5000
        assert accepted[0]["attributes"]["category_path"][-1] == "耳机"
        assert accepted[0]["attributes"]["rating_type"] == "good_ratio_percent"


def test_platform_specific_metadata_is_detected() -> None:
    taobao_items, taobao_meta = search_items("taobao", fixture("taobao_search.json"))
    jd_items, jd_meta = search_items("jd", fixture("jd_search.json"))
    douyin_items, douyin_meta = search_items("douyin", fixture("douyin_search.json"))
    assert len(taobao_items) == len(jd_items) == len(douyin_items) == 2
    assert taobao_meta["total_pages"] == 10
    assert jd_meta["page_size"] == 48
    assert douyin_meta["search_id"] == "search-next-page"


def test_zero_total_pages_does_not_hide_nonempty_jd_items() -> None:
    payload = fixture("jd_search.json")
    payload["data"]["totalPages"] = 0
    items, metadata = search_items("jd", payload)
    assert len(items) == 2
    assert metadata["total_pages"] == 0


def test_redaction_removes_token_recursively() -> None:
    source = {
        "token": "sensitive-token",
        "url": "https://example.test/?token=sensitive-token&q=耳机",
    }
    encoded = json.dumps(redact(source), ensure_ascii=False)
    assert "sensitive-token" not in encoded
    assert encoded.count("[REDACTED]") == 2


def test_ledger_caps_successful_call_budget(tmp_path: Path) -> None:
    config = CollectionConfig(
        root=tmp_path,
        max_success_calls=2,
        max_attempts=3,
        assumed_search_cost_cny=Decimal("0.10"),
        estimated_budget_cny=Decimal("0.10"),
    )
    ledger = RequestLedger(tmp_path / "ledger.jsonl", config)
    ledger.reserve("first", "taobao", {"keyword": "耳机", "page": 1})
    ledger.finish("first", status="ok", business_code="0")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", "jd", {"keyword": "耳机", "page": 1})


def test_client_reuses_raw_response_without_second_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["token"] == "test-token"
        return httpx.Response(200, json=fixture("taobao_search.json"))

    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = JustOneClient(
        "test-token", config, ledger, transport=httpx.MockTransport(handler)
    )
    params = {"keyword": "耳机", "page": 1, "_keyword_index": 0}
    try:
        first = client.call("taobao", params)
        second = client.call("taobao", params)
    finally:
        client.close()
    assert first == second
    assert calls == 1
    persisted = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.*"))
    assert "test-token" not in persisted


def test_cached_nonbillable_error_retries_once_only_when_explicit(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "page" not in request.url.params
        if calls == 1:
            return httpx.Response(
                200, json={"code": 301, "message": "COLLECT FAILED", "data": None}
            )
        return httpx.Response(200, json=fixture("douyin_search.json"))

    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    params = {"keyword": "耳机", "page": 1, "_keyword_index": 0}
    first_client = JustOneClient(
        "test-token", config, ledger, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderResponseError):
            first_client.call("douyin", params)
    finally:
        first_client.close()

    retry_client = JustOneClient(
        "test-token",
        config,
        ledger,
        transport=httpx.MockTransport(handler),
        retry_nonbillable_errors=True,
    )
    try:
        payload = retry_client.call("douyin", params)
        cached = retry_client.call("douyin", params)
    finally:
        retry_client.close()
    assert payload == cached
    assert calls == 2
    assert ledger.successful_calls == 1


def test_collector_runs_balanced_end_to_end_with_mock_transport(tmp_path: Path) -> None:
    fixture_by_path = {
        "/api/taobao/search-item-list/v1": "taobao_search.json",
        "/api/jd/search-item-list/v1": "jd_search.json",
        "/api/douyin-ec/search-item-list/v1": "douyin_search.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture(fixture_by_path[request.url.path]))

    config = CollectionConfig(
        root=tmp_path,
        targets={"taobao": 1, "jd": 1, "douyin": 1},
        minimums={"taobao": 1, "jd": 1, "douyin": 1},
        max_success_calls=3,
        max_attempts=3,
        estimated_budget_cny=Decimal("0.30"),
        request_interval_seconds=0,
    )
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = JustOneClient(
        "test-token", config, ledger, transport=httpx.MockTransport(handler)
    )
    try:
        manifest = JustOneCollector(client, config).run()
    finally:
        client.close()
    assert manifest["status"] == "complete"
    assert manifest["counts"] == {"taobao": 1, "jd": 1, "douyin": 1}
    assert manifest["requests"]["successful_calls"] == 3
    assert (tmp_path / "normalized" / "headphones.jsonl").exists()


def test_manual_success_response_is_imported_without_api_request(tmp_path: Path) -> None:
    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = JustOneClient("test-token", config, ledger)
    try:
        collector = JustOneCollector(client, config, platforms=("douyin",))
        manifest = collector.import_response(
            "douyin", FIXTURES / "douyin_search.json", keyword="耳机", page=1
        )
    finally:
        client.close()

    state = json.loads(
        (tmp_path / "state" / "collection_state.json").read_text(encoding="utf-8")
    )
    assert manifest["counts"]["douyin"] == 1
    assert manifest["requests"]["successful_calls"] == 0
    assert state["platforms"]["douyin"]["next_page"] == 2
    assert state["platforms"]["douyin"]["search_id"] == "search-next-page"
    assert (
        json.loads(
            (tmp_path / "reports" / "quality_report.json").read_text(encoding="utf-8")
        )["imported_response_count"]
        == 1
    )
    persisted = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.*"))
    assert "test-token" not in persisted


def test_smoke_test_does_not_skip_first_page_of_a_new_keyword(tmp_path: Path) -> None:
    config = CollectionConfig(root=tmp_path, request_interval_seconds=0)
    ledger = RequestLedger(tmp_path / "reports" / "request_ledger.jsonl", config)
    client = JustOneClient("test-token", config, ledger)
    try:
        collector = JustOneCollector(client, config, platforms=("douyin",))
        collector.candidates["douyin:existing"] = {"platform": "douyin"}
        progress = collector.state.platforms["douyin"]
        progress.keyword_index = 1
        progress.next_page = 1
        collector.smoke_test()
        assert progress.next_page == 1
    finally:
        client.close()


def test_dry_run_stays_within_estimated_budget() -> None:
    plan = dry_run(CollectionConfig())
    assert plan["target_total"] == 1000
    assert plan["max_success_calls"] == 80
    assert Decimal(plan["expected_estimated_cost_cny"]) <= Decimal(
        plan["estimated_budget_cny"]
    )
