"""Map conversation thread IDs to active WebSocket connections."""

import asyncio

from fastapi import WebSocket

from app.api.monitor import AgentEvent


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, thread_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(thread_id, []).append(websocket)

    async def disconnect(self, thread_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(thread_id, [])
            if websocket in connections:
                connections.remove(websocket)
            if not connections:
                self._connections.pop(thread_id, None)

    async def send(self, websocket: WebSocket, agent_event: AgentEvent) -> None:
        await websocket.send_json(agent_event.model_dump(mode="json"))

    async def broadcast(self, thread_id: str, agent_event: AgentEvent) -> None:
        async with self._lock:
            connections = list(self._connections.get(thread_id, []))
        stale = []
        for websocket in connections:
            try:
                await self.send(websocket, agent_event)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(thread_id, websocket)

    async def connection_count(self, thread_id: str | None = None) -> int:
        async with self._lock:
            if thread_id is not None:
                return len(self._connections.get(thread_id, []))
            return sum(len(items) for items in self._connections.values())


connection_manager = ConnectionManager()
