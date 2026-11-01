"""Real decoder/caching, revision authorization and process deadline regressions."""
import hashlib
from pathlib import Path
import time

import pymupdf
import pytest

from app.domain.exceptions import DependencyUnavailableError, NotFoundError, ValidationError
from app.infrastructure.config import Settings
from app.infrastructure.pdf.page_preview import PagePreviewRenderer
from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.storage import LocalFileStorage
from app.presentation.dependencies import AppContainer


def _slow_render(*_args):
    time.sleep(20)


@pytest.fixture
def indexed(tmp_path):
    container = AppContainer(Settings(_env_file=None, environment="test", data_dir=tmp_path/"data",
        embedding_provider="local", llm_provider="local", vector_backend="memory",
        auth_enabled=False, auto_seed_sample_manual=False, pdf_defer_previews=True,
        gemini_api_key="", openai_api_key="", groq_api_key="", anthropic_api_key=""))
    yield container
    container.close()


def _publish(container, rotation=0):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((100, 120), "Sensor ST-104: 85.0 grados.")
        page.draw_rect(pymupdf.Rect(100, 200, 300, 400), color=(1,0,0), fill=(1,0,0))
        page.set_cropbox(pymupdf.Rect(50, 70, 550, 750))
        page.set_rotation(rotation)
        job, _ = container.ingest_use_case.submit("manual.pdf", pdf.tobytes())
    result = container.ingest_use_case.execute_job(job.job_id)
    assert result.status.value == "completed"
    run = container.catalog.active_run_ids()[0]
    source = container.catalog.published_run_source(run)
    return job, run, container.storage.upload_path(source["source_id"])


@pytest.mark.parametrize("rotation", [0,90,180,270])
def test_deferred_preview_matches_eager_pixels_and_retains_authorization(indexed, tmp_path, monkeypatch, rotation):
    job, run, source = _publish(indexed, rotation)
    assert not indexed.storage.page_image_path(run, 1).exists()
    eager_storage = LocalFileStorage(tmp_path/"eager")
    PyMuPDFDocumentParser(eager_storage).parse(source, "run_eager", "manual.pdf")
    preview = indexed.page_previews.get(run, 1)
    assert hashlib.sha256(preview.read_bytes()).digest() == hashlib.sha256(
        eager_storage.page_image_path("run_eager", 1).read_bytes()).digest()
    monkeypatch.setattr(indexed.page_previews, "_create", lambda *_: pytest.fail("A cache hit must not decode again"))
    assert indexed.page_previews.get(run, 1) == preview
    with pytest.raises(NotFoundError):
        indexed.page_previews.get(run, 2)
    indexed.doc_repo.delete(job.document_id)
    with pytest.raises(NotFoundError):
        indexed.page_previews.get(run, 1)


def test_unpublished_preview_is_rejected_before_decoding_or_creating_files(indexed, monkeypatch):
    monkeypatch.setattr(indexed.page_previews, "_create", lambda *_: pytest.fail("Unpublished run"))
    with pytest.raises(NotFoundError):
        indexed.page_previews.get("run_unpublished", 1)
    assert not (indexed.storage.pages_root/"run_unpublished").exists()


def test_preview_pixel_budget_does_not_publish_a_partial_image(indexed):
    _, run, _ = _publish(indexed)
    restricted = PagePreviewRenderer(indexed.catalog, indexed.storage, max_pixels=1)
    with pytest.raises(ValidationError, match="píxeles"):
        restricted.get(run, 1)
    assert not indexed.storage.page_image_path(run, 1).exists()
    assert not list((indexed.storage.pages_root/run).glob("preview_*.png"))
    assert indexed.page_previews.get(run, 1).is_file()


def test_preview_wall_clock_deadline_kills_the_decoder_and_removes_partial_files(indexed, monkeypatch):
    from app.infrastructure.pdf import page_preview
    _, run, _ = _publish(indexed)
    monkeypatch.setattr(page_preview, "_render", _slow_render)
    renderer = PagePreviewRenderer(indexed.catalog, indexed.storage, timeout_seconds=2)
    began = time.monotonic()
    with pytest.raises(DependencyUnavailableError, match="tiempo permitido"):
        renderer.get(run, 1)
    assert time.monotonic()-began < 8
    assert not indexed.storage.page_image_path(run, 1).exists()
    assert not list((indexed.storage.pages_root/run).glob("preview_*.png"))


def test_success_and_failure_timings_are_durable_without_exposing_document_text(indexed, monkeypatch):
    job, _, source = _publish(indexed)
    timings = indexed.catalog.job_timings(job.job_id)
    assert timings["total_ms"] >= timings["parse_ms"] > 0
    assert "dense_embedding_ms" in timings and timings["parse_previews_ms"] == 0
    second, _ = indexed.ingest_use_case.submit("manual.pdf", source.read_bytes(), force_reingest=True)
    def invalid_pdf(**_kwargs):
        raise ValidationError("Synthetic extraction budget failure")
    monkeypatch.setattr(indexed.parser, "parse", invalid_pdf)
    assert indexed.ingest_use_case.execute_job(second.job_id).status.value == "failed"
    failed = indexed.catalog.job_timings(second.job_id)
    assert failed["total_ms"] >= failed["parse_ms"] >= 0
    assert all(isinstance(value, (float,int)) for value in failed.values())
