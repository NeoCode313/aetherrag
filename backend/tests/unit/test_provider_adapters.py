"""Unit tests for external LLM and Embedding adapters (Gemini, Ollama, OpenAI) using Mocks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from google.genai import errors

from app.domain.entities import ImageInput
from app.domain.exceptions import ProviderError, RateLimitError, TransientProviderError
from app.infrastructure.llm.gemini_adapter import (
    GeminiEmbeddingProvider,
    GeminiLLMProvider,
    _translate_gemini_error,
)
from app.infrastructure.llm.ollama_adapter import (
    OllamaEmbeddingProvider,
    OllamaLLMProvider,
    _translate_http_error,
)
from app.infrastructure.llm.openai_adapter import (
    OpenAIEmbeddingProvider,
    OpenAILLMProvider,
)
from app.infrastructure.llm.schemas import parse_structured_answer
from app.infrastructure.resilience import CircuitBreaker, ResilientExecutor


def _fast_executor() -> ResilientExecutor:
    return ResilientExecutor(
        breaker=CircuitBreaker(name="test-adapter", failure_threshold=4, recovery_timeout=1.0),
        max_attempts=2,
        base_delay=0.005,
        max_delay=0.02,
    )


def test_gemini_llm_and_embedding_adapters_with_mock_client(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    payload = {
        "answer": "La presión nominal del sello N2 es **14.5 bar** [E1].",
        "cited_ids": ["E1"],
        "used_visual_ids": ["F1"],
        "abstained": False,
        "abstention_reason": "",
        "evidence_coverage": "full",
        "answer_mode": "grounded_answer",
        "claims": [{"statement": "La presión nominal es 14.5 bar.",
                    "evidence_refs": [{"evidence_id": "E1", "quote": "La presión nominal es 14.5 bar."}]}],
        "missing_facets": [],
    }
    mock_client.interactions.create.return_value = SimpleNamespace(
        output_text=json.dumps(payload),
        usage=SimpleNamespace(input_tokens=210, output_tokens=48),
    )
    mock_client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps(payload),
        usage_metadata=SimpleNamespace(prompt_token_count=210, candidates_token_count=48))
    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[3.0, 4.0, 0.0])]
    )

    monkeypatch.setattr("app.infrastructure.llm.gemini_adapter.genai.Client", lambda api_key, **kwargs: mock_client)

    llm = GeminiLLMProvider(api_key="test-gemini-key", executor=_fast_executor(), model="gemini-3.8-flash")
    assert llm.model_name == "gemini-3.8-flash"
    answer = llm.generate_answer(
        system_instruction="System",
        prompt="Prompt",
        images=[ImageInput(data=b"fake-jpeg", mime_type="image/jpeg", alias="F1")],
    )
    assert answer.abstained is False
    assert answer.cited_ids == ["E1"]
    assert answer.used_visual_ids == ["F1"]
    assert answer.input_tokens == 210

    desc = llm.describe_image(ImageInput(data=b"fake", mime_type="image/jpeg", alias="F1"), context="Page 2")
    assert "14.5 bar" in desc

    emb = GeminiEmbeddingProvider(
        api_key="test-gemini-key",
        executor=_fast_executor(),
        model="gemini-embedding-001",
        dimension=3,
    )
    assert emb.model_name == "gemini-embedding-001"
    assert emb.dimension == 3
    doc_vecs = emb.embed_documents(["chunk 1"])
    query_vec = emb.embed_query("query 1")
    assert len(doc_vecs) == 1
    assert pytest.approx(doc_vecs[0][0], rel=1e-4) == 0.6
    assert pytest.approx(query_vec[1], rel=1e-4) == 0.8


def test_gemini_error_translation_and_retry() -> None:
    err_429 = errors.APIError(429, {"error": {"message": "Quota exceeded"}})
    translated_429 = _translate_gemini_error(err_429)
    assert isinstance(translated_429, RateLimitError)

    err_503 = errors.APIError(503, {"error": {"message": "Service unavailable"}})
    translated_503 = _translate_gemini_error(err_503)
    assert isinstance(translated_503, TransientProviderError)


def test_ollama_and_openai_adapters_with_mock_httpx() -> None:
    json_answer = json.dumps(
        {
            "answer": "El par de apriete M24 es **680 Nm** [E1].",
            "cited_ids": ["E1"],
            "used_visual_ids": ["F1"],
            "abstained": False,
            "abstention_reason": "",
            "evidence_coverage": "full",
            "answer_mode": "grounded_answer",
            "claims": [{"statement": "El par de apriete M24 es 680 Nm.",
                        "evidence_refs": [{"evidence_id": "E1", "quote": "El par de apriete M24 es 680 Nm."}]}],
            "missing_facets": [],
        }
    )

    with patch("httpx.Client") as mock_http_cls:
        client_instance = MagicMock()
        mock_http_cls.return_value.__enter__.return_value = client_instance

        # Mock Ollama chat + embed responses
        chat_resp = MagicMock()
        chat_resp.raise_for_status.return_value = None
        chat_resp.json.return_value = {
            "message": {"content": json_answer},
            "prompt_eval_count": 100,
            "eval_count": 35,
        }
        embed_resp = MagicMock()
        embed_resp.raise_for_status.return_value = None
        embed_resp.json.return_value = {"embeddings": [[0.1, 0.2, 0.3, 0.4]]}

        client_instance.post.side_effect = [chat_resp, chat_resp, embed_resp]

        ollama_llm = OllamaLLMProvider("http://localhost:11434", "qwen2.5vl:7b", _fast_executor(), supports_images=True)
        assert ollama_llm.model_name == "ollama/qwen2.5vl:7b"
        ans = ollama_llm.generate_answer("sys", "user", images=[ImageInput(data=b"img", mime_type="image/png", alias="F1")])
        assert ans.cited_ids == ["E1"]
        assert "680 Nm" in ollama_llm.describe_image(ImageInput(data=b"img", mime_type="image/png", alias="F1"), "ctx")

        ollama_emb = OllamaEmbeddingProvider("http://localhost:11434", "nomic-embed-text", _fast_executor(), dimension=4)
        vec = ollama_emb.embed_query("par de apriete")
        assert len(vec) == 4

        # Mock OpenAI chat + embed responses
        oai_chat_resp = MagicMock()
        oai_chat_resp.raise_for_status.return_value = None
        oai_chat_resp.json.return_value = {
            "choices": [{"message": {"content": json_answer}}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 30},
        }
        oai_emb_resp = MagicMock()
        oai_emb_resp.raise_for_status.return_value = None
        oai_emb_resp.json.return_value = {"data": [{"index": 0, "embedding": [0.5, 0.5, 0.5, 0.5]}]}

        client_instance.post.side_effect = [oai_chat_resp, oai_chat_resp, oai_emb_resp]

        oai_llm = OpenAILLMProvider("sk-test", "gpt-4o-mini", _fast_executor(), supports_images=True)
        assert oai_llm.model_name == "openai/gpt-4o-mini"
        ans_oai = oai_llm.generate_answer("sys", "user", images=[ImageInput(data=b"img", mime_type="image/png", alias="F1")])
        assert "680 Nm" in ans_oai.answer
        assert "680 Nm" in oai_llm.describe_image(ImageInput(data=b"img", mime_type="image/png", alias="F1"), "ctx")

        oai_emb = OpenAIEmbeddingProvider("sk-test", "text-embedding-3-small", _fast_executor(), dimension=4)
        vec_oai = oai_emb.embed_query("M24")
        assert vec_oai == [0.5, 0.5, 0.5, 0.5]


def test_http_error_translation_and_fenced_json_parser() -> None:
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    resp_429 = httpx.Response(429, request=req, text="Too many requests")
    exc_429 = httpx.HTTPStatusError("429", request=req, response=resp_429)
    assert isinstance(_translate_http_error(exc_429, "Ollama"), RateLimitError)

    resp_502 = httpx.Response(502, request=req, text="Bad gateway")
    exc_502 = httpx.HTTPStatusError("502", request=req, response=resp_502)
    assert isinstance(_translate_http_error(exc_502, "Ollama"), TransientProviderError)

    structured = {
        "answer": "Respuesta", "abstained": False, "abstention_reason": "",
        "evidence_coverage": "partial", "cited_ids": ["E1"], "used_visual_ids": [],
        "answer_mode": "grounded_answer", "missing_facets": [],
        "claims": [{"statement": "Respuesta", "evidence_refs": [{"evidence_id": "E1", "quote": "Respuesta"}]}],
    }
    parsed = parse_structured_answer("```json\n" + json.dumps(structured) + "\n```")
    assert parsed.answer == "Respuesta"
    assert parsed.cited_ids == ["E1"]
    with pytest.raises(ProviderError):
        parse_structured_answer("Texto plano con [E2] y figura F1.")
