"""Application settings loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
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
    llm_model: str = "deepseek-v4-flash"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = "https://api.deepseek.com/v1"
    llm_temperature: float = Field(default=0.3, ge=0, le=2)
    llm_context_window_tokens: int = Field(default=1_000_000, ge=8_192)
    llm_max_output_tokens: int = Field(default=32_768, ge=256)
    # Per-HTTP-request ceiling for one model call. A slow provider response must
    # fail fast and free the main-run budget instead of stalling for minutes.
    llm_request_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # Bounded retry count so timeout/transient failures cannot triple the stall.
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    # Optional client-side throttle for providers with a low organization RPM.
    llm_requests_per_minute: float | None = Field(default=None, gt=0)

    # Independent P1/P2 evaluation model. These settings intentionally do not
    # fall back to the main Agent model or its credentials.
    eval_judge_model: str | None = None
    eval_judge_base_url: str | None = None
    eval_judge_api_key: SecretStr | None = None
    eval_judge_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)

    output_dir: Path = Path("output")
    uploaded_dir: Path = Path("uploaded")
    prompt_file: Path = Path("app/prompt/prompts.yml")
    # Optional legacy hard override. When unset, the breakpoint is derived from
    # the configured model context window and output reserve.
    compression_token_limit: int | None = Field(default=None, ge=1_024)
    compression_trigger_ratio: float = Field(default=0.75, gt=0, lt=1)
    compression_target_ratio: float = Field(default=0.50, gt=0, lt=1)
    compression_safety_margin_tokens: int = Field(default=16_384, ge=1_024)
    # 压缩时保留最近三个工具调用组；ToolMessage 返回前先经过结果压缩。
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

    observability_provider: Literal["none", "langfuse"] = "none"
    langfuse_base_url: str = "https://jp.cloud.langfuse.com"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_capture_mode: Literal["none", "summary", "full"] = "summary"
    langfuse_sample_rate: float = Field(default=1.0, ge=0, le=1)
    observability_hash_salt: SecretStr | None = None
    observability_flush_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

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

    product_provider: Literal["none", "justone"] = "none"
    justone_base_url: str = "https://api.justoneapi.com"
    justone_token: SecretStr | None = None
    product_provider_timeout_seconds: float = Field(default=12.0, gt=0)
    catalog_minimum_total: int = Field(default=60, ge=1)
    catalog_target_total: int = Field(default=100, ge=1)
    catalog_hard_cap_total: int = Field(default=120, ge=1)
    catalog_minimum_per_platform: int = Field(default=15, ge=1)
    catalog_max_success_calls_per_run: int = Field(default=12, ge=1)
    catalog_max_attempts_per_run: int = Field(default=20, ge=1)
    catalog_soft_deadline_seconds: float = Field(default=20.0, gt=0)
    catalog_hard_deadline_seconds: float = Field(default=60.0, gt=0)
    catalog_freshness_seconds: int = Field(default=86_400, ge=60)
    catalog_scope_ttl_seconds: int = Field(default=2_592_000, ge=3600)
    catalog_lease_seconds: int = Field(default=120, ge=10)
    provider_max_concurrency: int = Field(default=3, ge=1, le=20)
    provider_per_platform_concurrency: int = Field(default=1, ge=1, le=1)

    @field_validator(
        "database_url",
        "compression_token_limit",
        "langfuse_public_key",
        "langfuse_secret_key",
        "observability_hash_salt",
        "eval_judge_model",
        "eval_judge_base_url",
        "eval_judge_api_key",
        mode="before",
    )
    @classmethod
    def empty_database_url_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_context_budget(self) -> Self:
        if self.compression_target_ratio >= self.compression_trigger_ratio:
            raise ValueError("compression target ratio must be below trigger ratio")
        reserved = self.llm_max_output_tokens + self.compression_safety_margin_tokens
        if reserved >= self.llm_context_window_tokens:
            raise ValueError("LLM output and safety reserves must fit inside the context window")
        return self

    @property
    def compression_trigger_tokens(self) -> int:
        if self.compression_token_limit is not None:
            return self.compression_token_limit
        ratio_limit = int(self.llm_context_window_tokens * self.compression_trigger_ratio)
        capacity_limit = (
            self.llm_context_window_tokens
            - self.llm_max_output_tokens
            - self.compression_safety_margin_tokens
        )
        return min(ratio_limit, capacity_limit)

    @property
    def compression_target_tokens(self) -> int:
        ratio_target = int(self.llm_context_window_tokens * self.compression_target_ratio)
        return min(ratio_target, max(1_024, self.compression_trigger_tokens - 1_024))

    @field_validator("justone_token", "iqs_api_key", mode="before")
    @classmethod
    def empty_provider_token_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_catalog_limits(self) -> Self:
        if not (
            self.catalog_minimum_total <= self.catalog_target_total <= self.catalog_hard_cap_total
        ):
            raise ValueError("catalog limits must satisfy minimum <= target <= hard cap")
        if self.catalog_soft_deadline_seconds > self.catalog_hard_deadline_seconds:
            raise ValueError("catalog soft deadline cannot exceed hard deadline")
        return self

    web_search_provider: Literal["none", "iqs"] = "iqs"
    iqs_api_key: SecretStr | None = None
    iqs_base_url: str = "https://cloud-iqs.aliyuncs.com"
    iqs_engine_type: Literal["Generic", "GenericAdvanced", "LiteAdvanced", "Deep"] = "LiteAdvanced"
    iqs_timeout_seconds: float = Field(default=8.0, gt=0)
    iqs_max_results: int = Field(default=10, ge=1, le=50)
    web_search_content_chars: int = Field(default=1_200, ge=100, le=4_000)

    product_search_backend: Literal["faiss"] = "faiss"
    # Long-term memory shares the same frozen 512d BGE-small encoder as transient
    # candidate FAISS. The local ONNX INT8 artifact is the only inference runtime;
    # nothing is downloaded or exported at request/worker time.
    embedding_model_name: str = "BAAI/bge-small-zh-v1.5"
    embedding_model_revision: str = "main"
    embedding_device: Literal["auto", "cpu", "cuda"] = "cpu"
    embedding_dimensions: int = Field(default=512, ge=1)
    embedding_batch_size: int = Field(default=16, ge=1)
    embedding_max_length: int = Field(default=256, ge=1)

    candidate_embedding_model_name: str = "BAAI/bge-small-zh-v1.5"
    candidate_embedding_model_revision: str = "main"
    candidate_embedding_dimensions: int = Field(default=512, ge=1)
    candidate_embedding_backend: Literal["auto", "cuda", "onnx"] = "auto"
    candidate_embedding_onnx_path: Path = Path("data/models/bge-small-zh-v1.5-onnx-int8")
    candidate_embedding_onnx_file: str = "onnx/model_qint8_avx512_vnni.onnx"
    candidate_embedding_batch_size: int = Field(default=64, ge=1)
    candidate_embedding_max_length: int = Field(default=128, ge=1)
    candidate_embedding_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    candidate_faiss_group_limit: int = Field(default=36, ge=3, le=120)
    candidate_embedding_cache_size: int = Field(default=10_000, ge=0, le=100_000)
    candidate_embedding_cache_ttl_seconds: int = Field(default=86_400, ge=1)
    candidate_rrf_rank_constant: int = Field(default=60, ge=1, le=1_000)

    memory_store_backend: Literal["pgvector"] = "pgvector"
    memory_recall_limit: int = Field(default=10, ge=1, le=50)
    memory_recall_candidate_pool: int = Field(default=50, ge=10, le=500)
    memory_prompt_token_limit: int = Field(default=1_200, ge=128, le=20_000)
    memory_processing_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    memory_consolidation_run_threshold: int = Field(default=10, ge=1, le=100)
    memory_consolidation_idle_seconds: int = Field(default=900, ge=60, le=86_400)
    memory_consolidation_poll_seconds: int = Field(default=5, ge=1, le=300)
    memory_consolidation_lease_seconds: int = Field(default=300, ge=30, le=3_600)
    memory_decay_window_days: int = Field(default=180, ge=1, le=3_650)
    memory_decay_floor: float = Field(default=0.6, ge=0.1, le=1.0)
    memory_history_retention_days: int = Field(default=180, ge=1, le=3_650)
    memory_outbox_max_attempts: int = Field(default=8, ge=1, le=32)
    memory_outbox_lease_seconds: int = Field(default=300, ge=30, le=3_600)
    product_dataset_path: Path = Path(
        "datasets/headphones_1000/structured/itemsearch_candidates.jsonl"
    )
    product_image_catalog_path: Path = Path(
        "datasets/justone_headphones/normalized/headphones.jsonl"
    )
    fork_candidate_limit: int = Field(default=10, ge=1, le=50)
    faiss_candidates_per_platform: int = Field(default=15, ge=1, le=50)
    item_rerank_group_limit: int = Field(default=36, ge=3, le=60)
    item_rerank_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    fork_max_depth: int = Field(default=1, ge=1, le=1)

    redis_url: str | None = "redis://127.0.0.1:6379/0"


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable-by-convention settings object."""

    return Settings()
