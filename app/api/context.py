"""Request and conversation context shared without passing parameters everywhere."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.utils.thread_ctx import thread_scope


@contextmanager
def bind_context(
    thread_id: str,
    session_dir: Path,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
) -> Iterator[None]:
    """Backward-compatible API wrapper around the shared task context."""

    with thread_scope(thread_id, session_dir, run_id=run_id, user_id=user_id):
        yield
