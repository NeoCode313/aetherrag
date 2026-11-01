"""Real owner/session, private provider persistence and legacy index recovery regressions."""
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.domain.exceptions import ConflictError, ProviderContractError, ValidationError
from app.infrastructure.catalog import Catalog
from app.infrastructure.config import Settings
from app.infrastructure.installation_state import InstallationState
from app.infrastructure.providers import embedding_profile, digest
from app.infrastructure.sample_manual import build_sample_manual_pdf
from app.infrastructure.storage import LocalFileStorage
from app.main import create_app
from app.presentation.dependencies import AppContainer, get_container


def settings(path, **overrides):
    values = dict(_env_file=None, environment="test", data_dir=path, llm_provider="local",
        embedding_provider="local", vector_backend="memory", queue_backend="background",
        database_url="", auto_seed_sample_manual=False, auth_enabled=True, session_secure=False,
        gemini_api_key="", openai_api_key="", groq_api_key="", anthropic_api_key="",
        operator_password_hash="", session_secret="", dispatcher_interval_seconds=.05)
    values.update(overrides)
    return Settings(**values)


def client_for(container):
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def login(client, username="reviewer", password="Unique-Owner-Password-48291"):
    from app.presentation.auth import _attempts
    _attempts.clear()
    result = client.post("/api/v1/auth/login", json=dict(username=username, password=password))
    assert result.status_code == 200, result.json()
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]


def register(client, container):
    return client.post("/api/v1/auth/register", json=dict(username="reviewer",
        password="Unique-Owner-Password-48291", setup_token=container.identity.read()["setup_token"]))


def test_first_owner_login_demo_ingestion_query_logout_and_restart(tmp_path):
    container = AppContainer(settings(tmp_path))
    with client_for(container) as client:
        assert client.get("/api/v1/auth/setup").json()["registration_required"]
        assert "setup_token" not in client.get("/api/v1/auth/setup").text
        assert client.get("/api/v1/documents").status_code == 401
        assert register(client, container).status_code == 201
        assert "setup_token" not in container.identity.read()
        assert register_again(client).status_code == 409
        login(client)
        assert client.post("/api/v1/system/test-provider").status_code == 200
        queued = client.post("/api/v1/documents/seed-sample")
        assert queued.status_code == 202, queued.text
        for _ in range(150):
            job = client.get("/api/v1/jobs/" + queued.json()["job_id"]).json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(.05)
        assert job["status"] == "completed", job
        result = client.post("/api/v1/chat/query", json={"question": "¿A qué temperatura dispara el sensor ST-104?"})
        assert result.status_code == 200, result.text
        assert result.json()["citations"] and result.json()["primary_visual"]
        assert client.get(result.json()["primary_visual"]["image_url"]).status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.get("/api/v1/documents").status_code == 401
    restored = AppContainer(settings(tmp_path))
    try:
        with client_for(restored) as client:
            assert not client.get("/api/v1/auth/setup").json()["registration_required"]
            login(client)
            assert len(client.get("/api/v1/documents").json()) == 1
    finally:
        restored.close()


def register_again(client):
    return client.post("/api/v1/auth/register", json=dict(username="second",
        password="Another-Owner-Password-48291", setup_token="invalid-code-long-enough"))


def test_untrusted_registration_does_not_hash_or_claim_installation(tmp_path):
    container = AppContainer(settings(tmp_path))
    with client_for(container) as client:
        with patch("app.presentation.auth.PasswordHasher") as hasher:
            assert register_again(client).status_code == 422
            hasher.assert_not_called()
        assert register(client, container).status_code == 201
        login(client)
        client.headers.pop("X-CSRF-Token")
        assert client.post("/api/v1/system/provider", json={"llm_provider": "local"}).status_code == 403


def test_registration_atomic_only_one_owner(tmp_path):
    store = InstallationState(tmp_path)
    token = store.read()["setup_token"]
    def attempt(index):
        try:
            store.register("owner"+str(index), "synthetic-hash", token)
            return True
        except ConflictError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1


