"""Document ingestion, job status tracking, and asset/page preview endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile, status, Header
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.domain.exceptions import NotFoundError
from app.presentation.dependencies import AppContainer, get_container
from app.presentation.schemas import (
    DocumentDTO,
    IngestionQueuedResponse,
    JobStatusResponse,
)

router = APIRouter(prefix="/api/v1", tags=["Ingestion & Documents"])


@router.post(
    "/documents/ingest",
    response_model=IngestionQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Subir e ingestar un documento PDF de forma asíncrona",
)
async def ingest_document(
    file: UploadFile = File(..., description="Documento técnico PDF"),
    force_reingest: bool = Query(default=False, description="Forzar reindexado aunque coincida el SHA-256"),
    container: AppContainer = Depends(get_container),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestionQueuedResponse:
    try:
        job, deduplicated = await run_in_threadpool(
            container.ingest_use_case.submit_stream,
            file.filename or "manual.pdf", file.file, force_reingest, idempotency_key)
    finally:
        await file.close()
    container.notify_dispatcher()
    return IngestionQueuedResponse(
        job_id=job.job_id,
        document_id=job.document_id,
        file_name=job.file_name,
        status=job.status.value,
        deduplicated=deduplicated,
        message=(
            "Documento idéntico detectado por hash SHA-256 (índice existente reutilizado)."
            if deduplicated
            else job.message
        ),
    )


@router.post(
    "/documents/seed-sample",
    response_model=IngestionQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generar e ingestar el manual técnico de demostración (Manual_Turbina_HX900.pdf)",
)
def seed_sample_manual(
    force: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
) -> IngestionQueuedResponse:
    job_id, deduplicated = container.seed_sample_manual(force=force)
    job = container.job_repo.get(job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} no encontrado.")
    return IngestionQueuedResponse(
        job_id=job.job_id,
        document_id=job.document_id,
        file_name=job.file_name,
        status=job.status.value,
        deduplicated=deduplicated,
        message=job.message,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Consultar el progreso en tiempo real de un trabajo de ingesta (Pending/Processing/Completed/Failed)",
)
def get_job_status(
    job_id: str,
    container: AppContainer = Depends(get_container),
) -> JobStatusResponse:
    job = container.job_repo.get(job_id)
    if job is None:
        raise NotFoundError(f"No existe ningún trabajo de ingesta con job_id='{job_id}'.")
    return JobStatusResponse(
        job_id=job.job_id,
        document_id=job.document_id,
        file_name=job.file_name,
        status=job.status.value,
        progress=job.progress,
        stage=job.stage,
        message=job.message,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get(
    "/documents",
    response_model=list[DocumentDTO],
    summary="Listar documentos técnicos procesados e indexados",
)
def list_documents(
    container: AppContainer = Depends(get_container),
) -> list[DocumentDTO]:
    docs = container.doc_repo.list()
    return [
        DocumentDTO(
            document_id=d.document_id,
            file_name=d.file_name,
            sha256=d.sha256,
            size_bytes=d.size_bytes,
            status=d.status.value,
            page_count=d.page_count,
            chunk_count=d.chunk_count,
            visual_count=d.visual_count,
            table_count=d.table_count,
            job_id=d.job_id,
            created_at=d.created_at,
            indexed_at=d.indexed_at,
        )
        for d in docs
    ]


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un documento y sus vectores/activos asociados",
)
def delete_document(
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> None:
    doc = container.doc_repo.get(document_id)
    if doc is None:
        raise NotFoundError(f"Documento '{document_id}' no encontrado.")
    # Tombstone first. Retain immutable runs for controlled garbage collection/rollback.
    container.doc_repo.delete(document_id)


@router.get(
    "/assets/{document_id}/{file_name}",
    summary="Servir imagen o diagrama recortado extraído del PDF",
)
def get_visual_asset(
    document_id: str,
    file_name: str,
    container: AppContainer = Depends(get_container),
) -> FileResponse:
    if not container.catalog.published_run_document(document_id):
        raise NotFoundError("El activo no pertenece a una revisión publicada")
    safe_name = file_name
    path = container.storage.asset_path(document_id, safe_name)
    if not path.exists():
        raise NotFoundError(f"Activo visual '{safe_name}' no encontrado.")
    return FileResponse(path, media_type="image/png")


@router.get(
    "/pages/{document_id}/{page_number}",
    summary="Servir vista completa de la página del PDF para superposición de Bounding Boxes",
)
def get_page_preview(
    document_id: str,
    page_number: int,
    container: AppContainer = Depends(get_container),
) -> FileResponse:
    if not container.catalog.published_run_document(document_id):
        raise NotFoundError("La página no pertenece a una revisión publicada")
    path = container.storage.page_image_path(document_id, page_number)
    if not path.exists():
        raise NotFoundError(f"Vista de página {page_number} no disponible para '{document_id}'.")
    return FileResponse(path, media_type="image/png")
