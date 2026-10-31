"""System status, healthcheck, and hot-swap provider configuration endpoints."""

from __future__ import annotations
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.domain.entities import JobStatus
from app.presentation.dependencies import AppContainer, get_container
from app.presentation.schemas import ProviderConfigRequest, SystemStatusResponse

router = APIRouter(prefix="/api/v1/system", tags=["System & Configuration"])


def _build_status(container: AppContainer) -> SystemStatusResponse:
    docs = [d for d in container.doc_repo.list() if d.status == JobStatus.COMPLETED]
    vec_mode = getattr(container.vector_store, "mode", container.settings.vector_backend)
    return SystemStatusResponse(
        app_name=container.settings.app_name,
        environment=container.settings.environment,
        llm_provider=container.active_provider_name,
        llm_model=container.llm_provider.model_name,
        embedding_model=container.embedding_provider.model_name,
        embedding_dimension=container.embedding_provider.dimension,
        vector_backend=str(vec_mode),
        queue_backend=container.settings.queue_backend,
        circuit_breaker_state=container.llm_breaker.state.value,
        circuit_breaker_failures=container.llm_breaker.failure_count,
        gemini_configured=bool(container.settings.gemini_api_key),
        documents_indexed=len(docs),
        total_chunks=sum(d.chunk_count for d in docs),
        total_visuals=sum(d.visual_count for d in docs),
        generation_id=container.catalog.active_generation()["generation_id"],
        supports_images=getattr(container.llm_provider, "supports_images", False),
        credentials_editable=container.settings.environment == "development",
        openai_backend={"api.groq.com": "groq", "api.openai.com": "openai"}.get(
            urlsplit(container.settings.openai_base_url).hostname, "custom"),
    )


@router.get("/status", response_model=SystemStatusResponse, summary="Estado operativo y proveedor IA activo")
def get_system_status(
    container: AppContainer = Depends(get_container),
) -> SystemStatusResponse:
    return _build_status(container)


@router.post(
    "/provider",
    response_model=SystemStatusResponse,
    summary="Intercambiar en caliente el proveedor de IA (Gemini / Ollama / OpenAI / Local) por Inyección de Dependencias",
)
def configure_provider(
    payload: ProviderConfigRequest,
    container: AppContainer = Depends(get_container),
) -> SystemStatusResponse:
    container.update_provider(
        llm_provider=payload.llm_provider,
        gemini_api_key=payload.gemini_api_key,
        gemini_model=payload.gemini_model,
        ollama_base_url=payload.ollama_base_url,
        ollama_model=payload.ollama_model,
        openai_api_key=payload.openai_api_key,
        openai_model=payload.openai_model,
        openai_base_url=payload.openai_base_url,
        reingest_sample=payload.reingest_sample,
    )
    return _build_status(container)


@router.get("/live", summary="Process liveness")
def live():
    return {"status": "alive"}


@router.get("/health", summary="Readiness of durable catalog and vector service")
def healthcheck(container: AppContainer = Depends(get_container)):
    catalog_ok = container.catalog.healthy()
    vector_ok = container.vector_store.healthy()
    ready = catalog_ok and vector_ok and container.llm_breaker.state.value != "open" and container.embed_breaker.state.value != "open"
    return JSONResponse({"status": "ready" if ready else "unavailable",
        "catalog_healthy": catalog_ok, "vector_store_healthy": vector_ok,
        "circuit_breaker": container.llm_breaker.state.value,
        "provider": container.active_provider_name}, status_code=200 if ready else 503)
