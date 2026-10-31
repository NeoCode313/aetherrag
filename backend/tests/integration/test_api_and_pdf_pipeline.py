"""End-to-end integration tests for FastAPI contracts, async ingestion, and multimodal PDF parsing."""

from __future__ import annotations

import time
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.infrastructure.config import Settings
from app.infrastructure.sample_manual import build_sample_manual_pdf
from app.main import create_app
from app.presentation.dependencies import AppContainer, get_container


def test_full_ingestion_and_multimodal_query_lifecycle(tmp_path: Path) -> None:
    test_settings = Settings(
        environment="test",
        data_dir=tmp_path / "data",
        llm_provider="local",
        vector_backend="memory",
        queue_backend="background",
        state_backend="memory",
        auto_seed_sample_manual=False,
    )
    container = AppContainer(settings=test_settings)
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container

    sample_pdf = build_sample_manual_pdf(tmp_path / "Manual_Turbina_HX900.pdf")

    with TestClient(app) as client:
        # 1. Reject invalid non-PDF upload with HTTP 422
        bad_resp = client.post(
            "/api/v1/documents/ingest",
            files={"file": ("notes.txt", b"hello world", "text/plain")},
        )
        assert bad_resp.status_code == 422

        # 2. Upload valid technical PDF -> returns 202 Accepted + job_id
        with sample_pdf.open("rb") as f:
            ingest_resp = client.post(
                "/api/v1/documents/ingest",
                files={"file": ("Manual_Turbina_HX900.pdf", f, "application/pdf")},
            )
        assert ingest_resp.status_code == 202
        queued = ingest_resp.json()
        job_id = queued["job_id"]
        assert queued["deduplicated"] is False

        # 3. Poll job endpoint until COMPLETED
        status_val = queued["status"]
        for _ in range(60):
            job_resp = client.get(f"/api/v1/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            status_val = job_data["status"]
            if status_val in {"completed", "failed"}:
                break
            time.sleep(0.1)
        assert status_val == "completed"

        # 4. Verify SHA-256 idempotency on duplicate upload
        with sample_pdf.open("rb") as f:
            dup_resp = client.post(
                "/api/v1/documents/ingest",
                files={"file": ("Manual_Turbina_HX900.pdf", f, "application/pdf")},
            )
        assert dup_resp.status_code == 202
        assert dup_resp.json()["deduplicated"] is True

        # 5. Verify document metadata (pages, chunks, extracted figures & tables)
        docs_resp = client.get("/api/v1/documents")
        assert docs_resp.status_code == 200
        docs = docs_resp.json()
        assert len(docs) == 1
        assert docs[0]["page_count"] == 3
        assert docs[0]["visual_count"] >= 2
        assert docs[0]["table_count"] >= 2

        # 6. Execute multimodal RAG query about sensor ST-104 & Figure 2
        q_resp = client.post(
            "/api/v1/chat/query",
            json={
                "question": "¿Dónde se ubica la sonda RTD ST-104 en la Figura 2 y a qué temperatura dispara TRIP-TEMP-02?",
            },
        )
        assert q_resp.status_code == 200
        ans = q_resp.json()
        assert ans["abstained"] is False, ans
        assert ans["answer_mode"] == "extractive_evidence"
        assert re.search(r"(?<!\d)85(?:[.,]0)?\s*°?C(?!\w)", ans["answer"]), ans["answer"]
        assert ans["metadata"]["supports_images"] is False
        assert ans["metadata"].get("used_asset_ids", []) == []
        assert ans["metrics"]["faithfulness_score"] is None
        assert len(ans["citations"]) >= 1
        assert ans["primary_visual"] is not None
        assert ans["primary_visual"]["page"] == 2

        # Verify asset and page preview endpoints serve valid PNG images
        img_resp = client.get(ans["primary_visual"]["image_url"])
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"] == "image/png"

        page_resp = client.get(ans["primary_visual"]["page_image_url"])
        assert page_resp.status_code == 200
        assert page_resp.headers["content-type"] == "image/png"

        # 7. Execute out-of-domain query -> defensive abstention
        abst_resp = client.post(
            "/api/v1/chat/query",
            json={"question": "¿Cuál es la receta tradicional de la paella valenciana?"},
        )
        assert abst_resp.status_code == 200
        assert abst_resp.json()["abstained"] is True
