"""Opt-in HTTPS/API -> PostgreSQL outbox -> Redis/Celery -> remote Qdrant smoke.

Requires STACK_TEST_URL and STACK_TEST_PASSWORD for the dedicated test deployment.
The test uploads a newly generated small manual and deletes only that document.
It uses local extractive models; it does not claim external multimodal-model quality.
"""
import os
import time
import unicodedata
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
            cases = [
                ("¿Qué significa el código de falla E-409 en el acumulador AC-301, qué componentes intervienen en la Figura 3 y cuál es la acción correctiva?",
                 "3", ("115", "140", "ac-301", "v-12b")),
                ("¿Cuáles son los componentes numerados del corte de la turbina en la Figura 1 y cuál es su potencia y salto nominal en la Tabla 1?",
                 "1", ("145", "182", "camara", "alabes", "rodete", "eje", "cojinete", "tubo")),
            ]
            for question, figure, expected in cases:
                response = client.post("/api/v1/chat/query", json={
                    "question": question, "document_ids": [document_id],
                })
                assert response.status_code == 200
                answer = response.json()
                normalized = "".join(char for char in unicodedata.normalize("NFKD", answer["answer"].lower())
                                     if not unicodedata.combining(char))
                assert not answer["abstained"]
                assert all(value in normalized for value in expected)
                assert answer["citations"] and answer["primary_visual"]
                assert str(answer["primary_visual"]["figure_number"]) == figure
                assert client.get(answer["primary_visual"]["image_url"]).status_code == 200
                assert client.get(answer["primary_visual"]["page_image_url"]).status_code == 200
        finally:
            if cleanup:
                assert client.delete(f"/api/v1/documents/{document_id}").status_code == 204


def test_real_stack_serves_compiled_react_and_same_origin_assets():
    """The container must deliver the built client, not just a healthy API."""
    import hashlib
    from html.parser import HTMLParser
    from urllib.parse import parse_qs, urlsplit

    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []
            self.inline = False
            self.in_script = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "script":
                self.in_script = True
                if attrs.get("src"):
                    self.urls.append(attrs["src"])
            if tag == "link" and attrs.get("rel") == "stylesheet":
                self.urls.append(attrs["href"])

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False

        def handle_data(self, text):
            if self.in_script and text.strip():
                self.inline = True

    with httpx.Client(base_url=URL, verify=os.getenv("STACK_TEST_CA") or False, timeout=45) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="root"' in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assets = Assets()
        assets.feed(page.text)
        assert not assets.inline
        assert any(urlsplit(url).path == "/static/app.js" for url in assets.urls)
        assert any(urlsplit(url).path == "/static/styles.css" for url in assets.urls)
        for url in assets.urls:
            assert url.startswith("/static/")
            asset = client.get(url)
            assert asset.status_code == 200, url
            version = parse_qs(urlsplit(url).query).get("v")
            if version:
                assert hashlib.sha256(asset.content).hexdigest()[:12] == version[0]
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "/static/api-docs-init.js" in docs.text
