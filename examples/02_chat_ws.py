"""Run after starting the API; `websockets` comes with uvicorn[standard]."""

import asyncio
import json

from websockets.asyncio.client import connect


async def main() -> None:
    async with connect("ws://127.0.0.1:8000/api/v1/ws/example-thread") as websocket:
        await websocket.send(json.dumps({"content": "比较两款降噪耳机时应该看什么？"}))
        while True:
            event = json.loads(await websocket.recv())
            print(event)
            if event["type"] in {"RUN_FINISHED", "RUN_ERROR"}:
                break


asyncio.run(main())
