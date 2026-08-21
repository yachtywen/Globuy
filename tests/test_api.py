import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.errors import ApiError
from app.api.monitor import EventType
from app.api.server import create_app
from app.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    async def fake_agent(content: str, thread_id: str):
        if content.startswith("slow"):
            await asyncio.sleep(30)
        return f"[test] {content}", {
            "message_id": f"message-{thread_id}",
            "memory_status": "not_configured",
        }

    settings = Settings(
        database_url=None,
        output_dir=tmp_path / "output",
        uploaded_dir=tmp_path / "uploaded",
        session_db_path=tmp_path / "sessions.sqlite3",
        legacy_sqlite_enabled=True,
        model_provider="mock",
        ws_ping_interval=1,
    )
    with TestClient(create_app(settings, agent_runner=fake_agent)) as test_client:
        yield test_client


def create_thread(client: TestClient, user_id: str = "anonymous-test") -> dict:
    response = client.post(
        "/api/v1/threads",
        json={
            "user_id": user_id,
            "current_thread_id": None,
            "client_request_id": uuid4().hex,
        },
    )
    assert response.status_code == 201
    return response.json()


def start_task(
    client: TestClient, thread_id: str, query: str, user_id: str = "anonymous-test"
) -> dict:
    response = client.post(
        "/api/v1/tasks",
        json={
            "query": query,
            "thread_id": thread_id,
            "user_id": user_id,
            "client_request_id": uuid4().hex,
        },
    )
    assert response.status_code == 202
    return response.json()


def wait_for_status(
    client: TestClient,
    thread_id: str,
    run_id: str,
    expected: set[str],
    timeout: float = 2,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/threads/{thread_id}/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in expected:
            return body
        time.sleep(0.01)
    raise AssertionError(f"run 未进入目标状态: {expected}")


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["model_provider"] == "mock"
    assert isinstance(payload["product_provider_configured"], bool)
    assert isinstance(payload["web_search_configured"], bool)
    assert isinstance(payload["category_cache_enabled"], bool)
    assert payload["observability_status"] == "disabled"
    assert payload["observability_configured"] is False
    assert all("key" not in key and "token" not in key for key in payload)


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


def test_thread_task_status_and_replayable_websocket(client: TestClient) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "测试 WebSocket")
    assert len(task["trace_id"]) == 32
    status = wait_for_status(
        client, thread["thread_id"], task["run_id"], {"succeeded"}
    )
    assert status["result"]["source_kind"] == "offline_snapshot"
    assert "测试 WebSocket" in status["result"]["final_text"]

    received = []
    url = f"/api/v1/ws/{thread['thread_id']}?run_id={task['run_id']}&after=0"
    with client.websocket_connect(url) as websocket:
        while len(received) < 20:
            item = websocket.receive_json()
            received.append(item)
            if item["event"] == "CUSTOM" and item["data"].get("name") == "stream_ready":
                break

    business = [item for item in received if item["sequence"] is not None]
    assert [item["event"] for item in business] == [
        "RUN_STARTED",
        "CUSTOM",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "CUSTOM",
        "RUN_FINISHED",
    ]
    assert all(item["type"] == "monitor_event" for item in received)
    assert [item["sequence"] for item in business] == list(range(1, 8))
    initializing = business[1]
    assert initializing["data"] == {"name": "conversation_initializing", "phase": "preparing"}
    assert initializing["message"] == "收到你的消息了~正在初始化本次对话"


def test_task_post_returns_before_slow_agent_and_cancel_is_run_aware(
    client: TestClient,
) -> None:
    thread = create_thread(client)
    started = time.monotonic()
    task = start_task(client, thread["thread_id"], "slow request")
    assert time.monotonic() - started < 1
    wait_for_status(client, thread["thread_id"], task["run_id"], {"running"})

    cancelled = client.post(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/cancel"
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelling"
    terminal = wait_for_status(
        client, thread["thread_id"], task["run_id"], {"cancelled"}
    )
    assert terminal["terminal_event"]["event"] == "TASK_CANCELLED"

    second_cancel = client.post(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/cancel"
    )
    assert second_cancel.status_code == 200
    assert second_cancel.json()["status"] == "cancelled"


def test_immediate_cancel_cannot_leave_starting_run_or_hanging_handle(
    client: TestClient,
) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "slow immediate")
    cancelled = client.post(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/cancel"
    )
    assert cancelled.status_code in {200, 202}
    terminal = wait_for_status(
        client, thread["thread_id"], task["run_id"], {"cancelled"}
    )
    assert terminal["status"] == "cancelled"
    assert thread["thread_id"] not in client.app.state.run_registry.active_tasks


