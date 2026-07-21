"""Resumable, budget-capped collection of Taobao and JD headphone offers."""

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

Platform = Literal["taobao", "jd"]

PLATFORMS: tuple[Platform, ...] = ("taobao", "jd")
KEYWORDS = ("耳机", "蓝牙耳机", "头戴式耳机", "降噪耳机", "入耳式耳机")
POSITIVE_TERMS = ("耳机", "耳麦", "airpods", "earbuds", "tws")
ACCESSORY_TERMS = (
    "保护套",
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
    "维修",
)
SEARCH_COST = Decimal("0.022")
DETAIL_COSTS: dict[Platform, Decimal] = {
    "taobao": Decimal("0.023"),
    "jd": Decimal("0.022"),
}


class CollectionError(RuntimeError):
    """Base error for a safe, non-retrying collection stop."""


class BudgetExceeded(CollectionError):
    """Raised before a request that would exceed a hard safety limit."""


class AuthenticationError(CollectionError):
    """Raised when credentials are missing or rejected."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs without printing or returning secrets."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def redact(value: Any) -> Any:
    """Remove credentials recursively before persistence."""

    sensitive = {"key", "secret", "api_key", "api_secret", "apikey", "apisecret"}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in sensitive else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(key|secret)=([^&\s]+)", r"\1=[REDACTED]", value)
        return re.sub(r"(?i)(key|secret)\[[^\]]+\]", r"\1[REDACTED]", value)
    return value


def classify_api_error(payload: dict[str, Any]) -> str:
    """Classify a provider error without persisting its credential-bearing message."""

    error_code = str(payload.get("error_code", "0000"))
    message = " ".join(
        str(payload.get(field) or "") for field in ("error", "reason", "error_msg", "msg")
    ).lower()
    if error_code in {"0000", "2000"}:
        return "ok"
    if "无权访问" in message or "开通接口" in message:
        return "interface_not_enabled"
    if "已超量" in message or "超量" in message:
        return "quota_exceeded"
    if error_code == "5000" or "data error" in message:
        return "provider_data_error"
    if error_code in {"4004", "4005", "4016"}:
        return "authentication_balance_or_permission_error"
    return "provider_api_error"


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


def request_key(platform: Platform, endpoint: str, params: dict[str, Any]) -> str:
    public_params = {key: params[key] for key in sorted(params) if key not in {"key", "secret"}}
    material = f"{platform}|{endpoint}|{urlencode(public_params)}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


@dataclass(slots=True)
class CollectionConfig:
    root: Path = Path(__file__).resolve().parent
    target_per_platform: int = 500
    minimum_per_platform: int = 450
    page_size: int = 40
    max_pages_per_keyword: int = 10
    detail_per_platform: int = 10
    max_search_calls: int = 70
    max_detail_calls: int = 20
    budget_cny: Decimal = Decimal("2.00")
    request_interval_seconds: float = 1.0
    request_timeout_seconds: float = 30.0

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
    """Append-only reservations prevent duplicate paid calls after crashes."""

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
        safe_item = redact(item)
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(safe_item, ensure_ascii=False, default=str) + "\n")

    @property
    def search_calls(self) -> int:
        return sum(item["endpoint"] == "item_search" for item in self._reserved.values())

    @property
    def detail_calls(self) -> int:
        return sum(item["endpoint"] == "item_get" for item in self._reserved.values())

    @property
    def estimated_cost(self) -> Decimal:
        return sum(
            (Decimal(item["estimated_cost_cny"]) for item in self._reserved.values()),
            start=Decimal("0"),
        )

    def contains(self, key: str) -> bool:
        return key in self._reserved

    def reserve(
        self,
        key: str,
        platform: Platform,
        endpoint: str,
        public_params: dict[str, Any],
        cost: Decimal,
    ) -> None:
        if key in self._reserved:
            raise CollectionError(f"请求 {key} 已登记，拒绝自动重复调用")
        if endpoint == "item_search" and self.search_calls >= self.config.max_search_calls:
            raise BudgetExceeded("已达到搜索调用次数上限")
        if endpoint == "item_get" and self.detail_calls >= self.config.max_detail_calls:
            raise BudgetExceeded("已达到详情调用次数上限")
        if self.estimated_cost + cost > self.config.budget_cny:
            raise BudgetExceeded("下一次调用将超过估算费用上限")
        record = {
            "event": "reserved",
            "request_key": key,
            "platform": platform,
            "endpoint": endpoint,
            "params": public_params,
            "estimated_cost_cny": str(cost),
            "timestamp": utc_now(),
        }
        self._append(record)
        self._reserved[key] = record

    def finish(self, key: str, **result: Any) -> None:
        record = {"event": "finished", "request_key": key, "timestamp": utc_now(), **result}
        self._append(record)
        self._finished[key] = record

    def summary(self) -> dict[str, Any]:
        failed = [item for item in self._finished.values() if item.get("status") != "ok"]
        incomplete = [key for key in self._reserved if key not in self._finished]
        error_codes: dict[str, int] = {}
        for item in failed:
            code = str(item.get("error_code") or item.get("status") or "unknown")
            error_codes[code] = error_codes.get(code, 0) + 1
        return {
            "search_calls": self.search_calls,
            "detail_calls": self.detail_calls,
            "total_calls": self.search_calls + self.detail_calls,
            "estimated_cost_cny": str(self.estimated_cost.quantize(Decimal("0.001"))),
            "max_search_calls": self.config.max_search_calls,
            "max_detail_calls": self.config.max_detail_calls,
            "budget_cny": str(self.config.budget_cny),
            "failed_calls": len(failed),
            "incomplete_calls": len(incomplete),
            "error_codes": error_codes,
        }


class OneBoundClient:
    def __init__(
        self,
        key: str,
        secret: str,
        config: CollectionConfig,
        ledger: RequestLedger,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_cached_provider_errors: bool = False,
    ) -> None:
        if not key or not secret:
            raise AuthenticationError(
                "请在 .env 中配置 GLOBUY_ONEBOUND_KEY 和 GLOBUY_ONEBOUND_SECRET"
            )
        self._key = key
        self._secret = secret
        self.config = config
        self.ledger = ledger
        self.retry_cached_provider_errors = retry_cached_provider_errors
        self._client = httpx.Client(
            base_url="https://api-gw.onebound.cn",
            timeout=config.request_timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
        )

    def close(self) -> None:
        self._client.close()

    def _raw_path(
        self, platform: Platform, endpoint: str, params: dict[str, Any], key: str
    ) -> Path:
        category = "search" if endpoint == "item_search" else "detail"
        if endpoint == "item_search":
            stem = f"p{params.get('page', 1):03d}-{key}"
        else:
            safe_id = re.sub(r"[^0-9A-Za-z_-]", "-", str(params.get("num_iid", "unknown")))
            stem = f"{safe_id}-{key}"
        return self.config.raw_dir / platform / category / f"{stem}.json"

    def call(
        self,
        platform: Platform,
        endpoint: Literal["item_search", "item_get"],
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        cost = SEARCH_COST if endpoint == "item_search" else DETAIL_COSTS[platform]
        public_params = dict(params)
        key = request_key(platform, endpoint, public_params)
        raw_path = self._raw_path(platform, endpoint, public_params, key)
        if raw_path.exists():
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            if (
                self.retry_cached_provider_errors
                and classify_api_error(payload)
                in {
                    "interface_not_enabled",
                    "quota_exceeded",
                    "authentication_balance_or_permission_error",
                }
            ):
                retry_params = {**public_params, "_provider_retry": 1}
                key = request_key(platform, endpoint, retry_params)
                raw_path = self._raw_path(platform, endpoint, public_params, key)
                if raw_path.exists():
                    retry_payload = json.loads(raw_path.read_text(encoding="utf-8"))
                    return self._validated_payload(platform, endpoint, retry_payload)
                if self.ledger.contains(key):
                    return None
                ledger_params = retry_params
            else:
                return self._validated_payload(platform, endpoint, payload)
        else:
            ledger_params = public_params
        if self.ledger.contains(key):
            return None

        self.ledger.reserve(key, platform, endpoint, ledger_params, cost)
        request_params = {**public_params, "key": self._key, "secret": self._secret, "cache": "yes"}
        try:
            response = self._client.get(f"/{platform}/{endpoint}/", params=request_params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.ledger.finish(
                key,
                status="transport_or_decode_error",
                error_type=type(exc).__name__,
            )
            raise CollectionError(
                f"{platform}.{endpoint} 调用失败（{type(exc).__name__}），为节省额度不自动重试"
            ) from None

        safe_payload = redact(payload)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        error_code = str(safe_payload.get("error_code", "0000"))
        status = "ok" if error_code in {"0000", "2000"} else "api_error"
        self.ledger.finish(key, status=status, error_code=error_code, raw_file=str(raw_path))
        time.sleep(self.config.request_interval_seconds)
        return self._validated_payload(platform, endpoint, safe_payload)

    @staticmethod
    def _validated_payload(
        platform: Platform,
        endpoint: Literal["item_search", "item_get"],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return usable data and never reissue a paid request for a cached API error."""

        error_code = str(payload.get("error_code", "0000"))
        category = classify_api_error(payload)
        if category in {
            "interface_not_enabled",
            "quota_exceeded",
            "authentication_balance_or_permission_error",
        }:
            labels = {
                "interface_not_enabled": "接口未开通",
                "quota_exceeded": "供应商调用额度已超量",
                "authentication_balance_or_permission_error": "鉴权、余额或权限检查失败",
            }
            raise AuthenticationError(f"{platform}.{endpoint} {labels[category]}: {error_code}")
        if category != "ok":
            return None
        return payload


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", -1, "-1"):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace("+", ""))
    except ValueError:
        return None


