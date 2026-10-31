"""Opt-in HTTPS/API -> PostgreSQL outbox -> Redis/Celery -> remote Qdrant smoke.

Requires STACK_TEST_URL and STACK_TEST_PASSWORD for the dedicated test deployment.
The test uploads a newly generated small manual and deletes only that document.
It uses local extractive models; it does not claim external multimodal-model quality.
"""
import os
import time
import uuid

import httpx
import pytest

from app.infrastructure.sample_manual import build_sample_manual_pdf

URL = os.getenv("STACK_TEST_URL")
PASSWORD = os.getenv("STACK_TEST_PASSWORD")
pytestmark = pytest.mark.skipif(not URL or not PASSWORD, reason="Dedicated stack URL/password are not configured")


def test_real_durable_ingestion_and_authenticated_query(tmp_path):
    # Trust only the test endpoint's private local CA exception. Public deployments
    # should provide STACK_TEST_CA with the trusted certificate file.
    verify = os.getenv("STACK_TEST_CA") or False
    with httpx.Client(base_url=URL, verify=verify, timeout=45) as client:
        for _ in range(40):
            try:
                if client.get("/api/v1/system/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("Stack did not become ready")
        assert client.get("/api/v1/documents").status_code == 401
        login = client.post("/api/v1/auth/login", json={
            "username": os.getenv("STACK_TEST_USERNAME", "admin"), "password": PASSWORD,
        })
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        pdf = build_sample_manual_pdf(tmp_path / f"stack_{uuid.uuid4().hex}.pdf")
        with pdf.open("rb") as stream:
            response = client.post("/api/v1/documents/ingest", files={"file": (pdf.name, stream, "application/pdf")})
        assert response.status_code == 202
        accepted = response.json()
        document_id = accepted["document_id"]
        # Never delete a pre-existing deduplicated document in a shared environment.
        cleanup = not accepted["deduplicated"]
        try:
            for _ in range(120):
                job = client.get(f"/api/v1/jobs/{accepted['job_id']}").json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(1)
            assert job["status"] == "completed", job
            answer = client.post("/api/v1/chat/query", json={
                "question": "¿A qué temperatura dispara el sensor ST-104?", "document_ids": [document_id],
            })
            assert answer.status_code == 200
            payload = answer.json()
            assert payload["citations"]
            assert payload["primary_visual"]
            assert client.get(payload["primary_visual"]["image_url"]).status_code == 200
            assert client.get(payload["primary_visual"]["page_image_url"]).status_code == 200
        finally:
            if cleanup:
                assert client.delete(f"/api/v1/documents/{document_id}").status_code == 204
