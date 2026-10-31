"""FastAPI entrypoint for the AetherRAG Multimodal System."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.domain.exceptions import (
    ConflictError, DependencyUnavailableError, CapacityError,
    DomainError,
    NotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TransientProviderError,
    ValidationError,
)
from app.infrastructure.config import get_settings
from app.infrastructure.http_limits import RequestBodyLimitMiddleware
from app.infrastructure.logging import configure_logging, request_id_ctx
from app.presentation.dependencies import get_container
from app.presentation.routes import chat, documents, system
from app.presentation import auth

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=(settings.environment == "production"),
    )
    resolver = app_instance.dependency_overrides.get(get_container, get_container)
    container = resolver()
    container.ensure_seeded_on_startup()
    logger.info(
        "%s ready (provider=%s, model=%s, vector_backend=%s)",
        settings.app_name,
        container.active_provider_name,
        container.llm_provider.model_name,
        getattr(container.vector_store, "mode", settings.vector_backend),
    )
    container.start_dispatcher()
    try:
        yield
    finally:
        container.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="3.0.0",
        docs_url=None,
        redoc_url=None,
        description=(
            "Sistema RAG Multimodal de Grado Empresarial con Arquitectura Hexagonal, "
            "Ingesta Asíncrona con Mapeo Espacial de Bounding Boxes, Recuperación Híbrida RRF "
            "y Generación Defensiva Anti-Alucinaciones."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=(settings.max_upload_mb + 1) * 1024 * 1024)

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:10]
        token = request_id_ctx.set(req_id)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            response.headers["X-Request-ID"] = req_id
            response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
            return response
        finally:
            request_id_ctx.reset(token)

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        path = request.url.path
        public = path in {"/api/v1/system/live", "/api/v1/system/health", "/api/v1/auth/login"}
        if path.startswith("/api/") and not public:
            try:
                auth.session(request, mutate=request.method not in {"GET", "HEAD", "OPTIONS"})
            except Exception as exc:
                from fastapi import HTTPException
                if isinstance(exc, HTTPException):
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                raise
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ConflictError)
    async def handle_conflict(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(DependencyUnavailableError)
    async def handle_dependency(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(CapacityError)
    async def handle_capacity(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "10"})

    # ------------------------------------------------------------------ exception mapping
    @app.exception_handler(ValidationError)
    async def handle_validation_error(_req: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "validation_error", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(NotFoundError)
    async def handle_not_found(_req: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "not_found", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(RateLimitError)
    async def handle_rate_limit(_req: Request, exc: RateLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "rate_limit_exceeded", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(ProviderUnavailableError)
    async def handle_circuit_open(_req: Request, exc: ProviderUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": "circuit_breaker_open", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(TransientProviderError)
    async def handle_transient_provider(_req: Request, exc: TransientProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"error": "upstream_timeout", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(ProviderError)
    async def handle_provider_error(_req: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "provider_error", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    @app.exception_handler(DomainError)
    async def handle_domain_error(_req: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "domain_error", "detail": str(exc), "request_id": request_id_ctx.get()},
        )

    # ------------------------------------------------------------------ routes
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(chat.router)
    app.include_router(system.router)

    # Serve the web client UI
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/docs", include_in_schema=False)
        async def serve_api_docs() -> FileResponse:
            # Self-hosted assets and external initializer work with script-src self.
            return FileResponse(frontend_dir / "api-docs.html")

        @app.get("/", include_in_schema=False)
        async def serve_spa() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

    return app


app = create_app()
