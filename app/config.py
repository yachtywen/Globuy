"""Application settings loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    llm_temperature: float = Field(default=0.3, ge=0, le=2)

    output_dir: Path = Path("output")
    uploaded_dir: Path = Path("uploaded")
    prompt_file: Path = Path("app/prompt/prompts.yml")
    #触发压缩的token上限
    compression_token_limit: int = 12_000
    #压缩时只保留最近的三个工具调用及返回结果（每个工具调用的结果,每个ToolMessage会在返回前先经过compact_tool_content.py压缩）
    compression_keep_recent: int = 3
    tool_result_token_limit: int = Field(default=4_000, ge=256)
    loop_detection_window: int = Field(default=6, ge=2, le=50)
    loop_repeat_threshold: int = Field(default=4, ge=2, le=20)
    main_agent_recursion_limit: int = Field(default=48, ge=2, le=100)
    main_agent_timeout_seconds: float = Field(default=300.0, gt=0)
    fork_recursion_limit: int = Field(default=12, ge=2, le=50)
    fork_timeout_seconds: float = Field(default=90.0, gt=0)
    summary_timeout_seconds: float = Field(default=60.0, gt=0)
    ws_ping_interval: int = Field(default=20, ge=1)
    session_db_path: Path = Path("output/globuy-sessions.sqlite3")
    legacy_sqlite_enabled: bool = False
    archive_page_size: int = Field(default=20, ge=1, le=50)
    archive_max_page_size: int = Field(default=50, ge=1, le=100)
    run_cancel_grace_seconds: float = Field(default=5.0, gt=0)
    event_buffer_size: int = Field(default=2_000, ge=10)
    event_retention_seconds: int = Field(default=1_800, ge=1)
    ws_subscriber_queue_size: int = Field(default=256, ge=10)

    database_url: SecretStr | None = None
    database_echo: bool = False
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60)
    auth_cookie_name: str = "globuy_session"
    auth_csrf_cookie_name: str = "globuy_csrf"
    auth_session_days: int = Field(default=30, ge=1, le=365)
    auth_cookie_secure: bool = False
    auth_login_max_attempts: int = Field(default=8, ge=2, le=100)
    auth_login_window_seconds: int = Field(default=900, ge=60, le=86400)
    price_refresh_interval_hours: int = Field(default=24, ge=1, le=168)
    price_refresh_local_hour: int = Field(default=3, ge=0, le=23)

    # Runtime commerce discovery. Leaving the token unset deliberately disables
    # live retrieval instead of silently presenting the offline snapshot as live.
    realtime_product_provider: Literal["none", "justone"] = "none"
    justone_api_token: SecretStr | None = None
    justone_base_url: str = "https://api.justoneapi.com"
    realtime_search_timeout_seconds: float = Field(default=12.0, gt=0, le=60)
    realtime_search_cache_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    realtime_search_candidate_limit: int = Field(default=20, ge=1, le=50)

    @field_validator("database_url", mode="before")
    @classmethod
    def empty_database_url_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    web_search_provider: Literal["none", "tavily"] = "tavily"
    tavily_api_key: SecretStr | None = None
    tavily_base_url: str = "https://api.tavily.com"
    tavily_project_id: str | None = "globuy"
    tavily_search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic"
    tavily_timeout_seconds: float = Field(default=12.0, gt=0)
    tavily_max_results: int = Field(default=10, ge=1, le=20)
    web_search_content_chars: int = Field(default=1_200, ge=100, le=4_000)

    ann_backend: Literal["faiss"] = "faiss"
    ann_index_path: Path = Path("data/item_index.faiss")
    embedding_model_name: str = "BAAI/bge-m3"
    embedding_model_revision: str = "main"
    embedding_device: Literal["auto", "cpu", "cuda"] = "auto"
    embedding_dimensions: int = Field(default=1024, ge=1)
    embedding_batch_size: int = Field(default=16, ge=1)
    embedding_max_length: int = Field(default=256, ge=1)

    store_backend: Literal["opensearch"] = "opensearch"
    opensearch_url: str = "http://127.0.0.1:9200"
    opensearch_memory_index: str = "globuy-memory"
    opensearch_product_index: str = "globuy-products-v1"
    opensearch_product_alias: str = "globuy-products"
    opensearch_product_pipeline: str = "globuy-products-rrf"
    opensearch_timeout_seconds: float = Field(default=10.0, gt=0)
    product_dataset_path: Path = Path(
        "datasets/headphones_1000/structured/itemsearch_candidates.jsonl"
    )
    product_image_catalog_path: Path = Path(
        "datasets/justone_headphones/normalized/headphones.jsonl"
    )
    item_search_pool_floor: int = Field(default=60, ge=1)
    item_search_pool_max: int = Field(default=150, ge=1)
    fork_candidate_limit: int = Field(default=10, ge=1, le=50)
    fork_max_depth: int = Field(default=1, ge=1, le=1)

    category_dataset_path: Path = Path(
        "datasets/headphones_1000/structured/itemsearch_candidates.jsonl"
    )
    category_source_category: str = "耳机"
    category_aliases_path: Path = Path("app/category/category_aliases.yml")
    category_build_output_dir: Path = Path("output/category")
    opensearch_category_alias: str = "globuy-category"
    opensearch_category_index_prefix: str = "globuy-category-v1"
    opensearch_category_pipeline_exact: str = "globuy-category-exact-v1"
    opensearch_category_pipeline_balanced: str = "globuy-category-balanced-v1"
    opensearch_category_pipeline_semantic: str = "globuy-category-semantic-v1"
    category_coarse_k: int = Field(default=30, ge=1, le=100)
    category_quick_k: int = Field(default=8, ge=1, le=30)
    category_deep_k: int = Field(default=15, ge=1, le=50)
    category_min_confidence: float = Field(default=0.5, ge=0, le=1)
    reranker_endpoint: str | None = None
    reranker_timeout_seconds: float = Field(default=3.0, gt=0)
    category_reranker_required: bool = False
    category_rerank_bypass_score: float | None = Field(default=None, ge=0)
    redis_url: str | None = "redis://127.0.0.1:6379/0"
    category_cache_ttl_seconds: int = Field(default=3600, ge=1)
    category_cache_timeout_seconds: float = Field(default=0.25, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable-by-convention settings object."""

    return Settings()
