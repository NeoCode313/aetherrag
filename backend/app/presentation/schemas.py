"""Pydantic v2 HTTP request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TurnDTO(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class ChatQueryRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=2500, description="Pregunta en lenguaje natural")
    history: list[TurnDTO] = Field(default_factory=list, max_length=12)
    document_ids: list[str] | None = Field(default=None, description="Filtro opcional de documentos")
    top_k: int | None = Field(default=None, ge=1, le=12)


class CitationDTO(BaseModel):
    ref: int
    modality: str = "text"
    chunk_id: str
    document_id: str
    document_name: str
    page: int
    section: str
    kind: str
    snippet: str
    bbox: list[float]
    score: float
    dense_score: float | None = None
    sparse_score: float | None = None
    page_image_url: str


class RAGMetricsDTO(BaseModel):
    max_cosine_similarity: float = 0.0
    mean_cosine_similarity: float = 0.0
    max_bm25_score: float = 0.0
    visual_proximity_score: float | None = None
    images_sent: int = 0
    lexical_score: float | None = None
    reference_validity: float | None = None
    lexical_overlap_score: float | None = None
    top_rrf_score: float = 0.0
    faithfulness_score: float | None = None
    citation_precision: float | None = None
    answer_relevance: float | None = None
    context_utilization: float = 0.0
    spatial_alignment_score: float = 0.0


class VisualEvidenceDTO(BaseModel):
    asset_id: str
    document_id: str
    document_name: str
    page: int
    kind: str
    caption: str
    figure_number: int | str | None = None
    figure_label: str | None = None
    inner_text: str = ""
    bbox: list[float]
    anchor_chunk_id: str
    anchor_bbox: list[float]
    distance: float | None
    reason: str
    image_url: str
    page_image_url: str
    width_px: int = 0
    height_px: int = 0


class ChatQueryResponse(BaseModel):
    answer: str
    message_id: str = ""
    answer_mode: str = "grounded_answer"
    metadata: dict = Field(default_factory=dict)
    abstained: bool
    abstention_reason: str | None = None
    evidence_coverage: str
    citations: list[CitationDTO]
    primary_visual: VisualEvidenceDTO | None = None
    related_visuals: list[VisualEvidenceDTO] = Field(default_factory=list)
    retrieved: int
    model: str
    latency_ms: int
    timings_ms: dict[str, int]
    metrics: RAGMetricsDTO = Field(default_factory=RAGMetricsDTO)


class IngestionQueuedResponse(BaseModel):
    job_id: str
    document_id: str
    file_name: str
    status: str
    deduplicated: bool = False
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    document_id: str
    file_name: str
    status: str
    progress: float
    stage: str
    message: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DocumentDTO(BaseModel):
    document_id: str
    file_name: str
    sha256: str
    size_bytes: int
    status: str
    page_count: int
    chunk_count: int
    visual_count: int
    table_count: int
    job_id: str | None = None
    created_at: datetime
    indexed_at: datetime | None = None


class ProviderConfigRequest(BaseModel):
    llm_provider: Literal["gemini", "ollama", "openai", "anthropic", "local"] = "gemini"
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    llm_supports_images: bool | None = None
    openai_response_format: Literal["auto", "json_schema", "json_object"] | None = None
    approve_endpoint: bool = False
    reingest_sample: bool = False


class SystemStatusResponse(BaseModel):
    app_name: str
    environment: str
    llm_provider: str
    llm_model: str
    embedding_model: str
    embedding_dimension: int
    vector_backend: str
    queue_backend: str
    circuit_breaker_state: str
    circuit_breaker_failures: int
    gemini_configured: bool
    documents_indexed: int
    total_chunks: int
    total_visuals: int
    generation_id: str = ""
    supports_images: bool = False
    credentials_editable: bool = False
    provider_ready: bool = True
    app_version: str = "4.0.0"
    build_id: str = ""
    index_compatible: bool = True
    active_pipeline: str = ""
    migration_status: str = "idle"
    openai_base_url: str = ""
    ollama_base_url: str = ""
    response_format: str = "auto"
    openai_backend: Literal["groq", "openai", "custom"] = "custom"