def _absolute_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    return "https:" + url if url.startswith("//") else url


def is_headphone_title(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in POSITIVE_TERMS) and not any(
        term in lowered for term in ACCESSORY_TERMS
    )


def search_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    container = payload.get("items")
    if not isinstance(container, dict):
        return [], {}
    items = container.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return ([item for item in items if isinstance(item, dict)], container)


def normalize_search_item(
    platform: Platform,
    item: dict[str, Any],
    *,
    keyword: str,
    page: int,
    captured_at: str,
) -> dict[str, Any] | None:
    source_id = item.get("num_iid")
    title = str(item.get("title") or "").strip()
    price = _decimal(item.get("promotion_price") or item.get("price"))
    source_url = _absolute_url(item.get("detail_url"))
    if not source_id or not title or price is None or source_url is None:
        return None
    if not is_headphone_title(title):
        return None
    return {
        "item_id": f"{platform}:{source_id}",
        "source_item_id": str(source_id),
        "platform": platform,
        "title": title,
        "price": str(price),
        "currency": "CNY",
        "rating": None,
        "sales": _integer(item.get("sales")),
        "image_url": _absolute_url(item.get("pic_url")),
        "source_url": source_url,
        "captured_at": captured_at,
        "attributes": {},
        "detail_enriched": False,
        "source_keyword": keyword,
        "source_page": page,
    }


