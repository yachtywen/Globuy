"""Command line entry point for the OneBound headphone collector."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from datasets.onebound_headphones.collector import (
    CollectionConfig,
    CollectionError,
    OneBoundClient,
    OneBoundCollector,
    RequestLedger,
    dry_run,
    load_dotenv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低成本采集淘宝、京东耳机候选数据")
    parser.add_argument("mode", choices=("dry-run", "collect", "resume"))
    parser.add_argument(
        "--platform",
        choices=("both", "taobao", "jd"),
        default="both",
        help="只续传指定平台；默认同时采集淘宝和京东",
    )
    parser.add_argument(
        "--account",
        choices=("primary", "fallback"),
        default="primary",
        help="选择 .env 中的主账号或备用账号；默认主账号",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="只执行淘宝和京东各一次搜索烟雾测试",
    )
    parser.add_argument(
        "--retry-cached-provider-errors",
        action="store_true",
        help="仅在确认已补充额度或开通接口后，对已缓存的权限/配额错误显式重试一次",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    config = CollectionConfig()
    if args.mode == "dry-run":
        print(json.dumps(dry_run(config), ensure_ascii=False, indent=2))
        return 0

    load_dotenv(repo_root / ".env")
    if args.mode == "collect" and (config.state_dir / "collection_state.json").exists():
        raise SystemExit("检测到已有采集状态；请使用 resume，避免重复付费请求")

    ledger = RequestLedger(config.reports_dir / "request_ledger.jsonl", config)
    credential_suffix = "" if args.account == "primary" else "_FALLBACK"
    client = OneBoundClient(
        os.getenv(f"GLOBUY_ONEBOUND{credential_suffix}_KEY", ""),
        os.getenv(f"GLOBUY_ONEBOUND{credential_suffix}_SECRET", ""),
        config,
        ledger,
        retry_cached_provider_errors=args.retry_cached_provider_errors,
    )
    try:
        platforms = ("taobao", "jd") if args.platform == "both" else (args.platform,)
        collector = OneBoundCollector(client, config, platforms=platforms)
        manifest = collector.run(smoke_only=args.smoke_only)
    except CollectionError as exc:
        print(json.dumps({"status": "stopped", "reason": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        client.close()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "complete" or args.smoke_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
