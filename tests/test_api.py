from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    async def fake_agent(content: str, thread_id: str):
        return f"[test] {content}", {"message_id": f"message-{thread_id}"}

    settings = Settings(
        output_dir=tmp_path / "output",
        uploaded_dir=tmp_path / "uploaded",
        model_provider="mock",
    )
    return TestClient(create_app(settings, agent_runner=fake_agent))


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model_provider": "mock"}


def test_http_chat_uses_mock_graph(client: TestClient) -> None:
    thread_id = f"test-{uuid4().hex}"
    response = client.post(
        "/api/v1/chat",
        json={"message": "你好", "thread_id": thread_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert "你好" in body["message"]
    assert body["run_id"]


def test_websocket_emits_agent_events(client: TestClient) -> None:
    thread_id = f"test-{uuid4().hex}"
    with client.websocket_connect(f"/api/v1/ws/{thread_id}") as websocket:
        websocket.send_json({"type": "user_message", "content": "测试 WebSocket"})
        events = [websocket.receive_json() for _ in range(5)]

    assert [item["type"] for item in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert "测试 WebSocket" in events[2]["data"]["delta"]


def test_upload_sanitizes_filename(client: TestClient) -> None:
    response = client.post(
        "/api/v1/files",
        headers={"X-Thread-ID": f"test-{uuid4().hex}"},
        files={"uploaded": ("../note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["filename"] == "note.txt"
    assert response.json()["size"] == 5
