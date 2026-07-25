"""Record a real RunRegistry cancellation without calling a model or provider."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from app.api.event_broker import EventBroker
from app.api.run_registry import RunRegistry
from app.api.storage import SessionStore


async def collect(output: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="globuy-p2-cancel-") as directory:
        store = SessionStore(Path(directory) / "sessions.sqlite3")
        broker = EventBroker()

        async def slow_runner(query: str, thread_id: str):
            del query, thread_id
            await asyncio.sleep(3_600)
            return "unreachable", {}

        registry = RunRegistry(
            store=store,
            broker=broker,
            agent_runner=slow_runner,
            stream_runner=None,
            session_dir=lambda thread_id: Path(directory) / thread_id,
            product_image_catalog_path=Path(directory) / "missing.jsonl",
            cancel_grace_seconds=2,
        )
        await store.open()
        try:
            thread = await store.replace_thread(
                user_id="p2-cancellation-user",
                current_thread_id=None,
                client_request_id="create-thread",
                new_thread_id="p2-cancellation-thread",
            )
            started = time.perf_counter()
            run = await registry.start_run(
                query="取消验收任务",
                thread_id=thread["thread_id"],
                user_id="p2-cancellation-user",
                client_request_id="create-run",
            )
            await registry.cancel_run(thread["thread_id"], run["run_id"])
            for _ in range(200):
                status = await registry.run_status(thread["thread_id"], run["run_id"])
                if status["status"] == "cancelled":
                    break
                await asyncio.sleep(0.01)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            record = {
                "case_id": "run-registry-cancellation",
                "query": "取消验收任务",
                "duration_ms": duration_ms,
                "expect_cancelled": True,
                "status": status["status"],
                "tool_calls": [],
            }
            existing = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            existing = [
                item for item in existing if item.get("case_id") != record["case_id"]
            ]
            output.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False) + "\n"
                    for item in [*existing, record]
                ),
                encoding="utf-8",
            )
            return record
        finally:
            await registry.close()
            await broker.close()
            await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/eval/records.jsonl"),
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(collect(args.output)), ensure_ascii=False))


if __name__ == "__main__":
    main()