@pytest.mark.parametrize("choice,fields,secret", [
    ("anthropic", {"anthropic_model": "claude-test", "anthropic_api_key": "synthetic-anthropic-key"}, "synthetic-anthropic-key"),
    ("gemini", {"gemini_model": "gemini-test", "gemini_api_key": "synthetic-gemini-key"}, "synthetic-gemini-key"),
    ("openai", {"openai_model": "local-test", "openai_base_url": "http://localhost:1234/v1"}, ""),
    ("openai", {"openai_model": "xai-test", "openai_base_url": "https://api.x.ai/v1", "openai_api_key": "synthetic-xai-key"}, "synthetic-xai-key"),
    ("ollama", {"ollama_model": "local-test", "ollama_base_url": "http://host.docker.internal:11434"}, ""),
])
def test_authenticated_provider_selection_persists_without_exposing_secrets(tmp_path, choice, fields, secret):
    original = settings(tmp_path)
    container = AppContainer(original.model_copy(deep=True))
    generation = container.configured_generation["generation_id"]
    with client_for(container) as client:
        request = {"llm_provider": choice, **fields}
        assert client.post("/api/v1/system/provider", json=request).status_code == 401
        assert register(client, container).status_code == 201
        login(client)
        response = client.post("/api/v1/system/provider", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["llm_provider"] == choice
        if secret:
            assert secret not in response.text
            container.catalog.set_maintenance(admissions_paused=True, activations_paused=True, dispatch_paused=True)
            try:
                assert secret not in json.dumps(container.catalog.export_snapshot())
            finally:
                container.catalog.set_maintenance(admissions_paused=False, activations_paused=False, dispatch_paused=False)
    restored = AppContainer(original.model_copy(deep=True))
    try:
        assert restored.active_provider_name == choice
        assert restored.configured_generation["generation_id"] == generation
        assert restored.llm_provider.model_name.endswith(fields.get(choice+"_model", ""))
    finally:
        restored.close()


def legacy_container(path, missing=False):
    config = settings(path, auth_enabled=False)
    storage = LocalFileStorage(path)
    old = Catalog("sqlite:///" + str(path / "catalog-test.sqlite"), allow_sqlite=True, pipeline_version="layout-v2")
    profile = embedding_profile(config.model_copy(update={"pipeline_version": "layout-v2"}))
    profile.pop("pipeline_config")
    generation = old.ensure_generation(digest(profile), "legacy_collection", profile)
    pdf = build_sample_manual_pdf(path / "legacy.pdf")
    with pdf.open("rb") as stream:
        source_id, sha, size = storage.save_stream(stream, 10_000_000)
    job, _ = old.submit(pdf.name, sha, size, source_id, generation["generation_id"], generation["profile_id"])
    old.close()
    if missing:
        storage.upload_path(source_id).unlink()
    return AppContainer(config), job.document_id, generation["generation_id"]


@pytest.mark.parametrize("missing", [False, True])
def test_legacy_recovery_rebuilds_without_relabelling_or_losing_sources(tmp_path, missing):
    from app.application.services.index_recovery import start_recovery
    container, document_id, old_generation = legacy_container(tmp_path, missing)
    try:
        assert not container.index_state()["compatible"]
        uploads_before = set(container.storage.uploads_dir.glob("*.pdf"))
        with pytest.raises(ConflictError, match="recuperación"):
            container.ingest_use_case.submit_stream("new.pdf", io.BytesIO(b"should never be read"))
        assert set(container.storage.uploads_dir.glob("*.pdf")) == uploads_before
        assert start_recovery(container)["status"] == "running"
        container._recovery_thread.join(timeout=35)
        assert not container._recovery_thread.is_alive()
        state = container.catalog.get_setting("index_recovery")
        assert state["status"] == ("failed" if missing else "completed"), state
        assert container.index_state()["compatible"] is not missing
        assert {d.document_id for d in container.doc_repo.list()} == {document_id}
        assert container.catalog.generation_by_id(old_generation)
        assert not container.catalog.maintenance()["admissions_paused"]
        assert list((container.identity.directory / "backups").glob("*.sqlite"))
        assert all(path.exists() for path in uploads_before)
        if not missing:
            answer = container.query_use_case.execute("¿A qué temperatura dispara el sensor ST-104?")
            assert answer.citations
    finally:
        container.close()


def test_endpoint_change_does_not_forward_old_cloud_key(tmp_path):
    container = AppContainer(settings(tmp_path))
    try:
        container.update_provider("openai", openai_api_key="synthetic-private-key", openai_model="m")
        container.update_provider("openai", openai_base_url="http://localhost:1234/v1", openai_model="m")
        assert not container.settings.openai_api_key
        assert not container.settings.groq_api_key
        with pytest.raises(ValidationError):
            container.update_provider("openai", openai_base_url="https://169.254.169.254/v1", approve_endpoint=True)
    finally:
        container.close()


def test_anthropic_uses_native_structured_text_and_rejects_invalid_output():
    from app.infrastructure.llm.anthropic_adapter import AnthropicLLMProvider
    from app.infrastructure.resilience import CircuitBreaker, ResilientExecutor
    executor = ResilientExecutor(CircuitBreaker("test-anthropic"), max_attempts=1)
    answer = dict(answer="", abstained=True, abstention_reason="No evidence", evidence_coverage="none",
        cited_ids=[], used_visual_ids=[], answer_mode="grounded_answer", claims=[], missing_facets=[])
    with patch("httpx.Client") as http:
        response = MagicMock()
        response.json.return_value = {"content": [{"type": "text", "text": json.dumps(answer)}]}
        client = http.return_value.__enter__.return_value
        client.post.return_value = response
        llm = AnthropicLLMProvider("synthetic-key", "claude-test", executor)
        assert llm.generate_answer("s", "p").abstained
        request = client.post.call_args.kwargs["json"]
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert "tool_choice" not in request
        response.json.return_value = {"content": [{"type": "text", "text": "private invalid text"}]}
        with pytest.raises(ProviderContractError) as error:
            llm.generate_answer("s", "p")
        assert "private invalid text" not in str(error.value)


def test_missing_provider_key_keeps_login_and_configuration_available(tmp_path):
    container = AppContainer(settings(tmp_path))
    container.catalog.set_setting("llm_profile", {"provider": "gemini", "model": "gemini-test"})
    container.close()
    restored = AppContainer(settings(tmp_path))
    with client_for(restored) as client:
        assert register(client, restored).status_code == 201
        login(client)
        status = client.get("/api/v1/system/status").json()
        assert not status["provider_ready"]
        assert status["llm_provider"] == "gemini"
        assert client.post("/api/v1/system/test-provider").status_code == 502
        assert client.post("/api/v1/system/provider", json={"llm_provider": "local"}).status_code == 200
        assert client.post("/api/v1/system/test-provider").status_code == 200


def test_two_processes_refresh_provider_without_changing_embeddings(tmp_path):
    original = settings(tmp_path, embedding_provider="auto")
    first = AppContainer(original.model_copy(deep=True))
    second = AppContainer(original.model_copy(deep=True))
    try:
        profile = first.configured_generation["profile_id"]
        first.update_provider("gemini", gemini_api_key="synthetic-key", gemini_model="gemini-test")
        second.refresh_private_settings()
        assert second.active_provider_name == "gemini"
        assert second.settings.gemini_api_key == "synthetic-key"
        assert second.configured_generation["profile_id"] == profile
    finally:
        first.close()
        second.close()
    third = AppContainer(original.model_copy(deep=True))
    try:
        assert third.configured_generation["profile_id"] == profile
    finally:
        third.close()


def test_rotated_installation_password_invalidates_prior_sessions(tmp_path):
    from argon2 import PasswordHasher
    container = AppContainer(settings(tmp_path))
    with client_for(container) as client:
        assert register(client, container).status_code == 201
        login(client)
        container.identity.rotate_password(PasswordHasher().hash("New-Owner-Password-48291"))
        assert client.get("/api/v1/auth/session").status_code == 401
        login(client, password="New-Owner-Password-48291")


def test_imported_environment_owner_accepts_explicit_password_rotation(tmp_path):
    from argon2 import PasswordHasher
    old_hash = PasswordHasher().hash("Old-Owner-Password-48291")
    new_hash = PasswordHasher().hash("New-Owner-Password-48291")
    first = InstallationState(tmp_path, "admin", old_hash, "o"*48)
    old_secret = first.read()["session_secret"]
    second = InstallationState(tmp_path, "admin", new_hash, "n"*48)
    assert second.read()["owner"]["password_hash"] == new_hash
    assert second.read()["session_secret"] != old_secret


@pytest.mark.parametrize("host,peer,origin,expected", [
    ("localhost", "127.0.0.1", "http://localhost", 201),
    ("localhost", "127.0.0.1", "https://attacker.invalid", 403),
    ("attacker.invalid", "127.0.0.1", "http://attacker.invalid", 422),
    ("localhost", "192.0.2.10", "http://localhost", 422),
])
def test_direct_local_registration_checks_client_host_and_origin(tmp_path, host, peer, origin, expected):
    container = AppContainer(settings(tmp_path))
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    with TestClient(app, base_url="http://localhost", client=(peer, 43210)) as client:
        result = client.post("/api/v1/auth/register", headers={"Origin": origin, "Host": host},
            json={"username": "owner", "password": "Private-Local-Password-48291"})
        assert result.status_code == expected, result.text
        assert "setup_token" not in client.get("/api/v1/auth/setup").text


def test_invalid_credentials_payload_does_not_echo_input(tmp_path):
    container = AppContainer(settings(tmp_path))
    with client_for(container) as client:
        result = client.post("/api/v1/auth/register", json={"username": "reviewer",
            "password": "sentinel-private-value", "setup_token": {"secret": "sentinel-private-value"}})
        assert result.status_code == 422
        assert "sentinel-private-value" not in result.text


def test_provider_quota_circuit_does_not_take_login_or_ingestion_offline(tmp_path):
    from app.domain.exceptions import RateLimitError
    container = AppContainer(settings(tmp_path, circuit_breaker_failure_threshold=1))
    with client_for(container) as client:
        container.llm_breaker.record_failure(RateLimitError("synthetic quota"))
        assert container.llm_breaker.state.value == "open"
        assert client.get("/api/v1/system/health").status_code == 200
        assert register(client, container).status_code == 201
        login(client)
        assert client.post("/api/v1/documents/seed-sample").status_code == 202
