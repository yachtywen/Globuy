"""Resumable, budget-capped collection of headphone offers from Just One API."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

Platform = Literal["taobao", "jd", "douyin"]

PLATFORMS: tuple[Platform, ...] = ("taobao", "jd", "douyin")
TARGETS: dict[Platform, int] = {"taobao": 334, "jd": 333, "douyin": 333}
MINIMUMS: dict[Platform, int] = {platform: 300 for platform in PLATFORMS}
KEYWORDS = (
    "耳机",
    "蓝牙耳机",
    "头戴式耳机",
    "降噪耳机",
    "入耳式耳机",
    "无线耳机",
    "游戏耳机",
    "运动耳机",
    "骨传导耳机",
    "开放式耳机",
    "有线耳机",
    "真无线耳机",
    "耳麦",
    "电脑耳机",
    "电竞耳机",
    "挂耳式耳机",
)
POSITIVE_TERMS = (
    "耳机",
    "耳麦",
    "airpods",
    "earbuds",
    "headphone",
    "headset",
    "tws",
)
ACCESSORY_TERMS = (
    "保护套",
    "保护壳",
    "耳机套",
    "耳塞套",
    "耳帽",
    "替换线",
    "耳机线",
    "转接头",
    "转换器",
    "收纳盒",
    "收纳包",
    "充电仓",
    "防尘塞",
    "耳机支架",
    "耳机架",
    "挂绳",
    "维修",
)

ENDPOINTS: dict[Platform, str] = {
    "taobao": "/api/taobao/search-item-list/v1",
    "jd": "/api/jd/search-item-list/v1",
    "douyin": "/api/douyin-ec/search-item-list/v1",
}
NON_BILLABLE_CODES = {100, 301, 302, 303, 400, 500, 600, 601, 602}
AUTHENTICATION_CODES = {100, 600, 601, 602}


class CollectionError(RuntimeError):
    """Base error for a safe collection stop."""


class BudgetExceeded(CollectionError):
    """Raised before a request that would exceed a hard safety limit."""


class AuthenticationError(CollectionError):
    """Raised when the token, balance, quota, or permission is rejected."""


class ProviderResponseError(CollectionError):
    """Raised for a cached or current non-success provider response."""

    def __init__(self, platform: Platform, code: str) -> None:
        super().__init__(f"{platform} 搜索返回非成功业务码 {code}，停止并保留断点")
        self.platform = platform
        self.code = code


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs without printing or returning secrets."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def redact(value: Any) -> Any:
    """Remove credentials recursively before persistence."""

    sensitive = {"token", "access_token", "api_token", "authorization"}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in sensitive else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(token|access_token)=([^&\s]+)", r"\1=[REDACTED]", value)
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    for attempt in range(3):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.02)


def request_key(platform: Platform, params: dict[str, Any]) -> str:
    material = f"{platform}|{urlencode(sorted(params.items()))}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _business_code(payload: dict[str, Any]) -> str:
    value = payload.get("code")
    return str(value) if value is not None else "missing"


@dataclass(slots=True)
class CollectionConfig:
    root: Path = Path(__file__).resolve().parent
    targets: dict[Platform, int] = field(default_factory=lambda: dict(TARGETS))
    minimums: dict[Platform, int] = field(default_factory=lambda: dict(MINIMUMS))
    max_pages_per_keyword: int = 50
    max_success_calls: int = 80
    max_attempts: int = 140
    assumed_search_cost_cny: Decimal = Decimal("0.10")
    estimated_budget_cny: Decimal = Decimal("8.00")
    request_interval_seconds: float = 0.8
    request_timeout_seconds: float = 120.0

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def normalized_dir(self) -> Path:
        return self.root / "normalized"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"


class RequestLedger:
    """Append-only request ledger with attempt and successful-call caps."""

    def __init__(self, path: Path, config: CollectionConfig) -> None:
        self.path = path
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reserved: dict[str, dict[str, Any]] = {}
        self._finished: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("event") == "reserved":
                self._reserved[item["request_key"]] = item
            elif item.get("event") == "finished":
                self._finished[item["request_key"]] = item

    def _append(self, item: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(redact(item), ensure_ascii=False, default=str) + "\n")

    @property
    def attempts(self) -> int:
        return len(self._reserved)

    @property
    def successful_calls(self) -> int:
        return sum(item.get("status") == "ok" for item in self._finished.values())

    @property
    def estimated_cost(self) -> Decimal:
        return self.config.assumed_search_cost_cny * self.successful_calls

    def contains(self, key: str) -> bool:
        return key in self._reserved

    def reserve(self, key: str, platform: Platform, params: dict[str, Any]) -> None:
        if key in self._reserved:
            raise CollectionError(f"请求 {key} 已登记，拒绝隐式重复调用")
        if self.attempts >= self.config.max_attempts:
            raise BudgetExceeded("已达到总请求尝试次数上限")
        if self.successful_calls >= self.config.max_success_calls:
            raise BudgetExceeded("已达到成功搜索调用次数上限")
        projected = self.estimated_cost + self.config.assumed_search_cost_cny
        if projected > self.config.estimated_budget_cny:
            raise BudgetExceeded("下一次成功调用将超过保守估算费用上限")
        record = {
            "event": "reserved",
            "request_key": key,
            "platform": platform,
            "params": params,
            "timestamp": utc_now(),
        }
        self._append(record)
        self._reserved[key] = record

    def finish(self, key: str, **result: Any) -> None:
        record = {"event": "finished", "request_key": key, "timestamp": utc_now(), **result}
        self._append(record)
        self._finished[key] = record

    def summary(self) -> dict[str, Any]:
        failures = [item for item in self._finished.values() if item.get("status") != "ok"]
        codes: dict[str, int] = {}
        platform_successes = {platform: 0 for platform in PLATFORMS}
        for key, item in self._finished.items():
            if item.get("status") == "ok":
                reserved = self._reserved.get(key, {})
                platform = reserved.get("platform")
                if platform in platform_successes:
                    platform_successes[platform] += 1
            else:
                code = str(item.get("business_code") or item.get("http_status") or "unknown")
                codes[code] = codes.get(code, 0) + 1
        return {
            "attempts": self.attempts,
            "successful_calls": self.successful_calls,
            "successful_calls_by_platform": platform_successes,
            "failed_calls": len(failures),
            "failure_codes": codes,
            "unfinished_reservations": sum(
                key not in self._finished for key in self._reserved
            ),
            "max_attempts": self.config.max_attempts,
            "max_success_calls": self.config.max_success_calls,
            "assumed_search_cost_cny": str(self.config.assumed_search_cost_cny),
            "estimated_cost_cny": str(self.estimated_cost.quantize(Decimal("0.01"))),
            "estimated_budget_cny": str(self.config.estimated_budget_cny),
            "price_assumption_verified": False,
        }


class JustOneClient:
    def __init__(
        self,
        token: str,
        config: CollectionConfig,
        ledger: RequestLedger,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_nonbillable_errors: bool = False,
    ) -> None:
        if not token:
            raise AuthenticationError(
                "请在 datasets/justone_headphones/.env 配置 GLOBUY_JUSTONE_TOKEN"
            )
        self._token = token
        self.config = config
        self.ledger = ledger
        self.retry_nonbillable_errors = retry_nonbillable_errors
        self._client = httpx.Client(
            base_url="https://api.justoneapi.com",
            timeout=config.request_timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
        )

    def close(self) -> None:
        self._client.close()

    def _raw_path(self, platform: Platform, params: dict[str, Any], key: str) -> Path:
        page = int(params.get("page", 1))
        keyword_index = int(params.get("_keyword_index", 0))
        retry_index = int(params.get("_retry_index", 0))
        stem = f"k{keyword_index:02d}-p{page:03d}-r{retry_index}-{key}"
        return self.config.raw_dir / platform / "search" / f"{stem}.json"

    @staticmethod
    def _public_params(params: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if key != "token"}

    @staticmethod
    def _provider_params(params: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if not key.startswith("_")}

    def _validated_payload(
        self, platform: Platform, payload: dict[str, Any]
    ) -> dict[str, Any]:
        code = _business_code(payload)
        if code == "0":
            return payload
        try:
            numeric_code = int(code)
        except ValueError:
            numeric_code = -1
        if numeric_code in AUTHENTICATION_CODES:
            raise AuthenticationError(f"{platform} 鉴权、权限、限额或余额检查失败：{code}")
        raise ProviderResponseError(platform, code)

    def call(self, platform: Platform, params: dict[str, Any]) -> dict[str, Any]:
        if (
            platform == "douyin"
            and int(params.get("page", 1)) == 1
            and not params.get("searchId")
            and "_request_variant" not in params
        ):
            params = {**params, "_request_variant": "omit_optional_page"}
        public_params = self._public_params(params)
        key = request_key(platform, public_params)
        raw_path = self._raw_path(platform, public_params, key)
        if raw_path.exists():
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            if _business_code(payload) == "0":
                return payload
            code = _business_code(payload)
            try:
                numeric_code = int(code)
            except ValueError:
                numeric_code = -1
            if (
                self.retry_nonbillable_errors
                and numeric_code in NON_BILLABLE_CODES
            ):
                retry_params = {
                    **params,
                    "_retry_index": int(public_params.get("_retry_index", 0)) + 1,
                }
                return self.call(platform, retry_params)
            return self._validated_payload(platform, payload)
        if self.ledger.contains(key):
            raise CollectionError(f"请求 {key} 已登记但无响应文件，拒绝自动重发")

        self.ledger.reserve(key, platform, public_params)
        provider_params = self._provider_params(params)
        if params.get("_request_variant") == "omit_optional_page":
            provider_params.pop("page", None)
        request_params = {**provider_params, "token": self._token}
        try:
            response = self._client.get(ENDPOINTS[platform], params=request_params)
            http_status = response.status_code
            try:
                decoded = response.json()
            except ValueError:
                decoded = {
                    "code": f"http_{http_status}",
                    "message": "non_json_response",
                    "data": None,
                }
            payload = decoded if isinstance(decoded, dict) else {
                "code": f"http_{http_status}",
                "message": "unexpected_json_type",
                "data": decoded,
            }
        except httpx.HTTPError as exc:
            self.ledger.finish(key, status="transport_error", error_type=type(exc).__name__)
            raise CollectionError(
                f"{platform} 网络请求失败（{type(exc).__name__}），停止且不自动重试"
            ) from None

        safe_payload = redact(payload)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        code = _business_code(safe_payload)
        status = "ok" if http_status == 200 and code == "0" else "provider_error"
        self.ledger.finish(
            key,
            status=status,
            http_status=http_status,
            business_code=code,
            raw_file=str(raw_path),
        )
        time.sleep(self.config.request_interval_seconds)
        return self._validated_payload(platform, safe_payload)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", -1, "-1"):
        return None
    if isinstance(value, dict):
        for key in ("value", "amount", "price", "current", "min", "minPrice"):
            if key in value:
                result = _decimal(value[key])
                if result is not None:
                    return result
        return None
    if isinstance(value, list):
        for item in value:
            result = _decimal(item)
            if result is not None:
                return result
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        result = Decimal(match.group())
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").replace("+", "").strip().lower()
    multiplier = 1
    if "万" in text or text.endswith("w"):
        multiplier = 10_000
    elif "亿" in text:
        multiplier = 100_000_000
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    return int(float(match.group()) * multiplier)


def _absolute_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith(("http://", "https://")) else None


def _nested_value(value: Any, aliases: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 5:
        return None
    lowered = {alias.lower() for alias in aliases}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in lowered and item not in (None, "", [], {}):
                return item
        for item in value.values():
            found = _nested_value(item, aliases, depth + 1)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for item in value[:5]:
            found = _nested_value(item, aliases, depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None


def _image_url(value: Any) -> str | None:
    direct = _absolute_url(value)
    if direct:
        return direct
    if isinstance(value, list):
        for item in value:
            result = _image_url(item)
            if result:
                return result
    if isinstance(value, dict):
        for key in ("url", "url_list", "imageUrl", "image_url", "src", "origin"):
            result = _image_url(value.get(key))
            if result:
                return result
    return None


def _jd_image_url(value: Any) -> str | None:
    direct = _image_url(value)
    if direct:
        return direct
    if isinstance(value, str) and value.strip().lstrip("/").startswith("jfs/"):
        return "https://img10.360buyimg.com/n1/" + value.strip().lstrip("/")
    return None


def is_headphone_title(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in POSITIVE_TERMS) and not any(
        term in lowered for term in ACCESSORY_TERMS
    )


def _candidate_array_score(items: list[Any]) -> int:
    score = 0
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        if _nested_value(item, ("title", "itemName", "productName", "goodsName", "name")):
            score += 3
        if _nested_value(
            item,
            ("itemId", "item_id", "productId", "product_id", "goodsId", "goods_id", "id"),
        ):
            score += 3
        if _nested_value(
            item,
            ("price", "salePrice", "sale_price", "priceYuanDouble", "discountPrice"),
        ):
            score += 2
    return score


def _arrays(value: Any, path: str = "$.data", depth: int = 0) -> list[tuple[str, list[Any]]]:
    if depth > 7:
        return []
    found: list[tuple[str, list[Any]]] = []
    if isinstance(value, list):
        found.append((path, value))
        if value:
            found.extend(_arrays(value[0], path + "[0]", depth + 1))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_arrays(item, f"{path}.{key}", depth + 1))
    return found


def search_items(
    platform: Platform, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data")
    metadata: dict[str, Any] = {}
    items: list[Any] = []
    if platform == "taobao" and isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, dict):
            items = model.get("itemList") or []
            page = model.get("page")
            if isinstance(page, dict):
                metadata = {
                    "total": page.get("totalItems"),
                    "total_pages": page.get("totalPages"),
                    "page_size": page.get("pageSize"),
                }
    elif platform == "jd" and isinstance(data, dict):
        items = data.get("products") or []
        metadata = {
            "total": data.get("totalCount"),
            "total_pages": data.get("totalPages"),
            "page_size": data.get("pageSize"),
        }
    elif platform == "douyin" and isinstance(data, dict):
        items = data.get("summary_promotions") or data.get("promotions") or []
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
        metadata = {
            "total": data.get("total"),
            "page_size": len(items),
            "has_more": data.get("has_more"),
            "search_id": extra.get("search_id") or extra.get("searchId"),
        }
    if not isinstance(items, list) or not items:
        candidates = sorted(
            _arrays(data),
            key=lambda pair: (_candidate_array_score(pair[1]), len(pair[1])),
            reverse=True,
        )
        if candidates and _candidate_array_score(candidates[0][1]) > 0:
            items = candidates[0][1]
            metadata["items_path"] = candidates[0][0]
    metadata.setdefault(
        "search_id", _nested_value(data, ("search_id", "searchId", "search_id_str"))
    )
    metadata.setdefault(
        "total", _nested_value(data, ("totalCount", "total_count", "total", "totalItems"))
    )
    metadata.setdefault(
        "total_pages", _nested_value(data, ("totalPages", "total_pages", "pageCount"))
    )
    return [item for item in items if isinstance(item, dict)], metadata


def _clean_attributes(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def _normalize_taobao(item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("itemId") or item.get("prodId")
    title = item.get("itemName") or item.get("itemSubName")
    price = _decimal(
        item.get("discntPriceYuan")
        or item.get("priceZKYuanDouble")
        or item.get("priceYuanDouble")
    )
    attributes = _clean_attributes(
        {
            "shop_id": item.get("shopId"),
            "shop_name": item.get("shopName"),
            "spu_id": item.get("spuId"),
            "seller_level": item.get("sellerLevel"),
            "seller_location": item.get("sellerLoc") or item.get("itemLoc"),
            "seller_good_rate": item.get("sellerGoodrat"),
            "item_type": item.get("itemType"),
            "user_type": item.get("userType"),
            "variant_names": item.get("vidname"),
            "options": item.get("options"),
            "tags": item.get("tmcTagList") or item.get("tagList"),
            "comment_count": _integer(item.get("commentCount")),
        }
    )
    return {
        "source_id": source_id,
        "title": title,
        "price": price,
        "rating": _decimal(item.get("itemGradeAvg")),
        "sales": _integer(item.get("orderPayUV")),
        "image_url": _image_url(item.get("picUrlFull") or item.get("picUrl")),
        "source_url": (
            f"https://item.taobao.com/item.htm?id={source_id}" if source_id else None
        ),
        "attributes": attributes,
    }


def _normalize_jd(item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("id") or item.get("oneItemId")
    attributes = _clean_attributes(
        {
            "shop_id": item.get("shopId"),
            "shop_name": item.get("shopName"),
            "vendor_id": item.get("venderId"),
            "category_1": item.get("cid1"),
            "category_2": item.get("cid2"),
            "category_3": item.get("cid3"),
            "sku_type": item.get("skuType"),
            "is_jd_market": item.get("isJdMarket"),
            "is_self_operated": item.get("zy"),
            "slogan": item.get("slogan") or item.get("sellPoint"),
            "structured_attributes": item.get("asgStructuredAttribute"),
            "attributes": item.get("asgAttribute"),
            "lowest_price": item.get("lowestPrice"),
            "price_range": item.get("jdpriceRange"),
            "month_sales": _integer(item.get("monthSales")),
        }
    )
    source_url = _absolute_url(item.get("landUrl"))
    if not source_url and source_id:
        source_url = f"https://item.jd.com/{source_id}.html"
    return {
        "source_id": source_id,
        "title": item.get("title") or item.get("shortTitle"),
        "price": _decimal(item.get("price") or item.get("lowestPrice")),
        "rating": None,
        "sales": _integer(item.get("sales") or item.get("monthSales")),
        "image_url": _jd_image_url(
            item.get("imageUrl") or item.get("longImageUrl") or item.get("images")
        ),
        "source_url": source_url,
        "attributes": attributes,
    }


def _douyin_money(value: Any) -> Decimal | None:
    """Convert Douyin e-commerce integer fen values to CNY yuan."""

    if isinstance(value, dict):
        value = value.get("origin")
    amount = _decimal(value)
    return amount / Decimal(100) if amount is not None else None


def _normalize_douyin(item: dict[str, Any]) -> dict[str, Any]:
    base = item.get("base_model") if isinstance(item.get("base_model"), dict) else {}
    product = (
        base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    )
    marketing = (
        base.get("marketing_info") if isinstance(base.get("marketing_info"), dict) else {}
    )
    price_desc = (
        marketing.get("price_desc")
        if isinstance(marketing.get("price_desc"), dict)
        else {}
    )
    shop = base.get("shop_info") if isinstance(base.get("shop_info"), dict) else {}
    category = product.get("category") if isinstance(product.get("category"), dict) else {}
    tag_info = base.get("tag_info") if isinstance(base.get("tag_info"), dict) else {}
    promotion = (
        base.get("promotion_info")
        if isinstance(base.get("promotion_info"), dict)
        else {}
    )
    shop_score_info = (
        shop.get("shop_score_info")
        if isinstance(shop.get("shop_score_info"), dict)
        else {}
    )
    shop_score = (
        shop_score_info.get("shop_score")
        if isinstance(shop_score_info.get("shop_score"), dict)
        else {}
    )
    good_ratio = product.get("good_ratio")
    month_sale = product.get("month_sale")
    category_path: list[str] = []
    category_ids: list[str] = []
    for layer in ("first_category", "second_category", "third_category", "fourth_category"):
        value = category.get(layer)
        if not isinstance(value, dict) or not value.get("category_name"):
            continue
        category_path.append(str(value["category_name"]))
        if value.get("category_id") not in (None, "", 0, "0"):
            category_ids.append(str(value["category_id"]))
    attributes = _clean_attributes(
        {
            "promotion_id": item.get("promotion_id"),
            "shop_id": shop.get("shop_id"),
            "shop_name": shop.get("shop_name"),
            "shop_score": shop_score.get("score"),
            "category_path": category_path,
            "category_ids": category_ids,
            "product_status": product.get("product_status"),
            "promotion_status": promotion.get("promotion_status"),
            "tag_codes": tag_info.get("tag_codes"),
            "price_text": price_desc.get("price_text"),
            "regular_price": (
                float(value)
                if (value := _douyin_money(price_desc.get("regular_price"))) is not None
                else None
            ),
            "rating_type": "good_ratio_percent" if good_ratio else None,
        }
    )
    rating_value = good_ratio.get("origin") if isinstance(good_ratio, dict) else good_ratio
    sales_value = month_sale.get("origin") if isinstance(month_sale, dict) else month_sale
    return {
        "source_id": item.get("product_id") or item.get("promotion_id"),
        "title": product.get("name"),
        "price": _douyin_money(price_desc.get("price")),
        "rating": _decimal(rating_value),
        "sales": _integer(sales_value),
        "image_url": _image_url(product.get("main_img") or product.get("white_img")),
        "source_url": _absolute_url(product.get("detail_url")),
        "attributes": attributes,
    }


def _normalize_generic(item: dict[str, Any]) -> dict[str, Any]:
    source_id = _nested_value(
        item,
        ("itemId", "item_id", "productId", "product_id", "goodsId", "goods_id", "id"),
    )
    title = _nested_value(
        item,
        (
            "title",
            "itemName",
            "item_name",
            "productName",
            "product_name",
            "goodsName",
            "goods_name",
            "name",
        ),
    )
    price = _decimal(
        _nested_value(
            item,
            (
                "salePrice",
                "sale_price",
                "discountPrice",
                "discount_price",
                "price",
                "minPrice",
                "min_price",
            ),
        )
    )
    image = _image_url(
        _nested_value(
            item,
            ("imageUrl", "image_url", "imgUrl", "img_url", "picUrl", "pic_url", "cover", "images"),
        )
    )
    source_url = _absolute_url(
        _nested_value(
            item,
            (
                "detailUrl",
                "detail_url",
                "productUrl",
                "product_url",
                "shareUrl",
                "share_url",
                "jumpUrl",
                "jump_url",
            ),
        )
    )
    attributes = _clean_attributes(
        {
            "brand": _nested_value(item, ("brand", "brandName", "brand_name")),
            "category": _nested_value(
                item, ("category", "categoryName", "category_name", "categoryId")
            ),
            "shop_id": _nested_value(item, ("shopId", "shop_id", "storeId", "store_id")),
            "shop_name": _nested_value(
                item, ("shopName", "shop_name", "storeName", "store_name")
            ),
            "sku": _nested_value(item, ("sku", "skuInfo", "sku_info", "skuList")),
            "properties": _nested_value(
                item, ("properties", "property", "attributes", "attrs", "specs")
            ),
        }
    )
    return {
        "source_id": source_id,
        "title": title,
        "price": price,
        "rating": _decimal(_nested_value(item, ("rating", "score", "goodRate"))),
        "sales": _integer(
            _nested_value(
                item,
                ("sales", "saleCount", "sale_count", "soldCount", "sold_count", "payCount"),
            )
        ),
        "image_url": image,
        "source_url": source_url,
        "attributes": attributes,
    }


def normalize_search_item(
    platform: Platform,
    item: dict[str, Any],
    *,
    keyword: str,
    page: int,
    captured_at: str,
) -> dict[str, Any] | None:
    mapped = (
        _normalize_taobao(item)
        if platform == "taobao"
        else _normalize_jd(item)
        if platform == "jd"
        else _normalize_douyin(item)
        if platform == "douyin" and isinstance(item.get("base_model"), dict)
        else _normalize_generic(item)
    )
    source_id = mapped["source_id"]
    title = str(mapped["title"] or "").strip()
    price = mapped["price"]
    if not source_id or not title or price is None or not is_headphone_title(title):
        return None
    return {
        "item_id": f"{platform}:{source_id}",
        "source_item_id": str(source_id),
        "platform": platform,
        "title": title,
        "price": float(price),
        "currency": "CNY",
        "rating": float(mapped["rating"]) if mapped["rating"] is not None else None,
        "sales": mapped["sales"],
        "image_url": mapped["image_url"],
        "source_url": mapped["source_url"],
        "captured_at": captured_at,
        "attributes": mapped["attributes"],
        "detail_enriched": False,
        "source_keyword": keyword,
        "source_page": page,
    }


@dataclass(slots=True)
class PlatformProgress:
    keyword_index: int = 0
    next_page: int = 1
    search_id: str | None = None
    stalled_pages: int = 0
    complete: bool = False


@dataclass(slots=True)
class CollectionState:
    smoke_complete: bool = False
    platforms: dict[str, PlatformProgress] = field(
        default_factory=lambda: {platform: PlatformProgress() for platform in PLATFORMS}
    )

    @classmethod
    def load(cls, path: Path) -> CollectionState:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            smoke_complete=bool(raw.get("smoke_complete")),
            platforms={
                platform: PlatformProgress(**raw.get("platforms", {}).get(platform, {}))
                for platform in PLATFORMS
            },
        )

    def save(self, path: Path) -> None:
        atomic_json(
            path,
            {
                "smoke_complete": self.smoke_complete,
                "platforms": {
                    platform: asdict(progress) for platform, progress in self.platforms.items()
                },
                "updated_at": utc_now(),
            },
        )


class JustOneCollector:
    def __init__(
        self,
        client: JustOneClient,
        config: CollectionConfig,
        *,
        platforms: tuple[Platform, ...] = PLATFORMS,
    ) -> None:
        self.client = client
        self.config = config
        self.platforms = platforms
        self.state_path = config.state_dir / "collection_state.json"
        self.candidates_path = config.state_dir / "candidates.json"
        self.state = CollectionState.load(self.state_path)
        self.candidates = self._load_candidates()
        self.rejected_items = 0
        self.duplicate_items = 0

    def _load_candidates(self) -> dict[str, dict[str, Any]]:
        if not self.candidates_path.exists():
            return {}
        values = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        return {item["item_id"]: item for item in values}

    def _checkpoint(self) -> None:
        self.state.save(self.state_path)
        atomic_json(self.candidates_path, list(self.candidates.values()))

    def platform_count(self, platform: Platform) -> int:
        return sum(item["platform"] == platform for item in self.candidates.values())

    def _search(
        self, platform: Platform, keyword: str, page: int, keyword_index: int
    ) -> tuple[int, int, int | None]:
        progress = self.state.platforms[platform]
        params: dict[str, Any] = {
            "keyword": keyword,
            "page": page,
            "_keyword_index": keyword_index,
        }
        if platform == "douyin" and progress.search_id:
            params["searchId"] = progress.search_id
        payload = self.client.call(platform, params)
        raw_items, metadata = search_items(platform, payload)
        search_id = metadata.get("search_id")
        if platform == "douyin" and search_id:
            progress.search_id = str(search_id)
        captured_at = utc_now()
        added = 0
        for raw_item in raw_items:
            candidate = normalize_search_item(
                platform,
                raw_item,
                keyword=keyword,
                page=page,
                captured_at=captured_at,
            )
            if candidate is None:
                self.rejected_items += 1
                continue
            if candidate["item_id"] in self.candidates:
                self.duplicate_items += 1
                continue
            if self.platform_count(platform) >= self.config.targets[platform]:
                break
            self.candidates[candidate["item_id"]] = candidate
            added += 1
        self._checkpoint()
        total_pages = _integer(metadata.get("total_pages"))
        return added, len(raw_items), total_pages if total_pages and total_pages > 0 else None

    def smoke_test(self) -> None:
        if self.state.smoke_complete and self.platforms == PLATFORMS:
            return
        for platform in self.platforms:
            progress = self.state.platforms[platform]
            if self.platform_count(platform) > 0:
                continue
            keyword_index = min(progress.keyword_index, len(KEYWORDS) - 1)
            added, raw_count, _ = self._search(
                platform, KEYWORDS[keyword_index], 1, keyword_index
            )
            if raw_count == 0 or added == 0:
                raise CollectionError(f"{platform} 烟雾测试没有得到有效耳机候选，停止批量采集")
            progress.next_page = 2
        self.state.smoke_complete = all(self.platform_count(platform) > 0 for platform in PLATFORMS)
        self._checkpoint()

    def collect_searches(self, *, max_pages: int | None = None) -> str | None:
        """Collect one page per platform per round for balanced budget usage."""

        processed_pages = 0
        while True:
            made_progress = False
            for platform in self.platforms:
                progress = self.state.platforms[platform]
                if self.platform_count(platform) >= self.config.targets[platform]:
                    progress.complete = True
                    self._checkpoint()
                    continue
                if progress.keyword_index >= len(KEYWORDS):
                    progress.complete = True
                    self._checkpoint()
                    continue
                made_progress = True
                keyword = KEYWORDS[progress.keyword_index]
                page = progress.next_page
                added, raw_count, total_pages = self._search(
                    platform, keyword, page, progress.keyword_index
                )
                progress.next_page += 1
                progress.stalled_pages = progress.stalled_pages + 1 if added == 0 else 0
                if (
                    raw_count == 0
                    or (total_pages is not None and page >= total_pages)
                    or page >= self.config.max_pages_per_keyword
                    or progress.stalled_pages >= 3
                ):
                    progress.keyword_index += 1
                    progress.next_page = 1
                    progress.search_id = None
                    progress.stalled_pages = 0
                self._checkpoint()
                processed_pages += 1
                if max_pages is not None and processed_pages >= max_pages:
                    return f"已按受控模式完成 {processed_pages} 页搜索"
            if not made_progress:
                return None

    def _raw_audit(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for platform in PLATFORMS:
            response_counts: list[int] = []
            success = 0
            for path in self.config.raw_dir.glob(f"{platform}/search/*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _business_code(payload) != "0":
                    continue
                items, _ = search_items(platform, payload)
                success += 1
                response_counts.append(len(items))
            result[platform] = {
                "successful_responses": success,
                "raw_items": sum(response_counts),
                "items_per_response": {
                    "minimum": min(response_counts, default=0),
                    "maximum": max(response_counts, default=0),
                    "average": round(
                        sum(response_counts) / len(response_counts), 2
                    ) if response_counts else 0.0,
                },
            }
        return result

    def _repair_candidates_from_raw(self) -> int:
        """Apply improved local mappers to cached raw responses without paid calls."""

        repaired = 0
        for path in self.config.raw_dir.glob("jd/search/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _business_code(payload) != "0":
                continue
            raw_items, _ = search_items("jd", payload)
            for raw_item in raw_items:
                mapped = _normalize_jd(raw_item)
                source_id = mapped.get("source_id")
                candidate = self.candidates.get(f"jd:{source_id}")
                if candidate is None or candidate.get("image_url") or not mapped.get("image_url"):
                    continue
                candidate["image_url"] = mapped["image_url"]
                repaired += 1
        if repaired:
            self._checkpoint()
        return repaired

    def import_response(
        self,
        platform: Platform,
        response_path: Path,
        *,
        keyword: str,
        page: int,
    ) -> dict[str, Any]:
        """Import a user-provided successful response without making an API request."""

        payload = json.loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or _business_code(payload) != "0":
            raise CollectionError("导入响应不是业务码 0 的成功 JSON，拒绝写入数据集")
        raw_items, metadata = search_items(platform, payload)
        if not raw_items:
            raise CollectionError("导入响应中没有识别到商品列表，拒绝写入数据集")

        captured_at = utc_now()
        added = 0
        duplicates = 0
        for raw_item in raw_items:
            candidate = normalize_search_item(
                platform,
                raw_item,
                keyword=keyword,
                page=page,
                captured_at=captured_at,
            )
            if candidate is None:
                self.rejected_items += 1
                continue
            if candidate["item_id"] in self.candidates:
                duplicates += 1
                continue
            if self.platform_count(platform) >= self.config.targets[platform]:
                break
            self.candidates[candidate["item_id"]] = candidate
            added += 1

        sanitized = redact(payload)
        material = json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(material).hexdigest()
        raw_path = (
            self.config.raw_dir
            / platform
            / "imported"
            / f"p{page:03d}-{digest[:20]}.json"
        )
        atomic_json(raw_path, sanitized)

        progress = self.state.platforms[platform]
        keyword_index = KEYWORDS.index(keyword) if keyword in KEYWORDS else 0
        progress.keyword_index = keyword_index
        progress.next_page = page + 1
        progress.search_id = (
            str(metadata["search_id"]) if metadata.get("search_id") else None
        )
        progress.stalled_pages = 0
        progress.complete = self.platform_count(platform) >= self.config.targets[platform]
        self.state.smoke_complete = all(
            self.platform_count(current_platform) > 0 for current_platform in PLATFORMS
        )
        self._checkpoint()

        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        import_record = {
            "imported_at": captured_at,
            "platform": platform,
            "keyword": keyword,
            "page": page,
            "response_sha256": digest,
            "raw_items": len(raw_items),
            "accepted_items": added,
            "duplicate_items": duplicates,
            "search_id_present": bool(progress.search_id),
            "raw_path": str(raw_path),
        }
        with (self.config.reports_dir / "import_ledger.jsonl").open(
            "a", encoding="utf-8"
        ) as target:
            target.write(json.dumps(import_record, ensure_ascii=False) + "\n")
        return self.write_outputs("已导入人工成功响应，尚未执行后续分页")

    @staticmethod
    def _coverage(items: list[dict[str, Any]]) -> dict[str, float]:
        fields = (
            "item_id",
            "title",
            "price",
            "currency",
            "rating",
            "sales",
            "image_url",
            "source_url",
            "attributes",
        )
        if not items:
            return {field: 0.0 for field in fields}
        return {
            field: round(
                sum(item.get(field) not in (None, "", [], {}) for item in items) / len(items),
                4,
            )
            for field in fields
        }

    def write_outputs(self, stop_reason: str | None = None) -> dict[str, Any]:
        self.config.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        repaired_candidates = self._repair_candidates_from_raw()
        ordered = sorted(
            self.candidates.values(), key=lambda item: (item["platform"], item["item_id"])
        )
        jsonl_path = self.config.normalized_dir / "headphones.jsonl"
        jsonl_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered),
            encoding="utf-8",
        )
        csv_path = self.config.normalized_dir / "headphones.csv"
        columns = (
            "item_id",
            "source_item_id",
            "platform",
            "title",
            "price",
            "currency",
            "rating",
            "sales",
            "image_url",
            "source_url",
            "captured_at",
            "attributes",
            "detail_enriched",
            "source_keyword",
            "source_page",
        )
        with csv_path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=columns)
            writer.writeheader()
            for item in ordered:
                row = dict(item)
                row["attributes"] = json.dumps(row["attributes"], ensure_ascii=False)
                writer.writerow(row)

        counts = {
            platform: sum(item["platform"] == platform for item in ordered)
            for platform in PLATFORMS
        }
        per_platform_coverage = {
            platform: self._coverage(
                [item for item in ordered if item["platform"] == platform]
            )
            for platform in PLATFORMS
        }
        complete = all(counts[p] >= self.config.targets[p] for p in PLATFORMS)
        acceptable = sum(counts.values()) >= 900 and all(
            counts[p] >= self.config.minimums[p] for p in PLATFORMS
        )
        quality = {
            "generated_at": utc_now(),
            "counts": counts,
            "total": sum(counts.values()),
            "duplicate_item_ids": len(ordered) - len({item["item_id"] for item in ordered}),
            "rejected_items_in_current_run": self.rejected_items,
            "duplicate_occurrences_in_current_run": self.duplicate_items,
            "locally_repaired_candidates": repaired_candidates,
            "coverage": self._coverage(ordered),
            "coverage_by_platform": per_platform_coverage,
            "raw_search_audit": self._raw_audit(),
            "imported_response_count": sum(
                1
                for line in (self.config.reports_dir / "import_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            )
            if (self.config.reports_dir / "import_ledger.jsonl").exists()
            else 0,
        }
        manifest = {
            "dataset": "justone_headphones",
            "generated_at": utc_now(),
            "status": "complete" if complete else "acceptable" if acceptable else "partial",
            "stop_reason": stop_reason,
            "targets": self.config.targets,
            "minimums": self.config.minimums,
            "counts": counts,
            "total": sum(counts.values()),
            "requests": self.client.ledger.summary(),
            "outputs": {"jsonl": str(jsonl_path), "csv": str(csv_path)},
            "notes": [
                "Only search endpoints were used; detail, comments, and SKU endpoints "
                "were not called.",
                "The per-call price is not public; estimated cost uses an unverified "
                "0.10 CNY assumption.",
                "Generated raw, normalized, report, and state files are local and Git-ignored.",
                "User-provided successful responses are recorded in a separate import ledger "
                "and excluded from paid request counts.",
            ],
        }
        atomic_json(self.config.reports_dir / "quality_report.json", quality)
        atomic_json(self.config.reports_dir / "manifest.json", manifest)
        return manifest

    def run(
        self, *, smoke_only: bool = False, max_search_pages: int | None = None
    ) -> dict[str, Any]:
        stop_reason: str | None = None
        try:
            self.smoke_test()
            if not smoke_only:
                stop_reason = self.collect_searches(max_pages=max_search_pages)
        except (BudgetExceeded, CollectionError) as exc:
            stop_reason = str(exc)
            manifest = self.write_outputs(stop_reason)
            if isinstance(exc, (ProviderResponseError, AuthenticationError)):
                raise
            return manifest
        return self.write_outputs(stop_reason)


def dry_run(config: CollectionConfig) -> dict[str, Any]:
    observed_page_sizes = {"taobao": 10, "jd": 48, "douyin": 20}
    expected_calls = {
        platform: (config.targets[platform] + page_size - 1) // page_size
        for platform, page_size in observed_page_sizes.items()
    }
    return {
        "platforms": list(PLATFORMS),
        "targets": config.targets,
        "target_total": sum(config.targets.values()),
        "minimums": config.minimums,
        "keywords": list(KEYWORDS),
        "search_only": True,
        "expected_calls_by_platform": expected_calls,
        "expected_success_calls": sum(expected_calls.values()),
        "max_success_calls": config.max_success_calls,
        "max_attempts": config.max_attempts,
        "assumed_search_cost_cny": str(config.assumed_search_cost_cny),
        "expected_estimated_cost_cny": str(
            (config.assumed_search_cost_cny * sum(expected_calls.values())).quantize(
                Decimal("0.01")
            )
        ),
        "estimated_budget_cny": str(config.estimated_budget_cny),
        "price_assumption_verified": False,
        "automatic_retries": 0,
    }
