"""Task-local identity shared by API, tools and trace writers."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

thread_id_var: ContextVar[str | None] = ContextVar("thread_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
session_dir_var: ContextVar[Path | None] = ContextVar("session_dir", default=None)
fork_depth_var: ContextVar[int] = ContextVar("fork_depth", default=0)
parent_thread_id_var: ContextVar[str | None] = ContextVar("parent_thread_id", default=None)
fork_target_platform_var: ContextVar[str | None] = ContextVar("fork_target_platform", default=None)


ContextTokens = tuple[Token, Token, Token, Token, Token, Token, Token]


def set_thread_context(
    thread_id: str,
    session_dir: Path,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
    fork_depth: int = 0,
    parent_thread_id: str | None = None,
    target_platform: str | None = None,
) -> ContextTokens:
    return (
        thread_id_var.set(thread_id),
        run_id_var.set(run_id),
        user_id_var.set(user_id),
        session_dir_var.set(session_dir),
        fork_depth_var.set(fork_depth),
        parent_thread_id_var.set(parent_thread_id),
        fork_target_platform_var.set(target_platform),
    )


def reset_thread_context(tokens: ContextTokens) -> None:
    thread_id_var.reset(tokens[0])
    run_id_var.reset(tokens[1])
    user_id_var.reset(tokens[2])
    session_dir_var.reset(tokens[3])
    fork_depth_var.reset(tokens[4])
    parent_thread_id_var.reset(tokens[5])
    fork_target_platform_var.reset(tokens[6])


@contextmanager
def thread_scope(
    thread_id: str,
    session_dir: Path,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
) -> Iterator[None]:
    """Bind task identity and always restore the enclosing async context."""

    tokens = set_thread_context(thread_id, session_dir, run_id=run_id, user_id=user_id)
    try:
        yield
    finally:
        reset_thread_context(tokens)


@contextmanager
def fork_scope(
    sub_thread_id: str,
    *,
    run_id: str | None = None,
    target_platform: str | None = None,
) -> Iterator[None]:
    """Give a fork its own checkpoint identity while inheriting the session/user."""

    session_dir = current_session_dir()
    if session_dir is None:
        raise RuntimeError("fork_scope 必须在 thread_scope 内使用")
    tokens = set_thread_context(
        sub_thread_id,
        session_dir,
        run_id=run_id or current_run_id(),
        user_id=current_user_id(),
        fork_depth=current_fork_depth() + 1,
        parent_thread_id=current_thread_id(),
        target_platform=target_platform,
    )
    try:
        yield
    finally:
        reset_thread_context(tokens)


def current_thread_id() -> str | None:
    return thread_id_var.get()


def current_run_id() -> str | None:
    return run_id_var.get()


def current_user_id() -> str | None:
    return user_id_var.get()


def current_session_dir() -> Path | None:
    return session_dir_var.get()


def current_fork_depth() -> int:
    return fork_depth_var.get()


def current_parent_thread_id() -> str | None:
    return parent_thread_id_var.get()


def current_fork_target_platform() -> str | None:
    return fork_target_platform_var.get()
