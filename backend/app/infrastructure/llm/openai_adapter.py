"""OpenAI-compatible LLM and Embedding adapters (OpenAI, vLLM, Groq, Azure)."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.entities import GeneratedAnswer, ImageInput
from app.domain.ports import EmbeddingProvider, LLMProvider
from app.domain.exceptions import ProviderError, ProviderContractError
from app.infrastructure.llm.ollama_adapter import _translate_http_error
from app.infrastructure.llm.schemas import RAG_ANSWER_JSON_SCHEMA, parse_structured_answer, response_schema
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
        response_format: str = "auto",
    ) -> None:
        self._api_key = api_key.strip().strip("'\"")
        self._model = model.strip().strip("'\"")
        self._base_url = base_url.strip().strip("'\"").rstrip("/")
        self._executor = executor
        self._timeout = max(1.0, min(timeout, 120.0))
        self._supports_images = supports_images
        self._response_format = response_format

    @property
    def _is_groq(self) -> bool:
        return urlsplit(self._base_url).hostname == "api.groq.com"

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
        system_instruction += (
            "\nPara referencias textuales E copia quote literalmente del campo text y usa "
            "solo evidence_id y quote. No calcules offsets ni completes citas de memoria. "
            "Las referencias F solo pueden apuntar a imágenes adjuntas y usan quote=null.")
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

        strict_groq = self._model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"}
        schema = response_schema(prompt, images)
        json_mode = self._response_format == "json_object" or (self._response_format == "auto" and self._is_groq and not strict_groq)
        if json_mode:
            resp_fmt: dict[str, Any] = {"type": "json_object"}
            system_instruction = (
                system_instruction
                + "\n\nDevuelve exclusivamente JSON que cumpla este esquema. No agregues explicaciones ni bloques de razonamiento. "
                + "Cuando falte evidencia, usa claims=[] y explica el motivo en missing_facets; nunca inventes citas.\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        else:
            resp_fmt = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rag_answer",
                    "strict": True,
                    "schema": schema,
                },
            }

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": resp_fmt,
        }
        # Bound reserved output quota as well as response size; provider defaults can
        # reserve a full context window and reject otherwise small RAG requests.
        limit_name = "max_completion_tokens" if urlsplit(self._base_url).hostname in {"api.groq.com", "api.openai.com"} else "max_tokens"
        payload[limit_name] = 1600
        if self._is_groq:
            payload["temperature"] = 0
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        provider_label = "Groq" if self._is_groq else "OpenAI"

        def _invoke() -> GeneratedAnswer:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    request_payload = {**payload, "messages": [dict(message) for message in payload["messages"]]}
                    # JSON-object mode does not enforce the schema. Allow one bounded
                    # regeneration, preserving the original evidence and strict validator.
                    attempts = 2 if json_mode else 1
                    for attempt in range(attempts):
                        resp = client.post(f"{self._base_url}/chat/completions", json=request_payload, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                        choice = data["choices"][0]
                        if choice.get("finish_reason") == "length":
                            raise ProviderContractError("truncated")
                        usage = data.get("usage", {})
                        try:
                            return parse_structured_answer(choice["message"].get("content"),
                                input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                                generated_mode="grounded_answer")
                        except ProviderContractError as error:
                            if attempt + 1 >= attempts:
                                raise
                            request_payload["messages"][0]["content"] += (
                                "\nEl intento anterior incumplió el esquema. Regenera el objeto completo y respeta tipos, "
                                "campos y referencias. Diagnóstico: " + str(error))
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
