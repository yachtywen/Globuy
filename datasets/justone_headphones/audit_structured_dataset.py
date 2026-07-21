"""Audit ItemSearch candidate image URLs and catalog coverage without downloading images."""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

INPUT_PATH = Path("datasets/headphones_1000/structured/itemsearch_candidates.jsonl")
OUTPUT_DIR = Path("datasets/headphones_1000/structured")
IMAGE_AUDIT_FILENAME = "image_url_audit.jsonl"
REPORT_FILENAME = "dataset_audit_report.json"
_CLIENT_LOCAL = threading.local()


def _client(timeout_seconds: float, *, verify: bool) -> httpx.Client:
    client_name = "verified_client" if verify else "insecure_client"
    client = getattr(_CLIENT_LOCAL, client_name, None)
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            verify=verify,
            headers={"User-Agent": "globuy-dataset-audit/1.0"},
        )
        setattr(_CLIENT_LOCAL, client_name, client)
    return client


def _image_response(
    method: str, url: str, timeout_seconds: float, *, verify: bool
) -> tuple[int | None, str | None, str | None]:
    headers = {"Range": "bytes=0-0"} if method == "GET" else {}
    try:
        with _client(timeout_seconds, verify=verify).stream(
            method, url, headers=headers
        ) as response:
            return (
                response.status_code,
                response.headers.get("content-type"),
                str(response.url),
            )
    except httpx.HTTPError as exc:
        return None, None, type(exc).__name__


def _audit_url(url: str, timeout_seconds: float, *, verify: bool) -> dict[str, Any]:
    status, content_type, final_url_or_error = _image_response(
        "HEAD", url, timeout_seconds, verify=verify
    )
    image_type = bool(content_type and content_type.lower().startswith("image/"))
    if status is not None and 200 <= status < 300 and image_type:
        return {
            "reachable": True,
            "valid_image_content_type": True,
            "method": "HEAD",
            "http_status": status,
            "content_type": content_type,
            "final_url_or_error": final_url_or_error,
        }

    status, content_type, final_url_or_error = _image_response(
        "GET", url, timeout_seconds, verify=verify
    )
    image_type = bool(content_type and content_type.lower().startswith("image/"))
    return {
        "reachable": bool(status is not None and 200 <= status < 300),
        "valid_image_content_type": bool(
            status is not None and 200 <= status < 300 and image_type
        ),
        "method": "GET",
        "http_status": status,
        "content_type": content_type,
        "final_url_or_error": final_url_or_error,
    }