def detail_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item", payload)
    if not isinstance(item, dict):
        return {}
    attributes: dict[str, Any] = {}
    for source, target in (
        ("brand", "brand"),
        ("cid", "category_id"),
        ("rootCatId", "root_category_id"),
        ("item_weight", "item_weight"),
        ("item_size", "item_size"),
    ):
        value = item.get(source)
        if value not in (None, "", [], {}):
            attributes[target] = value
    props_list = item.get("props_list")
    if isinstance(props_list, dict):
        for value in props_list.values():
            if not isinstance(value, str) or ":" not in value:
                continue
            name, content = value.split(":", 1)
            if name and content:
                attributes[name] = content
    props_name = item.get("props_name")
    if props_name and not attributes:
        attributes["props_name"] = props_name
    skus = item.get("skus", {})
    if isinstance(skus, dict):
        sku_items = skus.get("sku", [])
        if isinstance(sku_items, list):
            attributes["sku_count"] = len(sku_items)
    return attributes


@dataclass(slots=True)
class PlatformProgress:
    keyword_index: int = 0
    next_page: int = 1
    stalled_pages: int = 0
    complete: bool = False


@dataclass(slots=True)
class CollectionState:
    smoke_complete: bool = False
    details_complete: bool = False
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
            details_complete=bool(raw.get("details_complete")),
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
                "details_complete": self.details_complete,
                "platforms": {
                    platform: asdict(progress) for platform, progress in self.platforms.items()
                },
                "updated_at": utc_now(),
            },
        )


