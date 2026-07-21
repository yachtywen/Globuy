"""Command-line entry point for the Just One API headphone collector."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from datasets.justone_headphones.collector import (
    PLATFORMS,
    CollectionConfig,
    CollectionError,
    JustOneClient,
    JustOneCollector,
    RequestLedger,
    dry_run,
    load_dotenv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="低成本采集淘宝、京东、抖音商城耳机候选数据"
    )
    parser.add_argument(
        "mode", choices=("dry-run", "collect", "resume", "import-response")
    )
    parser.add_argument(
        "--platform",
        choices=("all", *PLATFORMS),
        default="all",
        help="只续传指定平台；默认三个平台",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="只执行三个平台各一页搜索烟雾测试",
    )
    parser.add_argument(
        "--single-page",
        action="store_true",
        help="只续传一页，用于批量采集前的受控验证；必须指定单个平台",
    )
    parser.add_argument(
        "--retry-nonbillable-errors",
        action="store_true",
        help="显式重试一次已缓存的非计费业务错误；默认不重试",
    )
    parser.add_argument(
        "--response-file",
        type=Path,
        help="import-response 模式下要导入的成功 JSON 响应",
    )
    parser.add_argument(
        "--keyword",
        default="耳机",
        help="导入响应对应的搜索关键词；默认耳机",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="导入响应对应的页码；默认 1",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        help="仅单平台续传时临时覆盖该平台的候选目标，用于受控补量",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    config = CollectionConfig()
    if args.target_count is not None:
        if args.platform == "all" or args.target_count <= 0:
            raise SystemExit("--target-count 必须配合单个平台和正整数使用")
        config.targets[args.platform] = args.target_count
        config.minimums[args.platform] = min(
            config.minimums[args.platform], args.target_count
        )
    if args.mode == "dry-run":
        print(json.dumps(dry_run(config), ensure_ascii=False, indent=2))
        return 0

    load_dotenv(config.root / ".env")
    load_dotenv(repo_root / ".env")
    if args.mode == "collect" and (config.state_dir / "collection_state.json").exists():
        raise SystemExit("检测到已有采集状态；请使用 resume，避免重复付费请求")

    ledger = RequestLedger(config.reports_dir / "request_ledger.jsonl", config)
    client = JustOneClient(
        os.getenv("GLOBUY_JUSTONE_TOKEN", ""),
        config,
        ledger,
        retry_nonbillable_errors=args.retry_nonbillable_errors,
    )
    try:
        platforms = PLATFORMS if args.platform == "all" else (args.platform,)
        collector = JustOneCollector(client, config, platforms=platforms)
        if args.mode == "import-response":
            if args.platform == "all" or args.response_file is None:
                raise CollectionError(
                    "import-response 必须指定单个平台和 --response-file"
                )
            manifest = collector.import_response(
                args.platform,
                args.response_file,
                keyword=args.keyword,
                page=args.page,
            )
        else:
            if args.single_page and args.platform == "all":
                raise CollectionError("--single-page 必须配合单个平台使用")
            manifest = collector.run(
                smoke_only=args.smoke_only,
                max_search_pages=1 if args.single_page else None,
            )
    except CollectionError as exc:
        print(json.dumps({"status": "stopped", "reason": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        client.close()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return (
        0
        if manifest["status"] in {"complete", "acceptable"}
        or args.smoke_only
        or args.mode == "import-response"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
