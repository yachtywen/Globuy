"""Versioned HTTP and WebSocket contracts for the browser task lifecycle."""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
RunStatus = Literal[
    "starting",
    "running",
    "cancelling",
    "succeeded",
    "cancelled",
    "failed",
    "interrupted",
]
ThreadStatus = Literal["active", "archived"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, pattern=ID_PATTERN)


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


class CreateThreadRequest(StrictModel):
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    current_thread_id: str | None = Field(default=None, pattern=ID_PATTERN)
    client_request_id: str = Field(min_length=1, max_length=128)


class CreateTaskRequest(StrictModel):
    query: str = Field(min_length=1, max_length=20_000)
    thread_id: str = Field(pattern=ID_PATTERN)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query trim 后不能为空")
        return normalized


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class ArtifactItem(BaseModel):
    file_id: str
    filename: str
    kind: str
    media_type: str
    size: int
    created_at: str
    download_url: str


class ArtifactListResponse(BaseModel):
    items: list[ArtifactItem] = Field(default_factory=list)


class RegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class AddWishlistItemRequest(StrictModel):
    offer_id: str = Field(min_length=1, max_length=128)
    source_thread_id: str | None = Field(default=None, pattern=ID_PATTERN)
    source_run_id: str | None = Field(default=None, pattern=ID_PATTERN)
    client_request_id: str = Field(min_length=1, max_length=128)


class UpdateWishlistItemRequest(StrictModel):
    status: Literal["active", "removed", "purchased"] | None = None
    target_price: Decimal | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=1000)


class CreateMemoryRequest(StrictModel):
    category: Literal["blacklist", "preference", "history"]
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4000)
    confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    source_thread_id: str | None = Field(default=None, pattern=ID_PATTERN)
    source_run_id: str | None = Field(default=None, pattern=ID_PATTERN)


class UpdateMemoryRequest(StrictModel):
    category: Literal["blacklist", "preference", "history"] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
