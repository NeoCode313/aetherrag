"""OpenAI-compatible LLM and Embedding adapters (OpenAI, vLLM, Groq, Azure)."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import httpx

from app.domain.entities import GeneratedAnswer, ImageInput
from app.domain.ports import EmbeddingProvider, LLMProvider
from app.domain.exceptions import ProviderError
from app.infrastructure.llm.ollama_adapter import _translate_http_error
from app.infrastructure.llm.schemas import RAG_ANSWER_JSON_SCHEMA, parse_structured_answer
from app.infrastructure.resilience import ResilientExecutor


class OpenAILLMProvider(LLMProvider):
    """OpenAI Chat Completions adapter with structured JSON output and vision support."""

    def __init__(
        self,
        api_key: str,
        model: str,
        executor: ResilientExecutor,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 45.0,
        supports_images: bool = False,
    ) -> None:
        self._api_key = api_key.strip().strip("'\"")
        self._model = model.strip().strip("'\"")
        self._base_url = base_url.strip().strip("'\"").rstrip("/")
        self._executor = executor
        self._timeout = max(1.0, min(timeout, 120.0))
        self._supports_images = supports_images

    @property
    def _is_groq(self) -> bool:
        return "groq.com" in self._base_url.lower() or self._api_key.startswith("gsk_")

    @property
    def supports_images(self) -> bool:
        return self._supports_images

    @property
    def supports_structured_output(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        prefix = "groq" if self._is_groq else "openai"
        return f"{prefix}/{self._model}"

    def generate_answer(
        self,
        system_instruction: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
    ) -> GeneratedAnswer:
        if images and not self.supports_images:
            raise ProviderError("El perfil OpenAI-compatible no tiene visión habilitada.")
        if images:
            user_content: Any = [{"type": "text", "text": prompt}]
            for img in images:
                b64 = base64.b64encode(img.data).decode("utf-8")
                user_content.append({"type": "text", "text": f"Imagen adjunta {img.alias}"})
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{img.mime_type};base64,{b64}"},
                    }
                )
        else:
            user_content = prompt

        if self._is_groq:
            resp_fmt: dict[str, Any] = {"type": "json_object"}
            system_instruction = (
                system_instruction
                + "\n\nDebes responder EXCLUSIVAMENTE con un objeto JSON válido con estas claves exactas:\n"
                + '{"answer": "resumen", "abstained": false, "abstention_reason": "", '
                + '"evidence_coverage": "full", "cited_ids": ["E1"], "used_visual_ids": [], '
                + '"answer_mode": "grounded_answer", "missing_facets": [], '
                + '"claims": [{"statement": "afirmación", '
                + '"evidence_refs": [{"evidence_id": "E1", "quote": "subcadena exacta copiada literalmente del campo text", '
                + '"span_start": null, "span_end": null, "visual_region": null}]}]}'
            )
        else:
            resp_fmt = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rag_answer",
                    "strict": True,
                    "schema": {**RAG_ANSWER_JSON_SCHEMA, "additionalProperties": False},
                },
            }

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": resp_fmt,
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        provider_label = "Groq" if self._is_groq else "OpenAI"

        def _invoke() -> GeneratedAnswer:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    return parse_structured_answer(
                        text,
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                    )
            except Exception as exc:
                raise _translate_http_error(exc, provider_label) from exc

        return self._executor.call(_invoke)

    def describe_image(self, image: ImageInput, context: str) -> str:
        if not self.supports_images:
            raise ProviderError("El perfil OpenAI-compatible no tiene visión habilitada.")
        b64 = base64.b64encode(image.data).decode("utf-8")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Describe técnicamente la imagen {image.alias} ({context}) en 80 palabras.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image.mime_type};base64,{b64}"},
                        },
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        def _invoke() -> str:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return str(resp.json()["choices"][0]["message"]["content"]).strip()
            except Exception as exc:
                raise _translate_http_error(exc, "OpenAI") from exc

        return self._executor.call(_invoke)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI ``/v1/embeddings`` adapter."""

    def __init__(
        self,
        api_key: str,
        model: str,
        executor: ResilientExecutor,
        base_url: str = "https://api.openai.com/v1",
        dimension: int = 768,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._executor = executor
        self._dimension = dimension
        self._timeout = max(1.0, min(timeout, 120.0))

    @property
    def model_name(self) -> str:
        return f"openai/{self._model}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self._self_key()}"}

        def _invoke() -> list[list[float]]:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/embeddings",
                        json={
                            "model": self._model,
                            "input": list(texts),
                            "dimensions": self._dimension,
                        },
                        headers=headers,
                    )
                    resp.raise_for_status()
                    items = sorted(resp.json()["data"], key=lambda x: x["index"])
                    vectors = [[float(v) for v in item["embedding"]] for item in items]
                    if len(vectors) != len(texts) or any(len(v) != self._dimension for v in vectors):
                        raise ProviderError("El proveedor devolvió embeddings incompletos o de dimensión incompatible.")
                    return vectors
            except Exception as exc:
                raise _translate_http_error(exc, "OpenAI") from exc

        return self._executor.call(_invoke)

    def _self_key(self) -> str:
        return self._api_key

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
