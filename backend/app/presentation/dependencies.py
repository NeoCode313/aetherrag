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
from app.infrastructure.providers import digest, embedding_profile, make_embedder, make_llm, configured_llm
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
        from app.infrastructure.installation_state import InstallationState, source_build
        self.identity = InstallationState(self.settings.data_dir, self.settings.operator_username,
            self.settings.operator_password_hash, self.settings.session_secret)
        self.build_id = source_build()
        self.started_at = __import__("time").time()
        state = self.identity.read()
        owner = state.get("owner") or {}
        retrieval = state.get("retrieval") or {}
        if retrieval.get("embedding_provider") == "onnx":
            self.settings.embedding_provider = "onnx"
        if self.settings.environment == "test" and "auth_enabled" not in self.settings.model_fields_set:
            self.settings.auth_enabled = False
        elif self.settings.environment != "test":
            self.settings.auth_enabled = True
        self.settings.installation_id = state["installation_id"]
        self.settings.session_secret = state["session_secret"]
        if owner:
            self.settings.operator_username = owner["username"]
            self.settings.operator_password_hash = owner["password_hash"]
        if self.settings.environment != "test":
            self.settings.session_cookie_name = "aetherrag_" + state["installation_id"][:12]
        if self.settings.environment == "development" and "session_secure" not in self.settings.model_fields_set:
            self.settings.session_secure = False
        self._provider_base = self.settings.model_copy(deep=True)
        self._provider_revision = -1
        self._load_private_provider()
        if self.settings.environment == "production":
            if not self.settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValidationError("Producción requiere PostgreSQL")
            if self.settings.queue_backend != "celery" or self.settings.vector_backend != "qdrant":
                raise ValidationError("Producción requiere Celery y Qdrant remotos")
            if not owner:
                raise ValidationError("Configura el propietario mediante setup_env.py antes de producción")
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
        # UI generation credentials cannot silently change the embedding profile at restart.
        profile = embedding_profile(self._provider_base)
        fingerprint = digest(profile)
        self.configured_generation = self.catalog.ensure_generation(
            fingerprint, self.settings.qdrant_collection + "_" + fingerprint[:16], profile)
        active = self.catalog.active_generation()
        self.store_for_generation(active["generation_id"]).ensure_collection(
            self.resolve_embedder(active["profile_id"]).dimension)
        selected_llm = {} if self.identity.read().get("provider", {}).get("llm_provider") else (self.catalog.get_setting("llm_profile") or {})
        self.active_provider_name, self.llm_provider = configured_llm(self.settings, self.llm_executor,
            provider=selected_llm.get("provider"), model=selected_llm.get("model"))
        self.parser = PyMuPDFDocumentParser(
            storage=self.storage, ocr_enabled=self.settings.ocr_enabled,
            ocr_language=self.settings.ocr_language, max_pages=self.settings.pdf_max_pages,
            max_parse_seconds=self.settings.pdf_max_parse_seconds,
            allow_large_vectors=profile["pipeline_config"].get("allow_large_vectors", False))
        pipeline_spec = profile["pipeline_config"]
        counter = (self.resolve_embedder(self.configured_generation["profile_id"]).count_tokens
                   if pipeline_spec.get("token_counter") == "e5" else None)
        self.chunker = LayoutAwareChunker(pipeline_spec["chunk_max_tokens"], self.settings.chunk_overlap_ratio,
                                        token_counter=counter)
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

    def _load_private_provider(self):
        state = self.identity.read()
        for key, value in state.get("provider", {}).items():
            if key in {"llm_provider", "gemini_api_key", "gemini_model", "openai_api_key", "groq_api_key",
                       "openai_model", "openai_base_url", "ollama_base_url", "ollama_model",
                       "anthropic_api_key", "anthropic_model", "openai_response_format",
                       "llm_supports_images", "provider_allowed_hosts", "provider_timeout_seconds"}:
                setattr(self.settings, key, value)
        self._provider_revision = state["revision"]

    def refresh_private_settings(self):
        state = self.identity.read()
        if state["revision"] != self._provider_revision:
            with self._lock:
                self._load_private_provider()
                if state.get("retrieval", {}).get("embedding_provider") == "onnx" and self.settings.embedding_provider != "onnx":
                    self._configure_semantic_index()
                self._llm_cache.clear()
                selected = {} if state.get("provider", {}).get("llm_provider") else (self.catalog.get_setting("llm_profile") or {})
                self.active_provider_name, self.llm_provider = configured_llm(self.settings, self.llm_executor,
                    provider=selected.get("provider"), model=selected.get("model"))
                if state.get("provider"):
                    self.llm_breaker.record_success()
        self.settings.session_secret = state["session_secret"]
        owner = state.get("owner") or {}
        if owner:
            self.settings.operator_username = owner["username"]
            self.settings.operator_password_hash = owner["password_hash"]

    def index_state(self):
        active = self.catalog.active_generation()
        profile = self.catalog.profile_by_id(active["profile_id"])
        return {"running_pipeline": self.settings.pipeline_version,
                "active_pipeline": profile.get("pipeline_version"),
                "compatible": (profile.get("pipeline_version") == self.settings.pipeline_version
                               and active["profile_id"] == self.configured_generation["profile_id"]),
                "target_generation": self.configured_generation["generation_id"],
                "migration": self.catalog.get_setting("index_recovery") or {"status": "idle"}}

    def _configure_semantic_index(self):
        from app.infrastructure.llm.semantic_embedding import MultilingualE5EmbeddingProvider
        embedder = MultilingualE5EmbeddingProvider(self.settings.embedding_model_dir)
        candidate = self.settings.model_copy(update={"embedding_provider": "onnx"})
        profile = embedding_profile(candidate)
        fingerprint = digest(profile)
        generation = self.catalog.ensure_generation(fingerprint,
            candidate.qdrant_collection + "_" + fingerprint[:16], profile)
        # Keep the previous pipeline immutable for jobs admitted before migration.
        self._pipeline_cache[self.configured_generation["profile_id"]] = (self.parser, self.chunker, self.spatial_linker)
        spec = profile["pipeline_config"]
        parser = PyMuPDFDocumentParser(storage=self.storage, ocr_enabled=spec["ocr_enabled"],
            ocr_language=spec["ocr_language"], max_pages=candidate.pdf_max_pages,
            max_parse_seconds=candidate.pdf_max_parse_seconds, allow_large_vectors=True)
        chunker = LayoutAwareChunker(spec["chunk_max_tokens"], spec["chunk_overlap_ratio"], token_counter=embedder.count_tokens)
        self.settings.embedding_provider = "onnx"
        self._provider_base.embedding_provider = "onnx"
        self.configured_generation = generation
        self._embed_cache[generation["profile_id"]] = embedder
        self.parser, self.chunker = parser, chunker

    def enable_semantic_index(self):
        with self._lock:
            if self.settings.embedding_provider != "onnx":
                self._configure_semantic_index()
                self.identity.save_retrieval({"embedding_provider": "onnx"})
        from app.application.services.index_recovery import start_recovery
        return start_recovery(self)

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
                        max_parse_seconds=self.settings.pdf_max_parse_seconds,
                        allow_large_vectors=spec.get("allow_large_vectors", False)),
                    LayoutAwareChunker(spec["chunk_max_tokens"], spec["chunk_overlap_ratio"],
                        token_counter=(self.resolve_embedder(profile_id).count_tokens
                                       if spec.get("token_counter") == "e5" else None)),
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
        self.refresh_private_settings()
        snapshot = self.catalog.query_snapshot(document_ids)
        active, runs, shared = snapshot["generation"], snapshot["run_ids"], snapshot["llm_profile"]
        if self.identity.read().get("provider", {}).get("llm_provider"):
            shared = {}
        with self._lock:
            key = digest({"profile": shared, "settings": self.settings.model_dump(mode="json", include={
                "llm_provider", "gemini_api_key", "gemini_model", "openai_api_key", "groq_api_key",
                "openai_model", "openai_base_url", "ollama_model", "ollama_base_url",
                "llm_supports_images", "provider_timeout_seconds", "anthropic_api_key", "anthropic_model", "openai_response_format"})})
            if key not in self._llm_cache:
                value = configured_llm(self.settings, self.llm_executor,
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
            candidate = self.settings.model_copy(deep=True)
            if llm_provider == "local" and any(kwargs.get(k) for k in ("gemini_api_key", "openai_api_key", "anthropic_api_key", "openai_base_url", "ollama_base_url")):
                raise ValidationError("El modo extractivo no utiliza claves ni endpoints")
            from app.infrastructure.providers import validate_endpoint, approved_hosts
            if kwargs.get("openai_base_url"):
                endpoint = kwargs["openai_base_url"].strip()
                candidate.provider_allowed_hosts = approved_hosts(endpoint, candidate.provider_allowed_hosts, kwargs.get("approve_endpoint", False))
                candidate.openai_base_url = validate_endpoint(endpoint, candidate.provider_allowed_hosts)
                if endpoint != self.settings.openai_base_url:
                    candidate.openai_api_key = ""
                    candidate.groq_api_key = ""
            if kwargs.get("ollama_base_url"):
                endpoint = kwargs["ollama_base_url"].strip()
                candidate.provider_allowed_hosts = approved_hosts(endpoint, candidate.provider_allowed_hosts, kwargs.get("approve_endpoint", False))
                candidate.ollama_base_url = validate_endpoint(endpoint, candidate.provider_allowed_hosts)
            for key in ("gemini_api_key", "anthropic_api_key"):
                if kwargs.get(key):
                    setattr(candidate, key, kwargs[key].strip())
            if kwargs.get("openai_api_key"):
                from urllib.parse import urlsplit
                key = "groq_api_key" if urlsplit(candidate.openai_base_url).hostname == "api.groq.com" else "openai_api_key"
                setattr(candidate, key, kwargs["openai_api_key"].strip())
            for key in ("gemini_model", "openai_model", "ollama_model", "anthropic_model", "openai_response_format", "llm_supports_images"):
                if kwargs.get(key) is not None:
                    setattr(candidate, key, kwargs[key])
            model = kwargs.get({"gemini": "gemini_model", "openai": "openai_model", "ollama": "ollama_model", "anthropic": "anthropic_model"}.get(llm_provider, ""))
            choice, provider = make_llm(candidate, self.llm_executor, provider=llm_provider, model=model)
            default_model = {"gemini": candidate.gemini_model, "openai": candidate.openai_model,
                             "ollama": candidate.ollama_model, "anthropic": candidate.anthropic_model, "local": provider.model_name}[choice]
            spec = {"provider": choice, "model": model or default_model}
            candidate.llm_provider = choice
            values = {key: getattr(candidate, key) for key in (
                "llm_provider",
                "gemini_api_key", "gemini_model", "openai_api_key", "groq_api_key", "openai_model",
                "openai_base_url", "ollama_base_url", "ollama_model", "anthropic_api_key", "anthropic_model",
                "openai_response_format", "llm_supports_images", "provider_allowed_hosts", "provider_timeout_seconds")}
            # The atomic installation file is authoritative for selection AND credentials.
            # Catalog metadata is only a redacted compatibility mirror for older tooling.
            self.identity.save_provider(values)
            try:
                self.catalog.set_setting("llm_profile", spec)
            except Exception:
                logger.warning("Provider configured; redacted catalog mirror deferred")
            self.settings = candidate
            self._provider_revision = self.identity.read()["revision"]
            self._llm_cache.clear()
            self.active_provider_name, self.llm_provider = choice, provider
            self.llm_breaker.record_success()

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
        import hashlib
        from pathlib import Path
        from app.infrastructure import sample_manual
        # A regenerated code template must not reuse a stale PDF from a previous release.
        version = hashlib.sha256(Path(sample_manual.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
        path = self.settings.data_dir / "sample_manuals" / version / "Manual_Turbina_HX900.pdf"
        if not path.exists():
            build_sample_manual_pdf(path)
        with path.open("rb") as stream:
            job, dedup = self.ingest_use_case.submit_stream(path.name, stream, force)
        self.notify_dispatcher()
        return job.job_id, dedup

    def ensure_seeded_on_startup(self):
        if self.settings.auto_seed_sample_manual and self.settings.environment != "test" and self.index_state()["compatible"] and not self.doc_repo.list():
            self.seed_sample_manual()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._dispatcher:
            self._dispatcher.join(timeout=5)
        recovery = getattr(self, "_recovery_thread", None)
        if recovery and recovery.is_alive():
            recovery.join(timeout=self.settings.pdf_max_parse_seconds + 10)
            if recovery.is_alive():
                logger.warning("Recovery still draining; resources kept open until process exit")
                return
        if hasattr(self.task_queue, "close"):
            self.task_queue.close()
        self.vector_store.client.close()
        self.catalog.close()


@lru_cache(maxsize=1)
def get_container():
    return AppContainer()
