"""Environment-driven configuration using Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = "AetherRAG Multimodal Engine"
    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    data_dir: Path = Field(default=Path("./data"))
    max_upload_mb: int = 50
    auto_seed_sample_manual: bool = True

    # Provider selection (Dependency Injection switches)
    llm_provider: Literal["gemini", "ollama", "openai", "local", "auto"] = "auto"
    embedding_provider: Literal["gemini", "ollama", "openai", "local", "auto"] = "auto"
    vector_backend: Literal["qdrant", "embedded", "memory", "auto"] = "auto"
    queue_backend: Literal["background", "celery"] = "background"
    state_backend: Literal["memory", "postgres", "redis", "auto"] = "auto"
    database_url: str = ""
    job_lease_seconds: int = 180
    dispatcher_interval_seconds: float = 2.0
    max_pending_jobs: int = 1000
    auth_enabled: bool = True
    operator_username: str = "admin"
    operator_password_hash: str = ""
    session_secret: str = ""
    session_secure: bool = True
    session_ttl_seconds: int = 28800
    cors_origins: list[str] = []
    provider_allowed_hosts: list[str] = ["api.openai.com", "api.groq.com", "localhost", "127.0.0.1", "ollama"]
    llm_supports_images: bool = False
    ocr_enabled: bool = False
    ocr_language: str = "eng"
    pdf_max_pages: int = 500
    pdf_max_parse_seconds: float = 120.0
    pipeline_version: str = "layout-v3"

    # Google Gemini (Current SDK: google-genai >= 2.30.1)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimension: int = 768

    # Ollama (Local open-weights fallback / alternative)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_embedding_model: str = "nomic-embed-text"

    # OpenAI-compatible adapter
    openai_api_key: str = ""
    groq_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    # Qdrant Vector DB
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "multimodal_technical_chunks"

    # Redis & Celery
    redis_url: str = "redis://localhost:6379/0"

    # RAG & Chunking hyperparameters
    chunk_max_tokens: int = 1800
    chunk_overlap_ratio: float = 0.15
    spatial_max_distance: float = 0.38
    retrieval_top_k: int = 6
    retrieval_candidate_pool: int = 16
    min_dense_score: float = 0.20
    rrf_k: int = 60
    dense_weight: float = 0.65
    sparse_weight: float = 0.35
    describe_visuals_on_ingest: bool = False
    attach_images_to_llm: bool = True

    # Resilience (Retry + Circuit Breaker)
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.6
    retry_max_delay_seconds: float = 6.0
    circuit_breaker_failure_threshold: int = 4
    circuit_breaker_recovery_seconds: float = 30.0
    provider_timeout_seconds: float = 45.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
