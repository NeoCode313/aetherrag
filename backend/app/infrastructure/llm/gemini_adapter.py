"""Google Gemini adapters using the current ``google-genai`` SDK (>= 2.30.1).

Uses:
* ``client.models.generate_content`` with ``gemini-3.8-flash`` for multimodal RAG generation
  and structured JSON output.
* ``client.models.embed_content`` with ``gemini-embedding-001`` for dense embeddings.
* Wrapped in ``ResilientExecutor`` (Exponential Backoff + Jitter + Circuit Breaker).
"""

from __future__ import annotations

import base64
import logging
import math
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import errors, types

from app.domain.entities import GeneratedAnswer, ImageInput
from app.domain.exceptions import (
    ProviderError,
    RateLimitError,
    TransientProviderError,
)
from app.domain.ports import EmbeddingProvider, LLMProvider
from app.infrastructure.llm.schemas import RAG_ANSWER_JSON_SCHEMA, parse_structured_answer, portable_output_schema
from app.infrastructure.resilience import ResilientExecutor

logger = logging.getLogger(__name__)


def _translate_gemini_error(exc: Exception) -> Exception:
    """Map ``google.genai.errors.APIError`` and transport errors into domain exceptions."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", 0) or 0
        if code == 429:
            return RateLimitError("Gemini rate limit exceeded (HTTP 429)")
        if code in {500, 502, 503, 504}:
            return TransientProviderError(f"Gemini upstream server error (HTTP {code})")
        return ProviderError(f"Gemini API error (HTTP {code})")
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg or "reset" in msg:
        return TransientProviderError("Gemini network/timeout error")
    return ProviderError("Gemini returned an invalid or unsupported response")


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    if not vec or any(not math.isfinite(v) for v in vec):
        raise ProviderError("Gemini devolvió un vector vacío o no finito.")
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        raise ProviderError("Gemini devolvió un vector nulo.")
    return [float(v) / norm for v in vec]


class GeminiLLMProvider(LLMProvider):
    """Multimodal LLM adapter powered by Gemini GenerateContent API (``gemini-3.8-flash``)."""

    def __init__(
        self,
        api_key: str,
        executor: ResilientExecutor,
        model: str = "gemini-3.8-flash",
        max_output_tokens: int = 2400,
        timeout: float = 45.0,
        supports_images: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiLLMProvider")
        self._api_key = api_key
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=int(max(1.0, min(timeout, 120.0)) * 1000)))
        self._executor = executor
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._supports_images = supports_images

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_images(self) -> bool:
        return self._supports_images

    @property
    def supports_structured_output(self) -> bool:
        return True

    def generate_answer(
        self,
        system_instruction: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
    ) -> GeneratedAnswer:
        if images and not self.supports_images:
            raise ProviderError("El perfil Gemini no tiene visión habilitada.")
        parts = [types.Part.from_text(text=prompt)]
        for img in images:
            parts.extend([types.Part.from_text(text="Imagen " + img.alias),
                          types.Part.from_bytes(data=img.data, mime_type=img.mime_type)])

        def _invoke() -> GeneratedAnswer:
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction, response_mime_type="application/json",
                        response_json_schema=portable_output_schema(prompt, images), max_output_tokens=self._max_output_tokens))
                usage = getattr(response, "usage_metadata", None)
                return parse_structured_answer(response.text or "",
                    input_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None), generated_mode="grounded_answer")
            except Exception as exc:
                raise _translate_gemini_error(exc) from exc

        return self._executor.call(_invoke)

    def describe_image(self, image: ImageInput, context: str) -> str:
        if not self.supports_images:
            raise ProviderError("El perfil Gemini no tiene visión habilitada.")
        def _invoke() -> str:
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[types.Content(role="user", parts=[
                        types.Part.from_text(text="Describe este diagrama técnico. Imagen " + image.alias + "; contexto: " + context),
                        types.Part.from_bytes(data=image.data, mime_type=image.mime_type)])],
                    config=types.GenerateContentConfig(max_output_tokens=250))
                return (response.text or "").strip()
            except Exception as exc:
                raise _translate_gemini_error(exc) from exc

        return self._executor.call(_invoke)


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Dense embedding adapter using ``gemini-embedding-001`` via ``client.models.embed_content``."""

    def __init__(
        self,
        api_key: str,
        executor: ResilientExecutor,
        model: str = "gemini-embedding-001",
        dimension: int = 768,
        batch_size: int = 32,
        timeout: float = 45.0,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingProvider")
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=int(max(1.0, min(timeout, 120.0)) * 1000)))
        self._executor = executor
        self._model = model
        self._dimension = dimension
        self._batch_size = batch_size

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])

            def _invoke(items: list[str] = batch) -> list[list[float]]:
                try:
                    response = self._client.models.embed_content(
                        model=self._model,
                        contents=items,
                        config=types.EmbedContentConfig(
                            task_type="RETRIEVAL_DOCUMENT",
                            output_dimensionality=self._dimension,
                        ),
                    )
                    vectors = [_l2_normalize(emb.values) for emb in (response.embeddings or [])]
                    if len(vectors) != len(items) or any(len(v) != self._dimension for v in vectors):
                        raise ProviderError("Gemini devolvió embeddings incompletos o de dimensión incompatible.")
                    return vectors
                except Exception as exc:
                    raise _translate_gemini_error(exc) from exc

            all_vectors.extend(self._executor.call(_invoke))
        return all_vectors

    def embed_query(self, text: str) -> list[float]:
        def _invoke() -> list[float]:
            try:
                response = self._client.models.embed_content(
                    model=self._model,
                    contents=text,
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_QUERY",
                        output_dimensionality=self._dimension,
                    ),
                )
                embeddings = response.embeddings or []
                if not embeddings:
                    raise ProviderError("Gemini returned empty embedding list.")
                vector = _l2_normalize(embeddings[0].values)
                if len(vector) != self._dimension:
                    raise ProviderError("Gemini devolvió una dimensión incompatible.")
                return vector
            except Exception as exc:
                raise _translate_gemini_error(exc) from exc

        return self._executor.call(_invoke)
