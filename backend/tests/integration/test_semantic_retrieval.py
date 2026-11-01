"""Opt-in real trained embeddings, no cloud API; fixtures contain no private documents."""
import os
from pathlib import Path

import numpy as np
import pytest

from app.infrastructure.llm.semantic_embedding import MultilingualE5EmbeddingProvider

MODEL_DIR = os.getenv("SEMANTIC_TEST_MODEL_DIR")
pytestmark = pytest.mark.skipif(not MODEL_DIR, reason="Pinned local model cache not configured")


def test_trained_multilingual_embeddings_retrieve_a_paraphrase_without_code_matching():
    embedder = MultilingualE5EmbeddingProvider(Path(MODEL_DIR))
    texts = ["El motor se detiene automáticamente cuando el refrigerante supera los ochenta grados.",
             "La distribución beta modela proporciones.",
             "La holgura nominal del cojinete es 0.18 mm."]
    documents = np.asarray(embedder.embed_documents(texts))
    query = np.asarray(embedder.embed_query("¿Qué sucede si se recalienta el equipo?"))
    assert documents.shape == (3, 384)
    assert np.allclose(np.linalg.norm(documents, axis=1), 1, atol=1e-5)
    assert int(np.argmax(documents @ query)) == 0
    spanish = np.asarray(embedder.embed_query("¿Cómo se representan porcentajes y fracciones de un total?"))
    assert int(np.argmax(documents @ spanish)) == 1


def test_semantic_migration_preserves_pdf_and_generator_across_restart(tmp_path):
    import pymupdf
    from app.infrastructure.config import Settings
    from app.presentation.dependencies import AppContainer
    settings = Settings(_env_file=None, environment="test", data_dir=tmp_path/"data",
        embedding_model_dir=Path(MODEL_DIR), embedding_provider="local", llm_provider="local",
        vector_backend="embedded", auth_enabled=False, auto_seed_sample_manual=False,
        openai_api_key="", groq_api_key="", gemini_api_key="", anthropic_api_key="")
    container = AppContainer(settings)
    try:
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((60,100), "El motor se detiene automaticamente cuando el refrigerante supera ochenta grados.")
            content = pdf.tobytes()
        job, _ = container.ingest_use_case.submit("motor.pdf", content)
        assert container.ingest_use_case.execute_job(job.job_id).status.value == "completed"
        original = container.catalog.active_generation()["generation_id"]
        assert container.enable_semantic_index()["status"] == "running"
        container._recovery_thread.join(timeout=60)
        assert container.index_state()["migration"]["status"] == "completed"
        assert container.index_state()["compatible"]
        assert container.catalog.active_generation()["generation_id"] != original
        assert container.query_use_case.embedder.dimension == 384
        assert container.doc_repo.list()[0].document_id == job.document_id
        assert container.active_provider_name == "local"
        assert container.identity.read()["retrieval"]["embedding_provider"] == "onnx"
    finally:
        container.close()
    restarted = AppContainer(settings.model_copy(update={"embedding_provider":"local"}))
    try:
        assert restarted.index_state()["compatible"]
        assert restarted.query_use_case.embedder.dimension == 384
        assert restarted.active_provider_name == "local"
        assert restarted.doc_repo.list()[0].document_id == job.document_id
    finally:
        restarted.close()
