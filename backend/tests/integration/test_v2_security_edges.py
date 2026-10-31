"""Boundary tests for untrusted auth input and pre-parser streaming body limits."""

import asyncio
from collections import defaultdict

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.infrastructure.config import Settings
from app.infrastructure.http_limits import RequestBodyLimitMiddleware
from app.main import create_app
from app.presentation import auth
from app.presentation.dependencies import AppContainer, get_container


def test_non_ascii_login_and_csrf_are_rejections_not_server_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_attempts", defaultdict(list))
    settings = Settings(environment="test", data_dir=tmp_path, llm_provider="local", embedding_provider="local",
                        vector_backend="memory", auto_seed_sample_manual=False, auth_enabled=True,
                        session_secret="s" * 40, session_secure=False,
                        operator_password_hash=PasswordHasher(time_cost=1, memory_cost=8192).hash("test-password"))
    container = AppContainer(settings)
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    with TestClient(app) as client:
        result = client.post("/api/v1/auth/login", json={"username": "administradór", "password": "test-password"})
        assert result.status_code == 401
        good = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-password"})
        assert good.status_code == 200
        cookie = client.cookies.get("aetherrag_session")
        scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/query", "app": app,
                 "headers": [(b"cookie", ("aetherrag_session=" + cookie).encode()), (b"x-csrf-token", b"\xe9")]}
        with pytest.raises(Exception) as rejected:
            auth.session(Request(scope), mutate=True)
        assert rejected.value.status_code == 403
        # A signed but structurally invalid legacy session fails closed.
        malformed = auth.serializer(settings).dumps(["not", "an", "object"])
        client.cookies.set("aetherrag_session", malformed)
        assert client.get("/api/v1/auth/session").status_code == 401


def _asgi_request(headers, chunks, limit=8):
    observed = {"called": False, "completed": False, "forwarded": [], "received": 0, "sent": []}
    async def endpoint(scope, receive, send):
        observed["called"] = True
        while True:
            message = await receive()
            observed["forwarded"].append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        observed["completed"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    remaining = list(chunks)
    async def receive():
        observed["received"] += 1
        return remaining.pop(0) if remaining else {"type": "http.disconnect"}
    async def send(message):
        observed["sent"].append(message)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/documents/ingest", "headers": headers}
    asyncio.run(RequestBodyLimitMiddleware(endpoint, max_body_bytes=limit)(scope, receive, send))
    return observed


@pytest.mark.parametrize("headers,status", [
    ([(b"content-length", b"9")], 413),
    ([(b"content-length", b"-1")], 400),
    ([(b"content-length", b"invalid")], 400),
    ([(b"content-length", b"1, 2")], 400),
    ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
    ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")], 400),
    ([(b"content-length", b"9" * 10000)], 400),
])
def test_body_limit_rejects_headers_before_reading_or_calling_endpoint(headers, status):
    result = _asgi_request(headers, [])
    assert not result["called"] and result["received"] == 0
    assert result["sent"][0]["status"] == status


@pytest.mark.parametrize("headers", [[], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")]])
def test_incremental_limit_stops_chunked_or_false_lengths_before_handler_completion(headers):
    result = _asgi_request(headers, [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"6789", "more_body": False},
    ])
    assert result["sent"][0]["status"] == 413
    assert result["received"] == 2
    assert result["forwarded"] == [b"12345"]
    assert not result["completed"]


def test_exact_body_limit_passes_chunks_without_assembling_them():
    result = _asgi_request([(b"content-length", b"8")], [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
    ])
    assert result["sent"][0]["status"] == 200
    assert result["completed"] and result["forwarded"] == [b"1234", b"5678"]


def test_body_cap_precedes_multipart_and_chunked_json_endpoint_side_effects():
    app = FastAPI()
    submitted = []
    @app.post("/upload")
    async def upload(file: UploadFile = File(...)):
        submitted.append("upload")
        return {"ok": True}
    @app.post("/json")
    async def parse_json(payload: dict):
        submitted.append("json")
        return payload
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=256)
    with TestClient(app) as client:
        assert client.post("/upload", files={"file": ("manual.pdf", b"%PDF-" + b"a" * 1000, "application/pdf")}).status_code == 413
        chunks = iter([b'{"payload":"', *([b"x" * 32] * 12), b'"}'])
        response = client.post("/json", content=chunks, headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert submitted == []
