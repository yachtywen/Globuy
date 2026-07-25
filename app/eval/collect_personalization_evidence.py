"""Collect cross-run preference evidence with a temporary MySQL QA user.

The run uses the real MemoryService, memory Outbox worker, dedicated OpenSearch
memory index, product retrieval, and deterministic ItemPicker. It never calls a
shopping provider or language model and removes the temporary user afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.auth.service import AuthService
from app.config import get_settings
from app.database.models import OutboxEvent, User
from app.database.services import MemoryService
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.memory.opensearch_store import GlobuyMemoryStore
from app.memory.outbox_worker import MemoryOutboxWorker
from app.search.encoder import get_embedding_encoder
from app.search.service import ProductSearchService
from app.tools.item_picker import item_picker

MEMORIES = (
    ("budget_500", "preference", "耳机预算不超过 500 元"),
    ("brand_sony", "preference", "耳机优先考虑索尼品牌"),
    ("reject_in_ear", "blacklist", "不要入耳式耳机"),
)
QUERY = "想买无线蓝牙耳机，预算 500 元，优先索尼，不要入耳式"


def _wearing_style(title: str) -> str:
    """Extract only explicit title evidence; unknown titles remain unspecified."""

    if "不入耳" in title or "挂耳" in title:
        return "open-ear"
    if "入耳" in title:
        return "in-ear"
    if "头戴" in title:
        return "over-ear"
    return "unspecified"


def _picker_candidates(search: ProductSearchService) -> list[dict[str, Any]]:
    candidates = []
    for platform in ("taobao", "jingdong", "douyin"):
        result = search.search("无线蓝牙耳机", platform, top_k=5)
        for item in result.candidates:
            payload = item.model_dump(exclude={"source_kind", "data_as_of"})
            payload["attributes"] = {
                **payload["attributes"],
                "wearing_style": _wearing_style(item.title),
                "wearing_style_evidence": "title-rule-v1",
            }
            candidates.append(payload)
    return candidates


def _record(
    case_id: str,
    duration_ms: float,
    expected_keys: list[str],
    recalled_keys: list[str],
    picks: dict[str, Any],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "query": QUERY,
        "duration_ms": duration_ms,
        "expected_memory_keys": expected_keys,
        "recalled_memory_keys": recalled_keys,
        "picker_top3": [item["item_id"] for item in picks["picks"]],
        "picker_rejected": picks["rejected_brief"],
        "tool_calls": [{"name": "item_picker", "status": picks["status"]}],
        "status": "succeeded",
    }


async def _cleanup_qa_user(
    database: Database,
    client: Any,
    memory_index: str,
    user_id: str,
    memory_ids: list[str],
) -> bool:
    await asyncio.to_thread(
        client.delete_by_query,
        index=memory_index,
        body={"query": {"term": {"user_id": user_id}}},
        refresh=True,
        conflicts="proceed",
    )
    async with database.sessions.begin() as session:
        if memory_ids:
            await session.execute(
                delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(memory_ids))
            )
        await session.execute(delete(User).where(User.user_id == user_id))
    async with database.sessions() as session:
        remaining = await session.scalar(
            select(func.count()).select_from(User).where(User.user_id == user_id)
        )
    return remaining == 0


async def collect(output: Path) -> dict[str, Any]:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    client = build_opensearch_client(settings)
    encoder = get_embedding_encoder()
    auth = AuthService(database, settings)
    service = MemoryService(database)
    worker = MemoryOutboxWorker(database)
    store = GlobuyMemoryStore(
        database,
        service,
        client,
        encoder,
        settings.opensearch_memory_index,
    )
    user_id: str | None = None
    memory_ids: list[str] = []
    try:
        issued = await auth.register(
            f"globuy-p2-{uuid4().hex}@example.invalid",
            f"Qa-{uuid4().hex}-9!",
            "P2 temporary QA",
            uuid4().hex,
        )
        user_id = issued.principal.user_id
        await worker.ensure_index()
        namespace = ("users", user_id, "memories")
        started = time.perf_counter()
        before_memories = await store.asearch(namespace, query=QUERY, limit=10)
        candidates = _picker_candidates(ProductSearchService(client, encoder, settings))
        before_picks = item_picker.invoke({"items": candidates, "limit": 3})
        before_duration = round((time.perf_counter() - started) * 1000, 2)

        skills = await service.list_skills(user_id)
        digital_skill_id = next(
            item["skill_id"] for item in skills if item["name"] == "数码设备"
        )
        for key, category, content in MEMORIES:
            created = await service.create(
                user_id,
                category=category,
                key=key,
                content=content,
                confidence=Decimal("1"),
                source_thread_id=None,
                source_run_id=None,
                skill_id=digital_skill_id,
                source="agent_confirmed",
            )
            memory_ids.append(created["memory_id"])
        publish_result = await worker.run_once()

        started = time.perf_counter()
        after_memories = await store.asearch(namespace, query=QUERY, limit=10)
        after_picks = item_picker.invoke(
            {
                "items": candidates,
                "constraints": {
                    "max_price": 500,
                    "excluded_attributes": {"wearing_style": ["in-ear"]},
                },
                "limit": 3,
            }
        )
        after_duration = round((time.perf_counter() - started) * 1000, 2)
        expected_keys = [item[0] for item in MEMORIES]
        records = [
            _record(
                "personalization-before-confirmation",
                before_duration,
                [],
                [item.key for item in before_memories],
                before_picks,
            ),
            _record(
                "personalization-after-confirmation",
                after_duration,
                expected_keys,
                [item.key for item in after_memories],
                after_picks,
            ),
        ]
        existing = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        case_ids = {record["case_id"] for record in records}
        existing = [record for record in existing if record.get("case_id") not in case_ids]
        output.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in [*existing, *records]
            ),
            encoding="utf-8",
        )
        evidence = {
            "query": QUERY,
            "memory_keys": expected_keys,
            "before_recalled": records[0]["recalled_memory_keys"],
            "after_recalled": records[1]["recalled_memory_keys"],
            "before_picker_top3": records[0]["picker_top3"],
            "after_picker_top3": records[1]["picker_top3"],
            "title_attribute_rule": "title-rule-v1",
            "outbox": publish_result,
        }
        evidence["qa_user_removed"] = await _cleanup_qa_user(
            database,
            client,
            settings.opensearch_memory_index,
            user_id,
            memory_ids,
        )
        user_id = None
        evidence_path = output.with_name("personalization-evidence.json")
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return evidence
    finally:
        if user_id is not None:
            await _cleanup_qa_user(
                database,
                client,
                settings.opensearch_memory_index,
                user_id,
                memory_ids,
            )
        await database.close()


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
