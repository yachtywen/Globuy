"""FastAPI application for persistent sessions and replayable background Agent runs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Header,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse as DownloadResponse
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.agent.main_agent import main_agent, run_agent, run_agent_stream
from app.api.auth_routes import router as auth_router
from app.api.connection import ConnectionManager, connection_manager
from app.api.context import bind_context
from app.api.domain_routes import (
    memory_service as memory_service_dependency,
)
from app.api.domain_routes import (
    router as domain_router,
)
from app.api.domain_routes import (
    wishlist_service as wishlist_service_dependency,
)
from app.api.errors import (
    ApiError,
    api_error_handler,
    http_error_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.api.event_broker import EventBroker
from app.api.run_registry import AgentRunner, AgentStreamRunner, RunRegistry
from app.api.schemas import (
    ArtifactListResponse,
    ChatRequest,
    ChatResponse,
    CreateTaskRequest,
    CreateThreadRequest,
    FileResponse,
)
from app.api.storage import SessionStore
from app.auth.service import AuthService
from app.config import Settings, get_settings
from app.database.services import MemoryService, WishlistService
from app.database.session import Database
from app.database.session_store import SQLAlchemySessionStore
from app.memory.postgres_store import PostgresMemoryStore
from app.observability import ObservabilityManager
from app.search.catalog_images import enrich_task_result
from app.search.encoder import get_embedding_encoder
from app.utils.path_utils import session_path, upload_path

_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_thread_id(value: str | None) -> str:
    """Compatibility-only ID helper; formal endpoints reject invalid identifiers."""

    if value and _THREAD_ID.fullmatch(value):
        return value
    return uuid4().hex


def _session_dir(settings: Settings, thread_id: str) -> Path:
    return session_path(settings.output_dir, thread_id)


def _database_path(settings: Settings) -> Path:
    default = Path("output/globuy-sessions.sqlite3")
    if settings.session_db_path == default and settings.output_dir != Path("output"):
        return settings.output_dir / default.name
    return settings.session_db_path


def _artifact_public(item: dict[str, Any], thread_id: str, run_id: str) -> dict[str, Any]:
    return {
        "file_id": item["file_id"],
        "filename": item["filename"],
        "kind": item["kind"],
        "media_type": item["media_type"],
        "size": item["size"],
        "created_at": item["created_at"],
        "download_url": (f"/api/v1/threads/{thread_id}/runs/{run_id}/files/{item['file_id']}"),
    }


def create_app(
    settings: Settings | None = None,
    agent_runner: AgentRunner = run_agent,
    agent_stream_runner: AgentStreamRunner | None = None,
    connections: ConnectionManager = connection_manager,
) -> FastAPI:
    settings = settings or get_settings()
    del connections  # The formal protocol uses EventBroker subscriptions, not a global socket map.
    stream_runner = (
        agent_stream_runner
        if agent_stream_runner is not None
        else (run_agent_stream if agent_runner is run_agent else None)
    )
    database: Database | None = None
    auth_service: AuthService | None = None
    wishlist_service: WishlistService | None = None
    memory_service: MemoryService | None = None
    if settings.database_url is not None:
        database = Database(
            settings.database_url.get_secret_value(),
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            pool_recycle=settings.database_pool_recycle_seconds,
        )
        store = SQLAlchemySessionStore(database)
        auth_service = AuthService(database, settings)
        wishlist_service = WishlistService(
            database,
            refresh_hours=settings.price_refresh_interval_hours,
            refresh_local_hour=settings.price_refresh_local_hour,
        )
        memory_service = MemoryService(database)
        if agent_runner is run_agent:
            main_agent.store = PostgresMemoryStore(
                database,
                memory_service,
                get_embedding_encoder(),
                settings,
            )
    else:
        # Kept only for isolated legacy tests and explicit local diagnostics.
        store = SessionStore(_database_path(settings))
        if agent_runner is run_agent:
            main_agent.store = None
    broker = EventBroker(
        buffer_size=settings.event_buffer_size,
        retention_seconds=settings.event_retention_seconds,
        subscriber_queue_size=settings.ws_subscriber_queue_size,
    )
    observability = ObservabilityManager(settings)

    async def persist_memory_candidates(
        user_id: str,
        thread_id: str,
        run_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if memory_service is None:
            return []
        persisted: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                confidence = Decimal(str(candidate.get("confidence", 0)))
                category = str(candidate.get("category") or "")
                key = str(candidate.get("key") or "").strip()
                content = str(candidate.get("content") or "").strip()
                if (
                    confidence < Decimal(str(settings.memory_candidate_min_confidence))
                    or category not in {"blacklist", "preference", "history"}
                    or not key
                    or not content
                ):
                    continue
                persisted.append(
                    await memory_service.create_candidate(
                        user_id,
                        category=category,
                        key=key[:128],
                        content=content[:4000],
                        confidence=confidence,
                        source_thread_id=thread_id,
                        source_run_id=run_id,
                        ttl_days=settings.memory_candidate_ttl_days,
                    )
                )
            except (TypeError, ValueError):
                continue
        return persisted

    registry = RunRegistry(
        store=store,
        broker=broker,
        agent_runner=agent_runner,
        stream_runner=stream_runner,
        session_dir=lambda thread_id: _session_dir(settings, thread_id),
        product_image_catalog_path=settings.product_image_catalog_path,
        memory_candidate_sink=(persist_memory_candidates if memory_service is not None else None),
        observability=observability,
        cancel_grace_seconds=settings.run_cancel_grace_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if database is None and not settings.legacy_sqlite_enabled:
            raise RuntimeError(
                "GLOBUY_DATABASE_URL is required; SQLite is available only when "
                "GLOBUY_LEGACY_SQLITE_ENABLED=true is explicitly set for legacy diagnostics"
            )
        await store.open()
        await store.recover_after_restart()
        try:
            yield
        finally:
            await registry.close()
            await broker.close()
            await observability.shutdown()
            await store.close()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="globuy AgentLoop API",
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.session_store = store
    app.state.database = database
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.event_broker = broker
    app.state.run_registry = registry
    app.state.observability = observability
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    router = APIRouter(prefix="/api/v1")

    async def request_user_id(
        request: Request,
        legacy_user_id: str | None = None,
        *,
        require_csrf: bool = False,
    ) -> str | None:
        if auth_service is None:
            return legacy_user_id
        principal = await auth_service.authenticate(request.cookies.get(settings.auth_cookie_name))
        if require_csrf:
            auth_service.verify_csrf(
                principal,
                request.cookies.get(settings.auth_csrf_cookie_name),
                request.headers.get("X-CSRF-Token"),
            )
        return principal.user_id

    @app.get("/", tags=["system"])
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "version": __version__, "docs": "/docs"}

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str | bool]:
        result: dict[str, str | bool] = {
            "status": "ok",
            "model_provider": settings.model_provider,
            "product_provider": settings.product_provider,
            "product_provider_configured": bool(
                settings.product_provider != "none" and settings.justone_token is not None
            ),
            "web_search_provider": settings.web_search_provider,
            "web_search_configured": bool(
                settings.web_search_provider != "none" and settings.tavily_api_key is not None
            ),
            "category_cache_enabled": bool(settings.redis_url),
            **observability.health(),
        }
        if database is not None:
            try:
                await database.ping()
                result["database"] = "ok"
            except Exception:
                result["status"] = "degraded"
                result["database"] = "unavailable"
        return result

    @router.get("/threads", tags=["sessions"])
    async def list_threads(
        request: Request,
        user_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        status: Literal["active", "archived"] = "archived",
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        page_size = limit or settings.archive_page_size
        if page_size > settings.archive_max_page_size:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                f"limit 不能超过 {settings.archive_max_page_size}",
            )
        owner_id = await request_user_id(request, user_id)
        if owner_id is None:
            raise ApiError(422, "VALIDATION_ERROR", "user_id 为必填项")
        return await store.list_threads(owner_id, status=status, cursor=cursor, limit=page_size)

    @router.post("/threads", status_code=201, tags=["sessions"])
    async def create_thread(payload: CreateThreadRequest, request: Request) -> dict[str, Any]:
        owner_id = await request_user_id(request, payload.user_id, require_csrf=True)
        if owner_id is None:
            raise ApiError(422, "VALIDATION_ERROR", "user_id 为必填项")
        return await registry.create_thread(
            user_id=owner_id,
            current_thread_id=payload.current_thread_id,
            client_request_id=payload.client_request_id,
        )

    @router.get("/threads/{thread_id}", tags=["sessions"])
    async def thread_detail(
        thread_id: str,
        request: Request,
        user_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ) -> dict[str, Any]:
        if not _THREAD_ID.fullmatch(thread_id):
            raise ApiError(422, "VALIDATION_ERROR", "thread_id 格式无效")
        owner_id = await request_user_id(request, user_id)
        if owner_id is None:
            raise ApiError(422, "VALIDATION_ERROR", "user_id 为必填项")
        detail = await store.thread_detail(thread_id, owner_id)
        for run in detail["runs"]:
            run["result"] = enrich_task_result(
                run.get("result"), settings.product_image_catalog_path
            )
            run["artifacts"] = [
                _artifact_public(item, thread_id, run["run_id"]) for item in run["artifacts"]
            ]
        return detail

    @router.post("/tasks", status_code=202, tags=["tasks"])
    async def create_task(payload: CreateTaskRequest, request: Request) -> dict[str, Any]:
        owner_id = await request_user_id(request, payload.user_id, require_csrf=True)
        if owner_id is None:
            raise ApiError(422, "VALIDATION_ERROR", "user_id 为必填项")
        return await registry.start_run(
            query=payload.query,
            thread_id=payload.thread_id,
            user_id=owner_id,
            client_request_id=payload.client_request_id,
        )

    @router.get("/threads/{thread_id}/runs/{run_id}", tags=["tasks"])
    async def run_status(thread_id: str, run_id: str, request: Request) -> dict[str, Any]:
        if not _THREAD_ID.fullmatch(thread_id) or not _THREAD_ID.fullmatch(run_id):
            raise ApiError(422, "VALIDATION_ERROR", "thread_id 或 run_id 格式无效")
        owner_id = await request_user_id(request)
        if owner_id is not None:
            await store.get_run(thread_id, run_id, user_id=owner_id)
        return await registry.run_status(thread_id, run_id)

    @router.post("/threads/{thread_id}/runs/{run_id}/cancel", tags=["tasks"])
    async def cancel_task(thread_id: str, run_id: str, request: Request) -> JSONResponse:
        if not _THREAD_ID.fullmatch(thread_id) or not _THREAD_ID.fullmatch(run_id):
            raise ApiError(422, "VALIDATION_ERROR", "thread_id 或 run_id 格式无效")
        owner_id = await request_user_id(request, require_csrf=True)
        if owner_id is not None:
            await store.get_run(thread_id, run_id, user_id=owner_id)
        response = await registry.cancel_run(thread_id, run_id)
        return JSONResponse(status_code=200 if response["terminal"] else 202, content=response)

    @router.get(
        "/threads/{thread_id}/runs/{run_id}/files",
        response_model=ArtifactListResponse,
        tags=["artifacts"],
    )
    async def list_run_files(thread_id: str, run_id: str, request: Request) -> dict[str, Any]:
        owner_id = await request_user_id(request)
        if owner_id is not None:
            await store.get_run(thread_id, run_id, user_id=owner_id)
        items = await store.list_artifacts(thread_id, run_id)
        return {"items": [_artifact_public(item, thread_id, run_id) for item in items]}

    @router.get(
        "/threads/{thread_id}/runs/{run_id}/files/{file_id}",
        tags=["artifacts"],
    )
    async def download_run_file(
        thread_id: str, run_id: str, file_id: str, request: Request
    ) -> DownloadResponse:
        owner_id = await request_user_id(request)
        if owner_id is not None:
            await store.get_run(thread_id, run_id, user_id=owner_id)
        item = await store.artifact(thread_id, run_id, file_id)
        relative = Path(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在")
        root = _session_dir(settings, thread_id).resolve()
        unresolved = root / relative
        if any(part.is_symlink() for part in [unresolved, *unresolved.parents] if part != root):
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在")
        try:
            target = unresolved.resolve(strict=True)
        except OSError as exc:
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在") from exc
        if root not in target.parents or not target.is_file():
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在")
        return DownloadResponse(
            target,
            filename=item["filename"],
            media_type=item["media_type"],
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @router.websocket("/ws/{thread_id}")
    async def subscribe_events(websocket: WebSocket, thread_id: str) -> None:
        await websocket.accept()
        if auth_service is not None:
            try:
                principal = await auth_service.authenticate(
                    websocket.cookies.get(settings.auth_cookie_name)
                )
            except ApiError:
                await websocket.close(code=4401, reason="请先登录")
                return
        else:
            principal = None
        run_id = websocket.query_params.get("run_id")
        try:
            after = int(websocket.query_params.get("after", "0"))
            if after < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=4400, reason="after 参数无效")
            return
        if (
            not _THREAD_ID.fullmatch(thread_id)
            or run_id is None
            or not _THREAD_ID.fullmatch(run_id)
        ):
            await websocket.close(code=4400, reason="thread_id/run_id 参数无效")
            return
        try:
            record = await store.get_run(
                thread_id,
                run_id,
                user_id=principal.user_id if principal is not None else None,
            )
        except ApiError:
            await websocket.close(code=4404, reason="thread/run 不存在")
            return
        if record["thread_status"] == "archived":
            await websocket.close(code=4404, reason="归档会话不建立事件流")
            return
        subscriber = await broker.subscribe(thread_id, run_id, after)

        async def writer() -> None:
            while True:
                try:
                    item = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=settings.ws_ping_interval
                    )
                except TimeoutError:
                    item = broker.control_event(
                        "heartbeat", thread_id, run_id, server_time=utc_now_for_ws()
                    )
                if item is None:
                    await websocket.close(code=4408, reason="订阅消费者过慢")
                    return
                await websocket.send_json(item.model_dump(mode="json"))

        async def reader() -> None:
            while True:
                packet = await websocket.receive()
                if packet["type"] == "websocket.disconnect":
                    return

        writer_task = asyncio.create_task(writer())
        reader_task = asyncio.create_task(reader())
        try:
            done, pending = await asyncio.wait(
                {writer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await broker.unsubscribe(subscriber)

    # Compatibility endpoints remain available for local diagnostics only.
    @router.post("/chat", response_model=ChatResponse, tags=["compatibility"])
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        await request_user_id(request)
        thread_id = _safe_thread_id(payload.thread_id)
        run_id = uuid4().hex
        with bind_context(thread_id, _session_dir(settings, thread_id), run_id=run_id):
            message, metadata = await agent_runner(payload.message, thread_id)
        return ChatResponse(
            thread_id=thread_id,
            run_id=run_id,
            message=message,
            metadata=metadata,
        )

    @router.post("/files", response_model=FileResponse, tags=["compatibility"])
    async def upload_file(
        request: Request,
        uploaded: Annotated[UploadFile, File()],
        x_thread_id: Annotated[str | None, Header()] = None,
    ) -> FileResponse:
        await request_user_id(request, require_csrf=True)
        thread_id = _safe_thread_id(x_thread_id)
        file_id = uuid4().hex
        original_name = Path(uploaded.filename or "upload.bin").name
        target_dir = upload_path(settings.uploaded_dir, thread_id)
        target = target_dir / f"{file_id}-{original_name}"
        size = 0
        with target.open("wb") as destination:
            while chunk := await uploaded.read(1024 * 1024):
                destination.write(chunk)
                size += len(chunk)
        await uploaded.close()
        return FileResponse(
            thread_id=thread_id,
            file_id=file_id,
            filename=original_name,
            size=size,
        )

    router.include_router(auth_router)
    if wishlist_service is not None and memory_service is not None:
        app.dependency_overrides[wishlist_service_dependency] = lambda: wishlist_service
        app.dependency_overrides[memory_service_dependency] = lambda: memory_service
        router.include_router(domain_router)
    app.include_router(router)
    return app


def utc_now_for_ws() -> str:
    from app.api.storage import utc_now

    return utc_now()


app = create_app()
