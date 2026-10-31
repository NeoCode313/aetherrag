"""Regressions for Groq schema support, private configuration and working docs."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from dotenv import dotenv_values

from app.domain.exceptions import ProviderContractError, ProviderError, ValidationError
from app.infrastructure.config import Settings
from app.infrastructure.llm.openai_adapter import OpenAILLMProvider
from app.infrastructure.llm.schemas import parse_structured_answer
from app.infrastructure.providers import make_llm
from app.infrastructure.resilience import CircuitBreaker, ResilientExecutor


def payload():
    return dict(answer="",abstained=True,abstention_reason="No evidence",
                evidence_coverage="none",cited_ids=[],used_visual_ids=[],
                answer_mode="grounded_answer",claims=[],missing_facets=[])


def executor():
    return ResilientExecutor(CircuitBreaker("regression"),max_attempts=1)


@pytest.mark.parametrize("model",["qwen/qwen3.8-27b","openai/gpt-oss-120b","openai/gpt-oss-20b"])
def test_groq_uses_strict_schema_for_supported_models(model):
    with patch("httpx.Client") as http:
        client=http.return_value.__enter__.return_value
        response=MagicMock()
        response.json.return_value={"choices":[{"message":{"content":json.dumps(payload())}}]}
        client.post.return_value=response
        llm=OpenAILLMProvider(("gsk_" + "x" * 32),model,executor(),base_url="https://api.groq.com/openai/v1")
        assert llm.generate_answer("s","p").abstained
        request=client.post.call_args.kwargs["json"]
        assert request["response_format"]["type"]=="json_schema"
        assert request["response_format"]["json_schema"]["strict"] is True
        assert client.post.call_count==1


@pytest.mark.parametrize("second_valid",[True,False])
def test_legacy_groq_json_mode_regenerates_once_without_accepting_invalid_output(second_valid):
    with patch("httpx.Client") as http:
        client=http.return_value.__enter__.return_value
        def response(value):
            result=MagicMock()
            result.json.return_value={"choices":[{"message":{"content":json.dumps(value)}}]}
            return result
        client.post.side_effect=[response({"answer":"private response should not be echoed"}),
                                  response(payload() if second_valid else {})]
        llm=OpenAILLMProvider(("gsk_" + "x" * 32),"llama-3.3-70b-versatile",executor(),base_url="https://api.groq.com/openai/v1")
        if second_valid:
            assert llm.generate_answer("s","p").abstained
        else:
            with pytest.raises(ProviderContractError):
                llm.generate_answer("s","p")
        assert client.post.call_count==2
        assert client.post.call_args.kwargs["json"]["response_format"]["type"]=="json_object"
        assert "private response should not be echoed" not in str(client.post.call_args.kwargs["json"])


def test_contract_error_reports_fields_without_model_text_or_extra_key_names():
    with pytest.raises(ProviderContractError) as error:
        parse_structured_answer(json.dumps({"confidential-extra-key":"confidential-content"}))
    assert "cited_ids" in str(error.value)
    assert "confidential" not in str(error.value)


def test_groq_credentials_never_select_an_unrelated_endpoint_or_silently_choose_local():
    settings=Settings(_env_file=None,llm_provider="auto",groq_api_key=("gsk_" + "x" * 32),gemini_api_key="",openai_api_key="",
                      openai_base_url="https://api.groq.com/openai/v1",openai_model="qwen/qwen3.8-27b")
    name,llm=make_llm(settings,executor())
    assert name=="openai" and llm.model_name=="groq/qwen/qwen3.8-27b"
    settings.openai_base_url="https://api.openai.com/v1"
    with pytest.raises(ValidationError,match="Groq"):
        make_llm(settings,executor())
    # A key prefix must not misclassify an unrelated endpoint as Groq.
    assert not OpenAILLMProvider("gsk_fake","m",executor())._is_groq


def container_settings(tmp_path):
    return Settings(_env_file=None,environment="test",data_dir=tmp_path,llm_provider="local",gemini_api_key="",openai_api_key="",
                    embedding_provider="local",vector_backend="memory",auto_seed_sample_manual=False,
                    database_url="",queue_backend="background",auth_enabled=False,session_secret="",
                    operator_password_hash="",qdrant_api_key="",redis_url="",
                    groq_api_key=("gsk_" + "x" * 32),openai_base_url="https://api.groq.com/openai/v1",
                    openai_model="qwen/qwen3.8-27b")


def test_selected_provider_and_raw_model_survive_restart(tmp_path):
    from app.presentation.dependencies import AppContainer
    settings=container_settings(tmp_path)
    first=AppContainer(settings)
    try:
        first.update_provider("openai")
        assert first.catalog.get_setting("llm_profile")["model"]=="qwen/qwen3.8-27b"
    finally:
        first.close()
    second=AppContainer(settings.model_copy())
    try:
        assert second.active_provider_name=="openai"
        assert second.llm_provider.model_name=="groq/qwen/qwen3.8-27b"
        assert second.query_use_case.llm.model_name==second.llm_provider.model_name
    finally:
        second.close()


def test_failed_development_change_does_not_mutate_settings(tmp_path):
    from app.presentation.dependencies import AppContainer
    settings=container_settings(tmp_path)
    settings.environment="development"
    container=AppContainer(settings)
    try:
        before=container.settings.model_dump()
        with pytest.raises(ValidationError):
            container.update_provider("gemini",gemini_api_key="synthetic-new-key",openai_base_url="https://not-allowed.invalid")
        assert container.settings.model_dump()==before
        assert container.catalog.get_setting("llm_profile") is None
        assert container.active_provider_name=="local"
    finally:
        container.close()


def test_production_style_change_accepts_model_only_and_rejects_browser_secrets(tmp_path):
    from app.presentation.dependencies import AppContainer
    container=AppContainer(container_settings(tmp_path))
    try:
        with pytest.raises(ValidationError,match="servidor"):
            container.update_provider("openai",openai_api_key="not-accepted-from-browser")
        container.update_provider("openai",openai_model="openai/gpt-oss-120b")
        assert container.llm_provider.model_name=="groq/openai/gpt-oss-120b"
    finally:
        container.close()


def test_private_groq_setup_preserves_other_credentials(tmp_path):
    from scripts.configure_groq import configure
    path=tmp_path/".env"
    path.write_text("DB_PASSWORD='test-only-db'\nSESSION_SECRET='test-only-session'\nEMBEDDING_PROVIDER='local'\nLLM_PROVIDER='local'\n",encoding="utf-8")
    configure(path,("gsk_" + "x" * 32),"qwen/qwen3.8-27b")
    values=dotenv_values(path)
    assert values["DB_PASSWORD"]=="test-only-db" and values["SESSION_SECRET"]=="test-only-session"
    assert values["EMBEDDING_PROVIDER"]=="local"
    assert values["LLM_PROVIDER"]=="openai" and values["GROQ_API_KEY"]==("gsk_" + "x" * 32)
    assert values["OPENAI_BASE_URL"]=="https://api.groq.com/openai/v1"


def test_docs_assets_are_local_and_compatible_with_strict_script_policy():
    from app.main import create_app
    client=TestClient(create_app())
    response=client.get("/docs")
    assert response.status_code==200
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    assert "cdn." not in response.text and "SwaggerUIBundle(" not in response.text
    assert "/static/api-docs-init.js" in response.text
    assert client.get("/static/api-docs-init.js").status_code==200
    assert client.get("/static/vendor/swagger-ui-bundle-5.34.1.js").status_code==200
    assert client.get("/openapi.json").status_code==200
