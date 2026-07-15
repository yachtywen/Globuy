"""Request and conversation context shared without passing parameters everywhere."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.utils.thread_ctx import reset_thread_context, set_thread_context


@contextmanager
def bind_context(thread_id: str, session_dir: Path) -> Iterator[None]:
    tokens = set_thread_context(thread_id, session_dir)
    try:
        yield
    finally:
        reset_thread_context(tokens)
