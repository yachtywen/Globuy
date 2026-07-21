from pathlib import Path
from uuid import uuid4

import pytest

from app.api.errors import ApiError
from app.api.storage import SessionStore, utc_now


@pytest.mark.asyncio
async def test_restart_marks_nonterminal_run_interrupted_and_archives_thread(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    await store.open()
    thread = await store.replace_thread(
        user_id="restart-user",
        current_thread_id=None,
        client_request_id="thread-request",
        new_thread_id="restart-thread",
    )
    run_id = "restart-run"
    created_at = utc_now()
    await store.create_run(
        user_id="restart-user",
        thread_id=thread["thread_id"],
        run_id=run_id,
        query="未完成任务",
        client_request_id="run-request",
        response={
            "status": "starting",
            "thread_id": thread["thread_id"],
            "run_id": run_id,
            "replaced_run_id": None,
            "created_at": created_at,
            "ws_url": "unused",
            "status_url": "unused",
        },
    )
    await store.set_run_status(thread["thread_id"], run_id, "running")
    await store.close()

    recovered = SessionStore(path)
    await recovered.open()
    counts = await recovered.recover_after_restart()
    run = await recovered.get_run(thread["thread_id"], run_id)
    detail = await recovered.thread_detail(thread["thread_id"], "restart-user")
    await recovered.close()

    assert counts["interrupted_runs"] == 1
    assert counts["archived_threads"] == 1
    assert run["status"] == "interrupted"
    assert run["error_code"] == "SERVER_RESTART"
    assert detail["status"] == "archived"
    assert detail["archive_reason"] == "server_restart"


@pytest.mark.asyncio
async def test_empty_active_thread_is_replaced_without_entering_archive(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    first = await store.replace_thread(
        user_id="empty-user",
        current_thread_id=None,
        client_request_id="first",
        new_thread_id="empty-first",
    )
    second = await store.replace_thread(
        user_id="empty-user",
        current_thread_id=first["thread_id"],
        client_request_id="second",
        new_thread_id="empty-second",
    )
    archived = await store.list_threads(
        "empty-user", status="archived", cursor=None, limit=20
    )
    active = await store.list_threads(
        "empty-user", status="active", cursor=None, limit=20
    )
    await store.close()

    assert second["archived_thread_id"] is None
    assert archived["items"] == []
    assert [item["thread_id"] for item in active["items"]] == ["empty-second"]


@pytest.mark.asyncio
async def test_archive_cursor_pagination_has_no_duplicates(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    current: str | None = None
    for index in range(4):
        thread_id = f"thread-{index}"
        response = await store.replace_thread(
            user_id="page-user",
            current_thread_id=current,
            client_request_id=f"thread-{index}-{uuid4().hex}",
            new_thread_id=thread_id,
        )
        current = response["thread_id"]
        run_id = f"run-{index}"
        await store.create_run(
            user_id="page-user",
            thread_id=current,
            run_id=run_id,
            query=f"问题 {index}",
            client_request_id=f"run-request-{index}",
            response={
                "status": "starting",
                "thread_id": current,
                "run_id": run_id,
                "replaced_run_id": None,
                "created_at": utc_now(),
                "ws_url": "unused",
                "status_url": "unused",
            },
        )
        await store.finish_run(
            thread_id=current,
            run_id=run_id,
            status="succeeded",
            final_text=f"回答 {index}",
            result={"status": "complete"},
            message_id=f"message-{index}",
            is_partial=False,
        )
    await store.replace_thread(
        user_id="page-user",
        current_thread_id=current,
        client_request_id="final-thread",
        new_thread_id="thread-final",
    )

    first_page = await store.list_threads(
        "page-user", status="archived", cursor=None, limit=2
    )
    second_page = await store.list_threads(
        "page-user",
        status="archived",
        cursor=first_page["next_cursor"],
        limit=2,
    )
    await store.close()
    ids = [item["thread_id"] for item in first_page["items"] + second_page["items"]]
    assert len(ids) == len(set(ids)) == 4


@pytest.mark.asyncio
async def test_store_refuses_to_archive_or_overlap_a_nonterminal_run(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    thread = await store.replace_thread(
        user_id="guard-user",
        current_thread_id=None,
        client_request_id="thread",
        new_thread_id="guard-thread",
    )
    response = {
        "status": "starting",
        "thread_id": thread["thread_id"],
        "run_id": "guard-run",
        "replaced_run_id": None,
        "created_at": utc_now(),
        "ws_url": "unused",
        "status_url": "unused",
    }
    await store.create_run(
        user_id="guard-user",
        thread_id=thread["thread_id"],
        run_id="guard-run",
        query="仍在运行",
        client_request_id="run",
        response=response,
    )
    with pytest.raises(ApiError) as archive_error:
        await store.replace_thread(
            user_id="guard-user",
            current_thread_id=thread["thread_id"],
            client_request_id="replacement",
            new_thread_id="guard-new",
        )
    assert archive_error.value.code == "RUN_CANCELLATION_FAILED"

    with pytest.raises(ApiError) as overlap_error:
        await store.create_run(
            user_id="guard-user",
            thread_id=thread["thread_id"],
            run_id="overlap-run",
            query="不得重叠",
            client_request_id="overlap",
            response={**response, "run_id": "overlap-run"},
        )
    assert overlap_error.value.code == "RUN_REPLACEMENT_TIMEOUT"
    await store.close()
