"""FastAPI application and transport endpoints."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, File, Header, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agent.main_agent import run_agent
from app.api.connection import ConnectionManager, connection_manager
from app.api.context import bind_context
from app.api.monitor import EventType, event
from app.api.schemas import ChatRequest, ChatResponse, FileResponse
from app.config import Settings, get_settings
from app.utils.path_utils import session_path

AgentRunner = Callable[[str, str], Awaitable[tuple[str, dict[str, Any]]]]


def _safe_thread_id(value: str | None) -> str:
    if value and all(char.isalnum() or char in "-_" for char in value):
        return value[:128]
    return uuid4().hex


def _session_dir(settings: Settings, thread_id: str) -> Path:
    return session_path(settings.output_dir, thread_id)


def create_app(
    settings: Settings | None = None,
    agent_runner: AgentRunner = run_agent,
    connections: ConnectionManager = connection_manager,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="globuy AgentLoop API",
        debug=settings.debug,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    router = APIRouter(prefix="/api/v1")

    @app.get("/", tags=["system"])
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "version": __version__, "docs": "/docs"}

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "model_provider": settings.model_provider}

    @router.post("/chat", response_model=ChatResponse, tags=["agent"])
    async def chat(payload: ChatRequest) -> ChatResponse:
        thread_id = _safe_thread_id(payload.thread_id)
        run_id = uuid4().hex
        with bind_context(thread_id, _session_dir(settings, thread_id)):
            message, metadata = await agent_runner(payload.message, thread_id)
        return ChatResponse(
            thread_id=thread_id,
            run_id=run_id,
            message=message,
            metadata=metadata,
        )

    @router.post("/files", response_model=FileResponse, tags=["files"])
    async def upload_file(
        uploaded: Annotated[UploadFile, File()],
        x_thread_id: Annotated[str | None, Header()] = None,
    ) -> FileResponse:
        thread_id = _safe_thread_id(x_thread_id)
        file_id = uuid4().hex
        original_name = Path(uploaded.filename or "upload.bin").name
        target_dir = session_path(settings.uploaded_dir, thread_id)
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

    @router.websocket("/ws/{thread_id}")
    async def websocket_agent(websocket: WebSocket, thread_id: str) -> None:
        thread_id = _safe_thread_id(thread_id)
        await connections.connect(thread_id, websocket)
        try:
            while True:
                packet = await websocket.receive()
                if packet["type"] == "websocket.disconnect":
                    return
                if packet.get("text") is None:
                    continue
                raw = packet["text"]
                try:
                    decoded = json.loads(raw)
                    content = decoded.get("content", "") if isinstance(decoded, dict) else ""
                except ValueError:
                    content = raw

                run_id = uuid4().hex
                if not isinstance(content, str) or not content.strip():
                    await websocket.send_json(
                        event(
                            EventType.RUN_ERROR,
                            thread_id,
                            run_id,
                            message="消息 content 不能为空",
                        ).model_dump(mode="json")
                    )
                    continue

                await websocket.send_json(
                    event(EventType.RUN_STARTED, thread_id, run_id).model_dump(mode="json")
                )
                message_id = uuid4().hex
                await websocket.send_json(
                    event(
                        EventType.TEXT_MESSAGE_START,
                        thread_id,
                        run_id,
                        message_id=message_id,
                        role="assistant",
                    ).model_dump(mode="json")
                )
                try:
                    with bind_context(thread_id, _session_dir(settings, thread_id)):
                        answer, metadata = await agent_runner(content.strip(), thread_id)
                    await websocket.send_json(
                        event(
                            EventType.TEXT_MESSAGE_CONTENT,
                            thread_id,
                            run_id,
                            message_id=message_id,
                            delta=answer,
                        ).model_dump(mode="json")
                    )
                    await websocket.send_json(
                        event(
                            EventType.TEXT_MESSAGE_END,
                            thread_id,
                            run_id,
                            message_id=message_id,
                        ).model_dump(mode="json")
                    )
                    await websocket.send_json(
                        event(
                            EventType.RUN_FINISHED,
                            thread_id,
                            run_id,
                            metadata=metadata,
                        ).model_dump(mode="json")
                    )
                except Exception as exc:  # Transport boundary: turn exceptions into events.
                    await websocket.send_json(
                        event(
                            EventType.RUN_ERROR,
                            thread_id,
                            run_id,
                            message=str(exc),
                        ).model_dump(mode="json")
                    )
        except WebSocketDisconnect:
            return
        finally:
            await connections.disconnect(thread_id, websocket)

    app.include_router(router)
    return app


app = create_app()