def audit_image_url(item: dict[str, Any], timeout_seconds: float = 15.0) -> dict[str, Any]:
    url = item.get("image_url")
    base = {
        "item_id": item["item_id"],
        "platform": item["platform"],
        "image_url": url,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return {
            **base,
            "reachable": False,
            "valid_image_content_type": False,
            "tls_verified": False,
            "tls_fallback_used": False,
            "strict_tls_error": "missing_or_invalid_url",
            "method": None,
            "http_status": None,
            "content_type": None,
            "final_url_or_error": "missing_or_invalid_url",
        }

    strict_result = _audit_url(url, timeout_seconds, verify=True)
    strict_error = strict_result["final_url_or_error"]
    if strict_result["http_status"] is None and strict_error == "ConnectError":
        fallback_result = _audit_url(url, timeout_seconds, verify=False)
        return {
            **base,
            **fallback_result,
            "tls_verified": False,
            "tls_fallback_used": True,
            "strict_tls_error": strict_error,
        }
    return {
        **base,
        **strict_result,
        "tls_verified": True,
        "tls_fallback_used": False,
        "strict_tls_error": None,
    }


def _contains(title: str, *terms: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in terms)


def price_band(price: float) -> str:
    if price < 100:
        return "low_under_100"
    if price < 300:
        return "budget_100_to_299"
    if price < 1000:
        return "mid_300_to_999"
    if price < 3000:
        return "high_1000_to_2999"
    return "premium_3000_plus"


def catalog_labels(item: dict[str, Any]) -> set[str]:
    title = str(item.get("title") or "")
    labels: set[str] = set()
    if _contains(title, "头戴", "头戴式", "包耳"):
        labels.add("over_ear")
    if _contains(title, "入耳", "耳塞", "真无线", "tws"):
        labels.add("in_ear")
    if _contains(title, "开放式", "不入耳", "骨传导", "挂耳", "耳夹"):
        labels.add("open_ear_or_bone_conduction")
    if _contains(title, "蓝牙", "无线", "真无线", "tws"):
        labels.add("bluetooth_or_wireless")
    if _contains(title, "有线", "3.5mm", "type-c", "usb-c", "线控", "双插头"):
        labels.add("wired")
    if _contains(title, "降噪", "anc", "主动降噪"):
        labels.add("noise_cancelling")
    if _contains(title, "游戏", "电竞"):
        labels.add("gaming")
    return labels or {"other_headphone"}


def _quantile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = round((len(values) - 1) * percentile)
    return values[index]


def catalog_coverage(items: list[dict[str, Any]]) -> dict[str, Any]:
    prices = sorted(float(item["price"]) for item in items)
    price_bands = Counter(price_band(float(item["price"])) for item in items)
    labels = Counter(label for item in items for label in catalog_labels(item))
    platform_bands: dict[str, dict[str, int]] = {}
    for platform in sorted({item["platform"] for item in items}):
        platform_bands[platform] = dict(
            sorted(
                Counter(
                    price_band(float(item["price"]))
                    for item in items
                    if item["platform"] == platform
                ).items()
            )
        )
    return {
        "price_bands": dict(sorted(price_bands.items())),
        "price_quantiles_cny": {
            "minimum": prices[0] if prices else None,
            "p05": _quantile(prices, 0.05),
            "p25": _quantile(prices, 0.25),
            "median": _quantile(prices, 0.5),
            "p75": _quantile(prices, 0.75),
            "p95": _quantile(prices, 0.95),
            "maximum": prices[-1] if prices else None,
        },
        "feature_labels": dict(sorted(labels.items())),
        "price_bands_by_platform": platform_bands,
    }


def _image_summary(audits: list[dict[str, Any]]) -> dict[str, Any]:
    by_platform: dict[str, dict[str, int]] = {}
    for platform in sorted({audit["platform"] for audit in audits}):
        subset = [audit for audit in audits if audit["platform"] == platform]
        by_platform[platform] = {
            "total": len(subset),
            "reachable": sum(audit["reachable"] for audit in subset),
            "valid_image_content_type": sum(
                audit["valid_image_content_type"] for audit in subset
            ),
        }
    return {
        "total": len(audits),
        "reachable": sum(audit["reachable"] for audit in audits),
        "valid_image_content_type": sum(
            audit["valid_image_content_type"] for audit in audits
        ),
        "tls_verified_reachable": sum(
            audit["reachable"] and audit["tls_verified"] for audit in audits
        ),
        "insecure_tls_fallback_reachable": sum(
            audit["reachable"] and audit["tls_fallback_used"] for audit in audits
        ),
        "http_status_counts": dict(
            sorted(Counter(str(audit["http_status"]) for audit in audits).items())
        ),
        "method_counts": dict(
            sorted(Counter(str(audit["method"]) for audit in audits).items())
        ),
        "by_platform": by_platform,
    }


def run_audit(
    input_path: Path = INPUT_PATH,
    output_dir: Path = OUTPUT_DIR,
    *,
    workers: int = 8,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    items = [
        json.loads(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    audits: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(audit_image_url, item, timeout_seconds) for item in items
        ]
        for future in as_completed(futures):
            audits.append(future.result())
    audits.sort(key=lambda audit: audit["item_id"])
    audit_path = output_dir / IMAGE_AUDIT_FILENAME
    audit_path.write_text(
        "".join(json.dumps(audit, ensure_ascii=False) + "\n" for audit in audits),
        encoding="utf-8",
    )
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "records": len(items),
        "image_url_audit": _image_summary(audits),
        "catalog_coverage": catalog_coverage(items),
    }
    (output_dir / REPORT_FILENAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit ItemSearch candidate images and coverage")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(
        json.dumps(
            run_audit(workers=arguments.workers, timeout_seconds=arguments.timeout_seconds),
            ensure_ascii=False,
            indent=2,
        )
    )
