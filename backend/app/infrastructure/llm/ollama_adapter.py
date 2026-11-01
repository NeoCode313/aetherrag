"""Ollama local runtime adapters (``/api/chat`` and ``/api/embed``).

Demonstrates the Dependency Inversion Principle required by Section 1 of the spec:
switching from cloud Gemini/OpenAI to a local Ollama model requires zero changes in the
application use cases.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import httpx

from app.domain.entities import GeneratedAnswer, ImageInput
from app.domain.exceptions import ProviderError, RateLimitError, TransientProviderError
from app.domain.ports import EmbeddingProvider, LLMProvider
from app.infrastructure.llm.schemas import RAG_ANSWER_JSON_SCHEMA, parse_structured_answer
from app.infrastructure.resilience import ResilientExecutor


def _translate_http_error(exc: Exception, provider: str = "Ollama") -> Exception:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return RateLimitError(f"{provider}: límite de solicitudes o cuota (HTTP 429). Espera o cambia de proveedor/modelo; la carga de documentos sigue disponible.")
        if status >= 500:
            return TransientProviderError(f"{provider} server error (HTTP {status})")
        return ProviderError(f"{provider} HTTP {status}")
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
        return TransientProviderError(f"{provider} network/timeout error")
    return ProviderError(f"{provider} returned an invalid or unsupported response")


class OllamaLLMProvider(LLMProvider):
    """Local LLM adapter targeting Ollama's ``/api/chat`` endpoint with JSON schema support."""

    def __init__(
        self,
        base_url: str,
        model: str,
        executor: ResilientExecutor,
        timeout: float = 45.0,
        supports_images: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._executor = executor
        self._timeout = max(1.0, min(timeout, 120.0))
        self._supports_images = supports_images

    @property
    def model_name(self) -> str:
        return f"ollama/{self._model}"

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
            raise ProviderError("El perfil Ollama no tiene visión habilitada.")
        aliases = "\n".join(f"Imagen {index}: {image.alias}" for index, image in enumerate(images, 1))
        user_msg: dict[str, Any] = {"role": "user", "content": prompt + ("\n" + aliases if images else "")}
        if images:
            user_msg["images"] = [base64.b64encode(img.data).decode("utf-8") for img in images]

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_instruction},
                user_msg,
            ],
            "format": RAG_ANSWER_JSON_SCHEMA,
            "stream": False,
            "options": {"temperature": 0.1},
        }

        def _invoke() -> GeneratedAnswer:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    content = data.get("message", {}).get("content", "")
                    return parse_structured_answer(
                        content,
                        input_tokens=data.get("prompt_eval_count"),
                        output_tokens=data.get("eval_count"),
                    )
            except Exception as exc:
                raise _translate_http_error(exc, "Ollama") from exc

        return self._executor.call(_invoke)

    def describe_image(self, image: ImageInput, context: str) -> str:
        if not self.supports_images:
            raise ProviderError("El perfil Ollama no tiene visión habilitada.")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": f"Describe técnicamente la imagen {image.alias} ({context}) en 80 palabras.",
                    "images": [base64.b64encode(image.data).decode("utf-8")],
                }
            ],
            "stream": False,
        }

        def _invoke() -> str:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
                    return str(resp.json().get("message", {}).get("content", "")).strip()
            except Exception as exc:
                raise _translate_http_error(exc, "Ollama") from exc

        return self._executor.call(_invoke)


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Local embedding adapter targeting Ollama's ``/api/embed`` endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        executor: ResilientExecutor,
        dimension: int = 768,
        timeout: float = 45.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._executor = executor
        self._dimension = dimension
        self._timeout = max(1.0, min(timeout, 120.0))

    @property
    def model_name(self) -> str:
        return f"ollama/{self._model}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        def _invoke() -> list[list[float]]:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/api/embed",
                        json={"model": self._model, "input": list(texts)},
                    )
                    resp.raise_for_status()
                    embeddings = resp.json().get("embeddings", [])
                    if len(embeddings) != len(texts) or any(len(v) != self._dimension for v in embeddings):
                        raise ProviderError("Ollama devolvió embeddings incompletos o de dimensión incompatible.")
                    return [[float(x) for x in vec] for vec in embeddings]
            except Exception as exc:
                raise _translate_http_error(exc, "Ollama") from exc

        return self._executor.call(_invoke)

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        if not vectors:
            raise ProviderError("Ollama returned no embeddings")
        return vectors[0]
