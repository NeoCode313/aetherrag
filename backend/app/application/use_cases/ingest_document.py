"""Durable ingestion: outbox admission, leased attempts and atomic publication."""
from __future__ import annotations

import io
import json
import logging
import threading
import uuid
from dataclasses import asdict
from collections.abc import Callable

from app.domain.entities import ChunkKind, ImageInput
from app.domain.exceptions import ConflictError, ValidationError, TransientProviderError, DependencyUnavailableError

logger = logging.getLogger(__name__)


class IngestDocumentUseCase:
    def __init__(self, *, parser, chunker, spatial_linker, sparse_encoder, storage,
                 catalog, provider_resolver: Callable, store_resolver: Callable,
                 pipeline_resolver: Callable | None = None,
                 max_upload_mb: int = 50, lease_seconds: int = 180,
                 llm_provider=None, describe_visuals_on_ingest: bool = False):
        self.parser, self.chunker, self.spatial_linker = parser, chunker, spatial_linker
        self.sparse_encoder, self.storage, self.catalog = sparse_encoder, storage, catalog
        self.provider_resolver, self.store_resolver = provider_resolver, store_resolver
        self.pipeline_resolver = pipeline_resolver
        self.max_upload_bytes, self.lease_seconds = max_upload_mb * 1024 * 1024, lease_seconds
        self.llm_provider, self.describe_visuals_on_ingest = llm_provider, describe_visuals_on_ingest

    def submit(self, file_name: str, data: bytes, force_reingest: bool = False,
               idempotency_key: str | None = None):
        return self.submit_stream(file_name, io.BytesIO(data), force_reingest, idempotency_key)

    def submit_stream(self, file_name: str, stream, force_reingest: bool = False,
                      idempotency_key: str | None = None, generation=None, administrative=False):
        clean_name = (file_name or "manual.pdf").strip()
        if not clean_name.lower().endswith(".pdf") or len(clean_name) > 512:
            raise ValidationError("Se requiere un nombre PDF válido de hasta 512 caracteres")
        target = generation or self.catalog.active_generation()
        if not target:
            raise ConflictError("No hay generación de índice activa")
        profile = self.catalog.profile_by_id(target["profile_id"]) or {}
        if profile.get("pipeline_version", self.catalog.pipeline_version) != self.catalog.pipeline_version:
            raise ConflictError("El índice requiere recuperación. Inicia sesión y pulsa Recuperar índice anterior.")
        source_id, sha, size = self.storage.save_stream(stream, self.max_upload_bytes)
        return self.catalog.submit(clean_name, sha, size, source_id, target["generation_id"],
                                   target["profile_id"], force_reingest, idempotency_key, administrative=administrative)

    def execute_job(self, job_id: str):
        owner = "worker_" + uuid.uuid4().hex
        context = self.catalog.claim(job_id, owner, self.lease_seconds)
        if context is None:
            return self.catalog.job_repo.get(job_id)
        epoch, run_id = context["epoch"], context["run_id"]
        stopped, lost = threading.Event(), threading.Event()

        def renew():
            while not stopped.wait(max(0.2, self.lease_seconds / 3)):
                try:
                    if not self.catalog.heartbeat(job_id, owner, epoch, self.lease_seconds):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    logger.exception("Heartbeat failed job=%s run=%s", job_id, run_id)
                    return

        heartbeat = threading.Thread(target=renew, name="ingest-heartbeat", daemon=True)
        heartbeat.start()

        def progress(pct: float, stage: str, message: str):
            if lost.is_set() or not self.catalog.progress(job_id, owner, epoch, pct, stage, message):
                raise ConflictError("El intento perdió su lease y no puede publicar")

        try:
            if context["pipeline_version"] != self.catalog.pipeline_version:
                raise ConflictError("El pipeline del trabajo requiere su versión de worker; drene antes de migrar")
            embedder = self.provider_resolver(context["profile_id"])
            parser, chunker, linker = (self.pipeline_resolver(context["profile_id"]) if self.pipeline_resolver
                                       else (self.parser, self.chunker, self.spatial_linker))
            store = self.store_resolver(context["generation_id"], run_id=run_id)
            progress(5, "parsing", "Extrayendo texto, tablas y figuras")
            parsed = parser.parse(
                pdf_path=self.storage.upload_path(context["source_id"]), document_id=run_id,
                file_name=context["file_name"],
                on_progress=lambda f, m: progress(5 + f * 40, "parsing", m))
            for page in parsed.pages:
                linker.assign_captions(page)
            progress(50, "chunking", "Segmentando contenido y resolviendo figuras")
            chunks = chunker.chunk(parsed)
            linker.link(chunks, parsed.visuals)
            if not chunks:
                raise ValidationError("No hay contenido extraíble; active OCR para páginas escaneadas")
            for chunk in chunks:
                chunk.document_id = context["document_id"]
                chunk.storage_id = run_id
            progress(65, "embedding", "Generando representaciones del perfil fijado al trabajo")
            texts = [c.embedding_text for c in chunks]
            dense, sparse = [], []
            # Bound provider and vector requests; heartbeat remains active during network calls.
            for start in range(0, len(texts), 32):
                batch = texts[start:start + 32]
                dense.extend(embedder.embed_documents(batch))
                sparse.extend(self.sparse_encoder.encode_document(t) for t in batch)
                progress(65 + 20 * min(1, (start + len(batch)) / len(texts)),
                         "embedding", f"Procesados {min(start + len(batch), len(texts))}/{len(texts)} fragmentos")
            if len(dense) != len(chunks) or any(len(v) != embedder.dimension for v in dense):
                raise ValidationError("El proveedor devolvió embeddings incompletos o incompatibles")
            progress(90, "indexing", "Escribiendo intento aislado en el índice")
            store.ensure_collection(embedder.dimension)
            store.upsert(chunks, dense, sparse)
            if not store.verify_run(run_id, [c.chunk_id for c in chunks]):
                raise DependencyUnavailableError("La verificación del manifiesto vectorial falló")
            stats = {"page_count": len(parsed.pages), "chunk_count": len(chunks),
                     "visual_count": len(parsed.visuals),
                     "table_count": sum(c.kind == ChunkKind.TABLE for c in chunks)}
            manifest = {"schema_version": 2, "job_id": job_id, "run_id": run_id,
                        "revision_id": context["revision_id"], "generation_id": context["generation_id"],
                        "profile_id": context["profile_id"], "pipeline_version": context["pipeline_version"],
                        "stats": stats, "chunks": [asdict(c) for c in chunks]}
            path = self.storage.write_manifest(run_id, manifest)
            progress(99, "publishing", "Verificando lease y activando revisión")
            if not self.catalog.complete(job_id, owner, epoch, stats, str(path)):
                raise ConflictError("El catálogo rechazó la publicación del intento")
            logger.info("Published job=%s run=%s generation=%s chunks=%d",
                        job_id, run_id, context["generation_id"], len(chunks))
        except Exception as exc:
            logger.exception("Ingestion failed job=%s run=%s", job_id, run_id)
            self.catalog.fail(job_id, owner, epoch, str(exc),
                              retryable=isinstance(exc, (TransientProviderError, DependencyUnavailableError)))
        finally:
            stopped.set()
            heartbeat.join(timeout=2)
        return self.catalog.job_repo.get(job_id)
