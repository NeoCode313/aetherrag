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
    container.refresh_private_settings()
    index = container.index_state()
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
        credentials_editable=True,
        semantic_retrieval=container.catalog.profile_by_id(container.catalog.active_generation()["profile_id"])["provider"] != "local",
        generation_mode="extractive" if container.active_provider_name == "local" else "generative",
        provider_ready=getattr(container.llm_provider, "supports_structured_output", True),
        openai_base_url=container.settings.openai_base_url,
        ollama_base_url=container.settings.ollama_base_url,
        response_format=container.settings.openai_response_format,
        build_id=container.build_id, index_compatible=index["compatible"],
        active_pipeline=index["active_pipeline"] or "unknown",
        migration_status=index["migration"]["status"],
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
        anthropic_api_key=payload.anthropic_api_key, anthropic_model=payload.anthropic_model,
        llm_supports_images=payload.llm_supports_images, openai_response_format=payload.openai_response_format,
        approve_endpoint=payload.approve_endpoint,
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
    # Provider quota failures must not take authentication/configuration out of service.
    ready = catalog_ok and vector_ok
    return JSONResponse({"status": "ready" if ready else "unavailable",
        "catalog_healthy": catalog_ok, "vector_store_healthy": vector_ok,
        "circuit_breaker": container.llm_breaker.state.value,
        "index_compatible": container.index_state()["compatible"],
        "provider": container.active_provider_name}, status_code=200 if ready else 503)

@router.get("/version")
def version(container: AppContainer = Depends(get_container)):
    return {"version": "4.1.0", "build_id": container.build_id, "environment": container.settings.environment,
            "pipeline": container.settings.pipeline_version, "index_compatible": container.index_state()["compatible"]}


@router.post("/recover-index", status_code=202)
def recover_index(container: AppContainer = Depends(get_container)):
    from app.application.services.index_recovery import start_recovery
    return start_recovery(container)


@router.post("/semantic-index", status_code=202, summary="Reconstruir con embeddings semánticos locales verificados")
def enable_semantic_index(container: AppContainer = Depends(get_container)):
    return container.enable_semantic_index()


@router.post("/test-provider", summary="Probar el proveedor guardado sin enviar documentos privados")
def test_provider(container: AppContainer = Depends(get_container)):
    import time
    from app.application.services.prompt_builder import SYSTEM_INSTRUCTION
    from app.domain.exceptions import ProviderContractError
    container.refresh_private_settings()
    provider = container.llm_provider
    start = time.monotonic()
    result = provider.generate_answer(SYSTEM_INSTRUCTION,
        '<evidence_data>[]</evidence_data>\n<sent_figures>[]</sent_figures>\n'
        '<request_data>{"question":"Prueba sintética sin evidencia. Debes abstenerte.","history":[]}</request_data>')
    if not result.abstained or result.claims:
        raise ProviderContractError("schema", ("abstained", "claims"))
    return {"status": "ok", "provider": container.active_provider_name, "model": provider.model_name,
            "latency_ms": int((time.monotonic()-start)*1000),
            "message": "Conexión y contrato de respuesta verificados con datos sintéticos; no se enviaron documentos."}
