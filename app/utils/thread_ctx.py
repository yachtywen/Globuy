"""ContextVar accessors shared by API, tools and trace writers."""

from contextvars import ContextVar, Token
from pathlib import Path

thread_id_var: ContextVar[str | None] = ContextVar("thread_id", default=None)
session_dir_var: ContextVar[Path | None] = ContextVar("session_dir", default=None)


def set_thread_context(thread_id: str, session_dir: Path) -> tuple[Token, Token]:
    return thread_id_var.set(thread_id), session_dir_var.set(session_dir)


def reset_thread_context(tokens: tuple[Token, Token]) -> None:
    thread_id_var.reset(tokens[0])
    session_dir_var.reset(tokens[1])


def current_thread_id() -> str | None:
    return thread_id_var.get()


def current_session_dir() -> Path | None:
    return session_dir_var.get()