class OneBoundCollector:
    def __init__(
        self,
        client: OneBoundClient,
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
        self.candidates: dict[str, dict[str, Any]] = self._load_candidates()
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

    def _search(self, platform: Platform, keyword: str, page: int) -> tuple[int, int | None]:
        params: dict[str, Any] = {
            "q": keyword,
            "page": page,
            "page_size": self.config.page_size,
        }
        if platform == "jd":
            params.update(
                {
                    "start_price": 0,
                    "end_price": 0,
                    "cat": 0,
                    "discount_only": "",
                    "sort": "",
                    "seller_info": "no",
                    "nick": "",
                    "ppath": "",
                    "imgid": "",
                    "filter": "",
                }
            )
        payload = self.client.call(
            platform,
            "item_search",
            params,
        )
        if payload is None:
            return 0, None
        raw_items, container = search_items(payload)
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
            if self.platform_count(platform) >= self.config.target_per_platform:
                break
            self.candidates[candidate["item_id"]] = candidate
            added += 1
        page_count = _integer(container.get("pagecount"))
        self._checkpoint()
        return added, page_count

    def smoke_test(self) -> None:
        if self.state.smoke_complete and self.platforms == PLATFORMS:
            return
        for platform in self.platforms:
            progress = self.state.platforms[platform]
            if self.platform_count(platform) > 0:
                progress.next_page = max(progress.next_page, 2)
                continue
            added, _ = self._search(platform, KEYWORDS[0], 1)
            if added == 0:
                raise CollectionError(f"{platform} 烟雾测试未得到有效耳机候选，停止批量采集")
            progress.next_page = 2
        self.state.smoke_complete = all(self.platform_count(platform) > 0 for platform in PLATFORMS)
        self._checkpoint()

    def collect_searches(self) -> str | None:
        """Collect one page per platform per round so one source cannot consume the budget."""

        while True:
            made_progress = False
            for platform in self.platforms:
                progress = self.state.platforms[platform]
                if self.platform_count(platform) >= self.config.target_per_platform:
                    progress.complete = True
                    self._checkpoint()
                    continue
                if progress.keyword_index >= len(KEYWORDS):
                    progress.complete = True
                    self._checkpoint()
                    continue
                if self.client.ledger.search_calls >= self.config.max_search_calls:
                    return "已达到搜索调用次数上限，已保留详情抽样预算"
                made_progress = True
                keyword = KEYWORDS[progress.keyword_index]
                page = progress.next_page
                added, page_count = self._search(platform, keyword, page)
                progress.next_page += 1
                progress.stalled_pages = progress.stalled_pages + 1 if added == 0 else 0
                if (
                    (page_count is not None and page >= page_count)
                    or page >= self.config.max_pages_per_keyword
                    or progress.stalled_pages >= 2
                ):
                    progress.keyword_index += 1
                    progress.next_page = 1
                    progress.stalled_pages = 0
                self._checkpoint()
            if not made_progress:
                return None

    @staticmethod
    def _detail_sample(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        pending = [
            item
            for item in items
            if not item.get("detail_enriched") and not item.get("_detail_attempted")
        ]
        pending.sort(key=lambda item: (Decimal(item["price"]), item["item_id"]))
        if len(pending) <= count:
            return pending
        if count == 1:
            return [pending[len(pending) // 2]]

        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        for keyword in KEYWORDS:
            group = [item for item in pending if item.get("source_keyword") == keyword]
            for fraction in (Decimal("0.33"), Decimal("0.67")):
                if not group or len(selected) >= count:
                    break
                index = round(float(fraction) * (len(group) - 1))
                candidate = group[index]
                if candidate["item_id"] not in selected_ids:
                    selected.append(candidate)
                    selected_ids.add(candidate["item_id"])

        indexes = [round(index * (len(pending) - 1) / (count - 1)) for index in range(count)]
        for index in indexes:
            candidate = pending[index]
            if candidate["item_id"] not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate["item_id"])
            if len(selected) >= count:
                break
        return selected

    def _provider_failure_summary(self) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], int] = {}
        if not self.config.raw_dir.exists():
            return []
        for path in self.config.raw_dir.rglob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            category = classify_api_error(payload)
            if category == "ok":
                continue
            relative = path.relative_to(self.config.raw_dir)
            if len(relative.parts) < 2:
                continue
            platform = relative.parts[0]
            endpoint = "item_search" if relative.parts[1] == "search" else "item_get"
            error_code = str(payload.get("error_code") or "unknown")
            group_key = (f"{platform}.{endpoint}", error_code, category)
            grouped[group_key] = grouped.get(group_key, 0) + 1
        return [
            {
                "endpoint": endpoint,
                "error_code": error_code,
                "category": category,
                "count": count,
            }
            for (endpoint, error_code, category), count in sorted(grouped.items())
        ]

    def _raw_search_audit(self) -> dict[str, Any]:
        successful_responses = 0
        response_item_counts: list[int] = []
        eligible_ids: list[str] = []
        rejected = 0
        if not self.config.raw_dir.exists():
            return {
                "successful_responses": 0,
                "raw_items": 0,
                "eligible_items": 0,
                "unique_eligible_ids": 0,
                "duplicate_occurrences": 0,
                "duplicate_rate": 0.0,
                "rejected_items": 0,
                "items_per_response": {"minimum": 0, "maximum": 0, "average": 0.0},
            }

        for path in self.config.raw_dir.glob("*/search/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if classify_api_error(payload) != "ok":
                continue
            platform = path.relative_to(self.config.raw_dir).parts[0]
            if platform not in PLATFORMS:
                continue
            raw_items, _ = search_items(payload)
            successful_responses += 1
            response_item_counts.append(len(raw_items))
            call_args = (
                payload.get("call_args")
                if isinstance(payload.get("call_args"), dict)
                else {}
            )
            keyword = str(call_args.get("q") or "")
            page = _integer(call_args.get("page")) or 1
            for raw_item in raw_items:
                candidate = normalize_search_item(
                    platform,
                    raw_item,
                    keyword=keyword,
                    page=page,
                    captured_at="audit",
                )
                if candidate is None:
                    rejected += 1
                else:
                    eligible_ids.append(candidate["item_id"])

        raw_items_total = sum(response_item_counts)
        unique_count = len(set(eligible_ids))
        duplicates = len(eligible_ids) - unique_count
        return {
            "successful_responses": successful_responses,
            "raw_items": raw_items_total,
            "eligible_items": len(eligible_ids),
            "unique_eligible_ids": unique_count,
            "duplicate_occurrences": duplicates,
            "duplicate_rate": round(duplicates / len(eligible_ids), 4) if eligible_ids else 0.0,
            "rejected_items": rejected,
            "items_per_response": {
                "minimum": min(response_item_counts, default=0),
                "maximum": max(response_item_counts, default=0),
                "average": round(raw_items_total / successful_responses, 2)
                if successful_responses
                else 0.0,
            },
        }

    def enrich_details(self) -> None:
        if self.state.details_complete:
            return
        for platform in self.platforms:
            platform_items = [
                item for item in self.candidates.values() if item["platform"] == platform
            ]
            already = sum(item.get("detail_enriched", False) for item in platform_items)
            needed = max(0, self.config.detail_per_platform - already)
            for candidate in self._detail_sample(platform_items, needed):
                candidate["_detail_attempted"] = True
                self._checkpoint()
                payload = self.client.call(
                    platform,
                    "item_get",
                    {"num_iid": candidate["source_item_id"]},
                )
                if payload is None:
                    candidate["_detail_error"] = "request_failed_or_reserved"
                    self._checkpoint()
                    continue
                candidate["attributes"] = detail_attributes(payload)
                candidate["detail_enriched"] = True
                candidate["detail_captured_at"] = utc_now()
                self._checkpoint()
        self.state.details_complete = all(
            sum(
                item.get("detail_enriched", False)
                for item in self.candidates.values()
                if item["platform"] == platform
            )
            >= self.config.detail_per_platform
            for platform in PLATFORMS
        )
        self._checkpoint()

    def write_outputs(self, stop_reason: str | None = None) -> dict[str, Any]:
        stored_items = sorted(
            self.candidates.values(),
            key=lambda item: (item["platform"], item["item_id"]),
        )
        items = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in stored_items
        ]
        self.config.normalized_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = self.config.normalized_dir / "headphones.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as target:
            for item in items:
                target.write(json.dumps(item, ensure_ascii=False) + "\n")

        csv_path = self.config.normalized_dir / "headphones.csv"
        fieldnames = [
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
        ]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for item in items:
                row = dict(item)
                row["attributes"] = json.dumps(row["attributes"], ensure_ascii=False)
                writer.writerow(row)

        counts = {platform: self.platform_count(platform) for platform in PLATFORMS}
        detail_counts = {
            platform: sum(
                item.get("detail_enriched", False)
                for item in items
                if item["platform"] == platform
            )
            for platform in PLATFORMS
        }

        def coverage(field_name: str) -> float:
            if not items:
                return 0.0
            present = sum(item.get(field_name) not in (None, "", [], {}) for item in items)
            return round(present / len(items), 4)

        quality = {
            "generated_at": utc_now(),
            "total_unique": len(items),
            "platform_counts": counts,
            "detail_enriched_counts": detail_counts,
            "field_coverage": {
                field_name: coverage(field_name)
                for field_name in (
                    "title",
                    "price",
                    "source_url",
                    "image_url",
                    "sales",
                    "attributes",
                )
            },
            "duplicate_ids": len(items) - len({item["item_id"] for item in items}),
            "duplicates_skipped_this_run": self.duplicate_items,
            "rejected_this_run": self.rejected_items,
            "raw_search_audit": self._raw_search_audit(),
        }
        manifest = {
            "dataset": "onebound_headphones",
            "generated_at": utc_now(),
            "status": "complete"
            if all(counts[p] >= self.config.minimum_per_platform for p in PLATFORMS)
            else "partial",
            "stop_reason": stop_reason,
            "targets": {
                "target_per_platform": self.config.target_per_platform,
                "minimum_per_platform": self.config.minimum_per_platform,
                "detail_per_platform": self.config.detail_per_platform,
            },
            "counts": counts,
            "detail_counts": detail_counts,
            "requests": self.client.ledger.summary(),
            "provider_failures": self._provider_failure_summary(),
            "outputs": {
                "jsonl": str(jsonl_path),
                "csv": str(csv_path),
            },
            "price_assumptions_cny": {
                "item_search": str(SEARCH_COST),
                "taobao.item_get": str(DETAIL_COSTS["taobao"]),
                "jd.item_get": str(DETAIL_COSTS["jd"]),
            },
        }
        atomic_json(self.config.reports_dir / "quality_report.json", quality)
        atomic_json(self.config.reports_dir / "manifest.json", manifest)
        return manifest

    def run(self, *, smoke_only: bool = False) -> dict[str, Any]:
        stop_reason: str | None = None
        try:
            self.smoke_test()
            if not smoke_only:
                stop_reason = self.collect_searches()
                self.enrich_details()
        except (BudgetExceeded, CollectionError) as exc:
            stop_reason = str(exc)
            manifest = self.write_outputs(stop_reason)
            if isinstance(exc, BudgetExceeded):
                return manifest
            raise
        return self.write_outputs(stop_reason)


def dry_run(config: CollectionConfig) -> dict[str, Any]:
    maximum = SEARCH_COST * config.max_search_calls + sum(
        DETAIL_COSTS[platform] * config.detail_per_platform for platform in PLATFORMS
    )
    return {
        "platforms": list(PLATFORMS),
        "target_per_platform": config.target_per_platform,
        "target_total": config.target_per_platform * len(PLATFORMS),
        "keywords": list(KEYWORDS),
        "page_size": config.page_size,
        "max_pages_per_keyword": config.max_pages_per_keyword,
        "detail_per_platform": config.detail_per_platform,
        "max_search_calls": config.max_search_calls,
        "max_detail_calls": config.max_detail_calls,
        "budget_cny": str(config.budget_cny),
        "calculated_maximum_cny": str(maximum.quantize(Decimal("0.001"))),
        "automatic_retries": 0,
    }