def test_new_thread_archives_completed_session_and_enforces_read_only(
    client: TestClient,
) -> None:
    old = create_thread(client)
    task = start_task(client, old["thread_id"], "需要归档的消息")
    wait_for_status(client, old["thread_id"], task["run_id"], {"succeeded"})

    request_id = uuid4().hex
    response = client.post(
        "/api/v1/threads",
        json={
            "user_id": "anonymous-test",
            "current_thread_id": old["thread_id"],
            "client_request_id": request_id,
        },
    )
    assert response.status_code == 201
    new = response.json()
    assert new["archived_thread_id"] == old["thread_id"]

    detail = client.get(
        f"/api/v1/threads/{old['thread_id']}?user_id=anonymous-test"
    )
    assert detail.status_code == 200
    assert detail.json()["read_only"] is True
    assert [message["role"] for message in detail.json()["messages"]] == [
        "user",
        "assistant",
    ]

    rejected = client.post(
        "/api/v1/tasks",
        json={
            "query": "不能继续",
            "thread_id": old["thread_id"],
            "user_id": "anonymous-test",
            "client_request_id": uuid4().hex,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "THREAD_ARCHIVED"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"/api/v1/ws/{old['thread_id']}?run_id={task['run_id']}&after=0"
        ) as websocket:
            websocket.receive_json()
    assert exc_info.value.code == 4404


def test_thread_and_run_creation_are_idempotent(client: TestClient) -> None:
    thread_request = {
        "user_id": "idem-user",
        "current_thread_id": None,
        "client_request_id": "same-thread-request",
    }
    first = client.post("/api/v1/threads", json=thread_request)
    second = client.post("/api/v1/threads", json=thread_request)
    assert first.json() == second.json()

    task_request = {
        "query": "只执行一次",
        "thread_id": first.json()["thread_id"],
        "user_id": "idem-user",
        "client_request_id": "same-run-request",
    }
    run_one = client.post("/api/v1/tasks", json=task_request)
    run_two = client.post("/api/v1/tasks", json=task_request)
    assert run_one.status_code == run_two.status_code == 202
    assert run_one.json()["run_id"] == run_two.json()["run_id"]


def test_thread_lists_and_owner_hiding(client: TestClient) -> None:
    thread = create_thread(client)
    active = client.get("/api/v1/threads?user_id=anonymous-test&status=active")
    assert [item["thread_id"] for item in active.json()["items"]] == [
        thread["thread_id"]
    ]
    hidden = client.get(f"/api/v1/threads/{thread['thread_id']}?user_id=another-user")
    assert hidden.status_code == 404


def test_upload_sanitizes_filename(client: TestClient) -> None:
    response = client.post(
        "/api/v1/files",
        headers={"X-Thread-ID": f"test-{uuid4().hex}"},
        files={"uploaded": ("../note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["filename"] == "note.txt"
    assert response.json()["size"] == 5


def test_formal_validation_error_uses_stable_envelope(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={
            "query": "   ",
            "thread_id": "bad/id",
            "user_id": "u",
            "client_request_id": "r",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_new_run_waits_for_old_run_and_old_finally_keeps_new_handle(
    client: TestClient,
) -> None:
    thread = create_thread(client)
    first = start_task(client, thread["thread_id"], "slow first")
    wait_for_status(client, thread["thread_id"], first["run_id"], {"running"})

    second = start_task(client, thread["thread_id"], "slow second")
    assert second["replaced_run_id"] == first["run_id"]
    wait_for_status(client, thread["thread_id"], first["run_id"], {"cancelled"})
    wait_for_status(client, thread["thread_id"], second["run_id"], {"running"})
    active = client.app.state.run_registry.active_tasks[thread["thread_id"]]
    assert active.run_id == second["run_id"]

    client.post(
        f"/api/v1/threads/{thread['thread_id']}/runs/{second['run_id']}/cancel"
    )
    wait_for_status(client, thread["thread_id"], second["run_id"], {"cancelled"})


def test_archiving_running_thread_cancels_before_transaction(client: TestClient) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "slow archive")
    wait_for_status(client, thread["thread_id"], task["run_id"], {"running"})

    response = client.post(
        "/api/v1/threads",
        json={
            "user_id": "anonymous-test",
            "current_thread_id": thread["thread_id"],
            "client_request_id": uuid4().hex,
        },
    )
    assert response.status_code == 201
    terminal_response = client.get(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}"
    )
    assert terminal_response.status_code == 200, terminal_response.text
    terminal = terminal_response.json()
    assert terminal["status"] == "cancelled"
    detail = client.get(
        f"/api/v1/threads/{thread['thread_id']}?user_id=anonymous-test"
    ).json()
    assert detail["status"] == "archived"


def test_replay_gap_and_multiple_subscribers_are_isolated(client: TestClient) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "事件重放")
    status = wait_for_status(
        client, thread["thread_id"], task["run_id"], {"succeeded"}
    )
    broker = client.app.state.event_broker
    stream = client.portal.call(
        broker.ensure_stream, thread["thread_id"], task["run_id"]
    )
    stream.max_events = 3
    for index in range(5):
        client.portal.call(
            partial(
                broker.publish,
                EventType.CUSTOM,
                thread["thread_id"],
                task["run_id"],
                data={"name": "test_event", "index": index},
            )
        )

    url = f"/api/v1/ws/{thread['thread_id']}?run_id={task['run_id']}&after=1"
    with client.websocket_connect(url) as websocket:
        gap = websocket.receive_json()
        assert gap["event"] == "CUSTOM"
        assert gap["data"]["name"] == "replay_gap"

    after = status["last_sequence"] + 5
    clean_url = (
        f"/api/v1/ws/{thread['thread_id']}?run_id={task['run_id']}&after={after}"
    )
    with client.websocket_connect(clean_url) as first_socket:
        assert first_socket.receive_json()["data"]["name"] == "stream_ready"
        with client.websocket_connect(clean_url) as second_socket:
            assert second_socket.receive_json()["data"]["name"] == "stream_ready"
        client.portal.call(
            partial(
                broker.publish,
                EventType.CUSTOM,
                thread["thread_id"],
                task["run_id"],
                data={"name": "still_connected"},
            )
        )
        assert first_socket.receive_json()["data"]["name"] == "still_connected"


def test_running_subscription_receives_application_heartbeat(client: TestClient) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "slow heartbeat")
    status = wait_for_status(
        client, thread["thread_id"], task["run_id"], {"running"}
    )
    url = (
        f"/api/v1/ws/{thread['thread_id']}?run_id={task['run_id']}"
        f"&after={status['last_sequence']}"
    )
    with client.websocket_connect(url) as websocket:
        ready = websocket.receive_json()
        heartbeat = websocket.receive_json()
    assert ready["data"]["name"] == "stream_ready"
    assert heartbeat["data"]["name"] == "heartbeat"
    assert heartbeat["sequence"] is None
    client.post(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/cancel"
    )
    wait_for_status(client, thread["thread_id"], task["run_id"], {"cancelled"})


def test_concurrent_new_thread_requests_leave_exactly_one_active_thread(
    client: TestClient,
) -> None:
    old = create_thread(client, user_id="concurrent-user")
    task = start_task(
        client, old["thread_id"], "并发归档", user_id="concurrent-user"
    )
    wait_for_status(client, old["thread_id"], task["run_id"], {"succeeded"})

    def replace(index: int):
        return client.post(
            "/api/v1/threads",
            json={
                "user_id": "concurrent-user",
                "current_thread_id": old["thread_id"],
                "client_request_id": f"concurrent-{index}",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(replace, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    active = client.get(
        "/api/v1/threads?user_id=concurrent-user&status=active"
    ).json()["items"]
    assert len(active) == 1


def test_artifact_manifest_download_and_path_traversal_rejection(
    client: TestClient, tmp_path: Path
) -> None:
    thread = create_thread(client)
    task = start_task(client, thread["thread_id"], "生成产物")
    wait_for_status(client, thread["thread_id"], task["run_id"], {"succeeded"})
    session_dir = tmp_path / "output" / "sessions" / thread["thread_id"]
    artifact_dir = session_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    target = artifact_dir / "summary.md"
    target.write_text("# summary", encoding="utf-8")
    store = client.app.state.session_store
    item = client.portal.call(
        partial(
            store.register_artifact,
            thread_id=thread["thread_id"],
            run_id=task["run_id"],
            filename="shopping-summary.md",
            kind="shopping_summary",
            media_type="text/markdown",
            size=target.stat().st_size,
            relative_path="artifacts/summary.md",
        )
    )
    listed = client.get(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/files"
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["file_id"] == item["file_id"]
    downloaded = client.get(listed.json()["items"][0]["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"# summary"
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    with pytest.raises(ApiError) as exc_info:
        client.portal.call(
            partial(
                store.register_artifact,
                thread_id=thread["thread_id"],
                run_id=task["run_id"],
                filename="secret.txt",
                kind="test",
                media_type="text/plain",
                size=1,
                relative_path="../secret.txt",
            )
        )
    assert exc_info.value.code == "INVALID_ARTIFACT_PATH"
    rejected = client.get(
        f"/api/v1/threads/{thread['thread_id']}/runs/{task['run_id']}/files/missing"
    )
    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "FILE_NOT_FOUND"
