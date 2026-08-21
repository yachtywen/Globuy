"""Offline fixture and production-protocol evaluation execution."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import yaml
from pydantic import ValidationError
from sqlalchemy import or_, select

from app.config import Settings, get_settings
from app.database.models import Offer, Product
from app.database.session import Database
from app.eval.schemas import CaseEvidence, CatalogFact, EvaluationCase, EvaluationCaseFile
from app.observability import trace_id_for_run

TERMINAL_RUN_STATUSES = {"succeeded", "cancelled", "failed", "interrupted"}
TERMINAL_EVENTS = {"RUN_FINISHED", "RUN_ERROR", "TASK_CANCELLED"}


def load_case_file(path: Path) -> EvaluationCaseFile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return EvaluationCaseFile.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"评测用例文件无效：{path}: {exc}") from exc


def fixture_evidence(case: EvaluationCase) -> CaseEvidence:
    if case.fixture is None:
        raise ValueError(f"offline case {case.id} 缺少 fixture")
    payload = dict(case.fixture)
    payload.setdefault("execution_status", "succeeded")
    payload.setdefault("terminal_status", payload.get("result", {}).get("status"))
    payload.setdefault("transcript", "\n\n".join(f"[用户] {turn.query}" for turn in case.turns))
    return CaseEvidence.model_validate(payload)


def _cookie_header(cookies: httpx.Cookies) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies.jar)


async def _collect_replayed_events(
    base_url: str,
    cookies: httpx.Cookies,
    thread_id: str,
    run_id: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - live-only optional path
        raise RuntimeError("live 评测需要 websockets 依赖") from exc
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{scheme}://{parsed.netloc}/api/v1/ws/{thread_id}?run_id={run_id}&after=0"
    headers = {"Cookie": _cookie_header(cookies)}
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout_seconds):
        async with websockets.connect(ws_url, additional_headers=headers) as socket:
            async for raw in socket:
                item = json.loads(raw)
                if item.get("sequence") is not None:
                    events.append(item)
                if item.get("event") in TERMINAL_EVENTS:
                    break
    return events


async def _catalog_facts(result: dict[str, Any], settings: Settings) -> list[CatalogFact]:
    picks = result.get("picks", []) if isinstance(result, dict) else []
    picks = [pick for pick in picks if isinstance(pick, dict)]
    if not picks or settings.database_url is None:
        return []
    offer_ids = {pick.get("offer_id") for pick in picks if pick.get("offer_id")}
    item_ids = {pick.get("item_id") for pick in picks if pick.get("item_id")}
    product_ids = {pick.get("product_id") for pick in picks if pick.get("product_id")}
    database = Database(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    try:
        conditions = []
        if offer_ids:
            conditions.append(Offer.offer_id.in_(offer_ids))
        if item_ids:
            conditions.append(Offer.source_item_id.in_(item_ids))
        if product_ids:
            conditions.append(Offer.product_id.in_(product_ids))
        if not conditions:
            return []
        statement = (
            select(Offer, Product)
            .join(Product, Product.product_id == Offer.product_id)
            .where(or_(*conditions))
        )
        async with database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return [
            CatalogFact(
                item_id=offer.source_item_id,
                product_id=product.product_id,
                offer_id=offer.offer_id,
                platform=offer.platform,
                title=product.title,
                price=float(offer.current_price) if offer.current_price is not None else -1,
                currency=offer.currency,
                product_url=offer.product_url,
            )
            for offer, product in rows
            if offer.current_price is not None
        ]
    finally:
        await database.close()


class LiveEvaluationClient:
    def __init__(self, base_url: str, *, settings: Settings | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.settings = settings or get_settings()
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=30)
        self.csrf_token = ""
        self.current_thread_id: str | None = None

    async def __aenter__(self) -> LiveEvaluationClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    async def health(self) -> dict[str, Any]:
        response = await self.client.get("/healthz")
        response.raise_for_status()
        return response.json()

    async def login(self) -> None:
        email = os.getenv("GLOBUY_EVAL_EMAIL", "").strip()
        password = os.getenv("GLOBUY_EVAL_PASSWORD", "")
        if not email or not password:
            raise RuntimeError("live 评测需要 GLOBUY_EVAL_EMAIL/GLOBUY_EVAL_PASSWORD")
        response = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        response.raise_for_status()
        self.csrf_token = str(response.json()["csrf_token"])

    @property
    def write_headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf_token}

    async def _create_thread(self) -> str:
        response = await self.client.post(
            "/api/v1/threads",
            headers=self.write_headers,
            json={
                "current_thread_id": self.current_thread_id,
                "client_request_id": f"eval-thread-{uuid4().hex}",
            },
        )
        response.raise_for_status()
        self.current_thread_id = str(response.json()["thread_id"])
        return self.current_thread_id

    async def _setup_memories(self, case: EvaluationCase) -> list[str]:
        memory_ids: list[str] = []
        expected_keys = {f"eval_{case.id}_{item.key}" for item in case.setup_memories}
        if expected_keys:
            response = await self.client.get("/api/v1/memories")
            response.raise_for_status()
            for existing in response.json().get("items", []):
                if existing.get("key") in expected_keys and existing.get("memory_id"):
                    await self.client.delete(
                        f"/api/v1/memories/{existing['memory_id']}",
                        headers=self.write_headers,
                    )
        for item in case.setup_memories:
            payload = item.model_dump(mode="json")
            payload["key"] = f"eval_{case.id}_{payload['key']}"
            response = await self.client.post(
                "/api/v1/memories", headers=self.write_headers, json=payload
            )
            response.raise_for_status()
            memory_ids.append(str(response.json()["memory_id"]))
        return memory_ids

    async def _delete_memories(self, memory_ids: list[str]) -> None:
        for memory_id in memory_ids:
            response = await self.client.delete(
                f"/api/v1/memories/{memory_id}", headers=self.write_headers
            )
            if response.status_code not in {204, 404}:
                response.raise_for_status()

    async def _wait_run(self, thread_id: str, run_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.client.get(f"/api/v1/threads/{thread_id}/runs/{run_id}")
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") in TERMINAL_RUN_STATUSES:
                return payload
            await asyncio.sleep(0.5)
        await self.client.post(
            f"/api/v1/threads/{thread_id}/runs/{run_id}/cancel",
            headers=self.write_headers,
        )
        raise TimeoutError(f"run {run_id} 超过 {timeout:g}s")

    async def run_case(self, case: EvaluationCase) -> CaseEvidence:
        started = time.perf_counter()
        thread_id = await self._create_thread()
        memory_ids: list[str] = []
        all_events: list[dict[str, Any]] = []
        trace_ids: list[str] = []
        transcript: list[str] = []
        final_run: dict[str, Any] = {}
        try:
            memory_ids = await self._setup_memories(case)
            for turn in case.turns:
                response = await self.client.post(
                    "/api/v1/tasks",
                    headers=self.write_headers,
                    json={
                        "query": turn.query,
                        "thread_id": thread_id,
                        "client_request_id": f"eval-run-{uuid4().hex}",
                    },
                )
                response.raise_for_status()
                run_id = str(response.json()["run_id"])
                trace_ids.append(
                    str(response.json().get("trace_id") or trace_id_for_run(run_id))
                )
                final_run = await self._wait_run(thread_id, run_id, case.timeout_seconds)
                all_events.extend(
                    await _collect_replayed_events(
                        self.base_url,
                        self.client.cookies,
                        thread_id,
                        run_id,
                        min(case.timeout_seconds, 30),
                    )
                )
                result = final_run.get("result") or {}
                transcript.append(
                    f"[用户] {turn.query}\n[Globuy] {result.get('final_text', '')}"
                )
            result = final_run.get("result") or {}
            return CaseEvidence(
                execution_status=(
                    "succeeded" if final_run.get("status") == "succeeded" else "failed"
                ),
                terminal_status=result.get("status") or final_run.get("status"),
                result=result,
                events=all_events,
                catalog=await _catalog_facts(result, self.settings),
                transcript="\n\n".join(transcript),
                duration_ms=int((time.perf_counter() - started) * 1000),
                trace_ids=trace_ids,
                error=(final_run.get("error") or {}).get("message"),
            )
        except TimeoutError as exc:
            return CaseEvidence(
                execution_status="timeout",
                events=all_events,
                transcript="\n\n".join(transcript),
                duration_ms=int((time.perf_counter() - started) * 1000),
                trace_ids=trace_ids,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - one case must not stop the suite
            return CaseEvidence(
                execution_status="error",
                events=all_events,
                transcript="\n\n".join(transcript),
                duration_ms=int((time.perf_counter() - started) * 1000),
                trace_ids=trace_ids,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await self._delete_memories(memory_ids)


__all__ = ["LiveEvaluationClient", "fixture_evidence", "load_case_file"]
