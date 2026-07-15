"""Application settings loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GLOBUY_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "globuy"
    app_env: str = "development"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    model_provider: Literal["mock", "openai-compatible"] = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None

    output_dir: Path = Path("output")
    uploaded_dir: Path = Path("uploaded")
    prompt_file: Path = Path("app/prompt/prompts.yml")
    compression_token_limit: int = 24_000
    compression_keep_recent: int = 8


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable-by-convention settings object."""

    return Settings()
