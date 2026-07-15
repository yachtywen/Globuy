"""HTTP request and response contracts."""

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatResponse(BaseModel):
    thread_id: str
    run_id: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileResponse(BaseModel):
    thread_id: str
    file_id: str
    filename: str
    size: int
