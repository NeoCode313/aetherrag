"""Security and publication regressions spanning the real HTTP composition root."""
from pathlib import Path
import io

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.domain.entities import Chunk, ChunkKind, BoundingBox, SparseVector
from app.domain.exceptions import ValidationError, ConflictError
from app.infrastructure.config import Settings
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.vector_store.qdrant_adapter import QdrantHybridVectorStore
from app.main import create_app
from app.presentation.dependencies import AppContainer, get_container


def container_for(tmp_path, **kwargs):
    defaults = dict(environment="test", data_dir=tmp_path / "data", llm_provider="local",
                    embedding_provider="local", vector_backend="memory", auto_seed_sample_manual=False,
                    auth_enabled=False)
    defaults.update(kwargs)
    return AppContainer(Settings(**defaults))


def client_for(container):
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


@pytest.mark.parametrize("identifier", [r"Z:\example-workspace", r"..\state", "../state", "/etc", r"\\server\share", "a:b"])
def test_storage_rejects_cross_platform_paths(tmp_path, identifier):
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(ValidationError):
        storage.asset_path(identifier, "README.md")


def test_read_paths_never_create_directories(tmp_path):
    storage = LocalFileStorage(tmp_path)
    assert not storage.asset_path("run_missing", "figure.png").exists()
    assert not storage.page_image_path("run_missing", 1).exists()
    assert not (tmp_path / "assets" / "run_missing").exists()
    assert not (tmp_path / "pages" / "run_missing").exists()


def test_upload_stream_limits_before_commit_and_removes_temporary_file(tmp_path):
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(ValidationError):
        storage.save_stream(io.BytesIO(b"%PDF-" + b"x" * 1000), 64)
    assert list(storage.uploads_dir.iterdir()) == []


def test_dimension_mismatch_preserves_collection_and_unpublished_runs_are_hidden():
    store = QdrantHybridVectorStore(in_memory=True)
    store.ensure_collection(2)
    chunk = Chunk("c1", "doc_test", "manual.pdf", 1, ChunkKind.TEXT, "Pressure 14 bar",
                  BoundingBox(0, 0, 1, 1))
    writer = store.view(store.collection_name, run_id="run_staging")
    writer.upsert([chunk], [[1.0, 0.0]], [SparseVector([1], [1.0])])
    with pytest.raises(ConflictError):
        store.ensure_collection(3)
    assert store.count_points() == 1
    assert store.view(store.collection_name, active_run_ids=[]).dense_search([1, 0], 5) == []
    assert len(store.view(store.collection_name, active_run_ids=["run_staging"]).dense_search([1, 0], 5)) == 1
    store.client.close()


def test_sessions_protect_queries_assets_and_mutations_with_csrf(tmp_path):
    container = container_for(
        tmp_path, auth_enabled=True, session_secure=False, session_secret="s" * 40,
        operator_password_hash=PasswordHasher(time_cost=1, memory_cost=8192).hash("test-password"))
    with client_for(container) as client:
        assert client.get("/api/v1/documents").status_code == 401
        assert client.get("/api/v1/assets/run_x/figure.png").status_code == 401
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        result = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-password"})
        assert result.status_code == 200
        assert "HttpOnly" in result.headers["set-cookie"]
        csrf = result.json()["csrf_token"]
        assert client.get("/api/v1/documents").status_code == 200
        assert client.post("/api/v1/chat/query", json={"question": "What pressure?"}).status_code == 403
        answer = client.post("/api/v1/chat/query", json={"question": "What pressure?"},
                             headers={"X-CSRF-Token": csrf})
        assert answer.status_code == 200
        assert answer.json()["abstained"]
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/v1/documents").status_code == 401


def test_unpublished_asset_and_failed_dependency_are_not_reported_as_ready(tmp_path, monkeypatch):
    container = container_for(tmp_path)
    with client_for(container) as client:
        assert client.get("/api/v1/assets/run_notpublished/sensitive.png").status_code == 404
        monkeypatch.setattr(container.vector_store, "healthy", lambda: False)
        response = client.get("/api/v1/system/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"
        assert client.get("/api/v1/system/live").status_code == 200


def test_provider_switch_does_not_mutate_embedding_space_or_persist_secrets(tmp_path):
    container = container_for(tmp_path)
    generation = container.catalog.active_generation()["generation_id"]
    container.update_provider("local", reingest_sample=True)
    assert container.catalog.active_generation()["generation_id"] == generation
    assert container.catalog.get_setting("llm_profile")["provider"] == "local"
    with pytest.raises(ValidationError):
        container.update_provider("local", openai_api_key="do-not-store")
    assert not (tmp_path / "data/state/provider_config.json").exists()
    container.close()

