"""Composition root: durable catalog, explicit providers and immutable query snapshots."""
from __future__ import annotations

import logging
import threading
import secrets
from functools import lru_cache

from app.application.services.hybrid_ranker import HybridRanker
from app.application.services.layout_chunker import LayoutAwareChunker
from app.application.services.prompt_builder import DefensivePromptBuilder
from app.application.services.spatial_linker import SpatialLinker
from app.application.use_cases.ingest_document import IngestDocumentUseCase
from app.application.use_cases.query_rag import QueryRAGUseCase
from app.domain.exceptions import ValidationError, ConflictError
from app.infrastructure.catalog import Catalog
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.providers import digest, embedding_profile, make_embedder, make_llm
from app.infrastructure.resilience import CircuitBreaker, ResilientExecutor
from app.infrastructure.sample_manual import build_sample_manual_pdf
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.tasks.queue_adapter import CeleryTaskQueue, ThreadedBackgroundTaskQueue
from app.infrastructure.vector_store.qdrant_adapter import QdrantHybridVectorStore
from app.infrastructure.vector_store.sparse_bm25 import HashedLexicalEncoder

logger = logging.getLogger(__name__)


class AppContainer:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if self.settings.pipeline_version != "layout-v3":
            raise ValidationError("Este código requiere PIPELINE_VERSION=layout-v3. Drene workers v2, construya la nueva generación y ejecute el cutover.")
        if self.settings.environment == "test" and "auth_enabled" not in self.settings.model_fields_set:
            self.settings.auth_enabled = False
        elif self.settings.environment == "development":
            if "auth_enabled" not in self.settings.model_fields_set and not self.settings.operator_password_hash:
                self.settings.auth_enabled = False
            if "session_secure" not in self.settings.model_fields_set:
                self.settings.session_secure = False
            if len(self.settings.session_secret) < 32:
                self.settings.session_secret = secrets.token_urlsafe(48)
        if self.settings.auth_enabled and (len(self.settings.session_secret) < 32 or not self.settings.operator_password_hash):
            raise ValidationError("Configure SESSION_SECRET y OPERATOR_PASSWORD_HASH para habilitar autenticación")
        if self.settings.environment == "production":
            if not self.settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValidationError("Producción requiere DATABASE_URL de PostgreSQL")
            if self.settings.queue_backend != "celery" or self.settings.vector_backend != "qdrant":
                raise ValidationError("Producción requiere Celery y Qdrant remotos explícitos")
            if (not self.settings.auth_enabled or len(self.settings.session_secret) < 32
                    or not self.settings.operator_password_hash):
                raise ValidationError("Configure autenticación y secretos antes de iniciar producción")
        self._lock = threading.RLock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._dispatcher = None
        self._embed_cache = {}
        self._llm_cache = {}
        self._pipeline_cache = {}
        self.storage = LocalFileStorage(self.settings.data_dir)
        url = self.settings.database_url
        if not url:
            if self.settings.environment in ("test", "development"):
                db_name = "catalog-test.sqlite" if self.settings.environment == "test" else "catalog-dev.sqlite"
                url = "sqlite:///" + str((self.settings.data_dir / db_name).resolve()).replace("\\", "/")
            else:
                raise ValidationError("Configure DATABASE_URL y ejecute PostgreSQL con Docker Compose")
        self.catalog = Catalog(url, allow_sqlite=self.settings.environment in ("test", "development"),
                               pipeline_version=self.settings.pipeline_version,
                               max_pending_jobs=self.settings.max_pending_jobs)
        self.job_repo, self.doc_repo = self.catalog.job_repo, self.catalog.document_repo
        self.llm_breaker = CircuitBreaker("llm", self.settings.circuit_breaker_failure_threshold,
                                         self.settings.circuit_breaker_recovery_seconds)
        self.embed_breaker = CircuitBreaker("embedding", self.settings.circuit_breaker_failure_threshold,
                                           self.settings.circuit_breaker_recovery_seconds)
        def executor(breaker):
            return ResilientExecutor(breaker, self.settings.retry_max_attempts,
                                     self.settings.retry_base_delay_seconds, self.settings.retry_max_delay_seconds)
        self.llm_executor, self.embed_executor = executor(self.llm_breaker), executor(self.embed_breaker)
        memory = self.settings.environment == "test" or self.settings.vector_backend == "memory"
        embedded = self.settings.vector_backend == "embedded" or (
            self.settings.vector_backend == "auto"
            and self.settings.environment == "development"
            and not self.settings.database_url
        )
        self.vector_store = QdrantHybridVectorStore(
            collection_name=self.settings.qdrant_collection, in_memory=memory,
            url=None if memory or embedded else self.settings.qdrant_url,
            api_key=self.settings.qdrant_api_key,
            local_path=self.settings.data_dir / "qdrant_local" if embedded else None)
        profile = embedding_profile(self.settings)
        fingerprint = digest(profile)
        self.configured_generation = self.catalog.ensure_generation(
            fingerprint, self.settings.qdrant_collection + "_" + fingerprint[:16], profile)
        active = self.catalog.active_generation()
        self.store_for_generation(active["generation_id"]).ensure_collection(
            self.resolve_embedder(active["profile_id"]).dimension)
        self.active_provider_name, self.llm_provider = make_llm(self.settings, self.llm_executor)
        self.parser = PyMuPDFDocumentParser(
            storage=self.storage, ocr_enabled=self.settings.ocr_enabled,
            ocr_language=self.settings.ocr_language, max_pages=self.settings.pdf_max_pages,
            max_parse_seconds=self.settings.pdf_max_parse_seconds)
        self.chunker = LayoutAwareChunker(self.settings.chunk_max_tokens, self.settings.chunk_overlap_ratio)
        self.spatial_linker = SpatialLinker(max_distance=self.settings.spatial_max_distance)
        self.sparse_encoder = HashedLexicalEncoder()
        self.ranker = HybridRanker(rrf_k=self.settings.rrf_k, dense_weight=self.settings.dense_weight,
                                   sparse_weight=self.settings.sparse_weight)
        self.prompt_builder = DefensivePromptBuilder()
        self.ingest_use_case = IngestDocumentUseCase(
            parser=self.parser, chunker=self.chunker, spatial_linker=self.spatial_linker,
            sparse_encoder=self.sparse_encoder, storage=self.storage, catalog=self.catalog,
            provider_resolver=self.resolve_embedder, store_resolver=self.store_for_generation,
            pipeline_resolver=self.resolve_pipeline,
            max_upload_mb=self.settings.max_upload_mb, lease_seconds=self.settings.job_lease_seconds)
        self.task_queue = (CeleryTaskQueue() if self.settings.queue_backend == "celery"
                           else ThreadedBackgroundTaskQueue(self.ingest_use_case.execute_job, max_workers=2))

    def resolve_pipeline(self, profile_id):
        with self._lock:
            if profile_id == self.configured_generation["profile_id"]:
                return self.parser, self.chunker, self.spatial_linker
            if profile_id not in self._pipeline_cache:
                profile = self.catalog.profile_by_id(profile_id) or {}
                spec = profile.get("pipeline_config")
                if profile.get("pipeline_version") != "layout-v3" or not spec:
                    raise ConflictError("El perfil no contiene una configuración reproducible del pipeline v3")
                self._pipeline_cache[profile_id] = (
                    PyMuPDFDocumentParser(storage=self.storage, ocr_enabled=spec["ocr_enabled"],
                        ocr_language=spec["ocr_language"], max_pages=self.settings.pdf_max_pages,
                        max_parse_seconds=self.settings.pdf_max_parse_seconds),
                    LayoutAwareChunker(spec["chunk_max_tokens"], spec["chunk_overlap_ratio"]),
                    SpatialLinker(max_distance=spec["spatial_max_distance"]))
            return self._pipeline_cache[profile_id]

    def resolve_embedder(self, profile_id):
        with self._lock:
            if profile_id not in self._embed_cache:
                profile = self.catalog.profile_by_id(profile_id)
                if not profile:
                    raise ConflictError("Perfil de embeddings inexistente")
                self._embed_cache[profile_id] = make_embedder(profile, self.settings, self.embed_executor)
            return self._embed_cache[profile_id]

    @property
    def embedding_provider(self):
        return self.resolve_embedder(self.catalog.active_generation()["profile_id"])

    def store_for_generation(self, generation_id, *, run_id=None, active_run_ids=None):
        generation = self.catalog.generation_by_id(generation_id)
        if not generation:
            raise ConflictError("Generación inexistente")
        return self.vector_store.view(generation["collection"], run_id=run_id,
                                      active_run_ids=active_run_ids)

    @property
    def query_use_case(self):
        return self.query_for()

    def query_for(self, document_ids=None):
        # Physical collection, query embedder and active runs are pinned for this request.
        snapshot = self.catalog.query_snapshot(document_ids)
        active, runs, shared = snapshot["generation"], snapshot["run_ids"], snapshot["llm_profile"]
        with self._lock:
            key = digest({"profile": shared, "settings": self.settings.model_dump(mode="json", include={
                "llm_provider", "gemini_api_key", "gemini_model", "openai_api_key", "groq_api_key",
                "openai_model", "openai_base_url", "ollama_model", "ollama_base_url",
                "llm_supports_images", "provider_timeout_seconds"})})
            if key not in self._llm_cache:
                value = make_llm(self.settings, self.llm_executor,
                                 provider=shared.get("provider"), model=shared.get("model"))
                self._llm_cache = {key: value}
            choice, llm = self._llm_cache[key]
            self.active_provider_name, self.llm_provider = choice, llm
        query = QueryRAGUseCase(
            embedder=self.resolve_embedder(active["profile_id"]), sparse_encoder=self.sparse_encoder,
            vector_store=self.vector_store.view(active["collection"], active_run_ids=runs),
            ranker=self.ranker, prompt_builder=self.prompt_builder, llm=llm, storage=self.storage,
            top_k=self.settings.retrieval_top_k, candidate_pool=self.settings.retrieval_candidate_pool,
            min_dense_score=self.settings.min_dense_score, attach_images_to_llm=self.settings.attach_images_to_llm)
        query.generation_id = active["generation_id"]
        query.profile_id = active["profile_id"]
        return query

    def update_provider(self, llm_provider, **kwargs):
        with self._lock:
            # In test/production or for local provider, raw secrets/URLs are rejected.
            for key in ("gemini_api_key", "openai_api_key", "openai_base_url", "ollama_base_url"):
                if kwargs.get(key) and (self.settings.environment != "development" or llm_provider == "local"):
                    raise ValidationError("Las claves y URLs se configuran mediante secretos del servidor")
            if self.settings.environment == "development" and llm_provider != "local":
                from app.infrastructure.providers import validate_endpoint
                if kwargs.get("gemini_api_key"):
                    self.settings.gemini_api_key = kwargs["gemini_api_key"].strip().strip("'\"")
                if kwargs.get("ollama_base_url"):
                    self.settings.ollama_base_url = validate_endpoint(kwargs["ollama_base_url"].strip(), self.settings.provider_allowed_hosts)
                if kwargs.get("openai_base_url"):
                    self.settings.openai_base_url = validate_endpoint(kwargs["openai_base_url"].strip(), self.settings.provider_allowed_hosts)
                if kwargs.get("openai_api_key"):
                    clean_key = kwargs["openai_api_key"].strip().strip("'\"")
                    self.settings.openai_api_key = clean_key
                    if "groq.com" in self.settings.openai_base_url.lower() or clean_key.startswith("gsk_"):
                        self.settings.groq_api_key = clean_key
            model = kwargs.get({"gemini": "gemini_model", "openai": "openai_model", "ollama": "ollama_model"}.get(llm_provider, ""))
            choice, provider = make_llm(self.settings, self.llm_executor, provider=llm_provider, model=model)
            spec = {"provider": choice, "model": model or provider.model_name}
            self.catalog.set_setting("llm_profile", spec)
            self.active_provider_name, self.llm_provider = choice, provider

    def dispatch_once(self):
        self.catalog.reconcile()
        for event in self.catalog.ready_outbox(limit=16):
            self.task_queue.enqueue_ingestion(event["job_id"])
            self.catalog.mark_dispatched(event["event_id"])

    def start_dispatcher(self):
        if self._dispatcher and self._dispatcher.is_alive():
            return
        def run():
            while not self._stop.is_set():
                try:
                    self.dispatch_once()
                except Exception:
                    logger.exception("Outbox dispatch deferred; durable jobs remain queued")
                self._wake.wait(self.settings.dispatcher_interval_seconds)
                self._wake.clear()
        self._dispatcher = threading.Thread(target=run, name="outbox-dispatcher", daemon=True)
        self._dispatcher.start()

    def notify_dispatcher(self):
        self._wake.set()

    def seed_sample_manual(self, force=False):
        path = self.settings.data_dir / "sample_manuals" / "Manual_Turbina_HX900.pdf"
        if not path.exists():
            build_sample_manual_pdf(path)
        with path.open("rb") as stream:
            job, dedup = self.ingest_use_case.submit_stream(path.name, stream, force)
        self.notify_dispatcher()
        return job.job_id, dedup

    def ensure_seeded_on_startup(self):
        if self.settings.auto_seed_sample_manual and self.settings.environment != "test" and not self.doc_repo.list():
            self.seed_sample_manual()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._dispatcher:
            self._dispatcher.join(timeout=5)
        if hasattr(self.task_queue, "close"):
            self.task_queue.close()
        self.vector_store.client.close()
        self.catalog.close()


@lru_cache(maxsize=1)
def get_container():
    return AppContainer()
