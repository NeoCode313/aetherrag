"""Native Anthropic Messages adapter with JSON structured output and local validation."""
import base64
import json
from collections.abc import Sequence
import httpx

from app.domain.entities import ImageInput
from app.domain.exceptions import ProviderError, ProviderContractError
from app.infrastructure.llm.schemas import portable_output_schema, parse_structured_answer
from app.infrastructure.llm.ollama_adapter import _translate_http_error


class AnthropicLLMProvider:
    def __init__(self, api_key, model, executor, timeout=45, supports_images=False):
        self.key, self.model, self.executor = api_key, model, executor
        self.timeout, self.supports_images = timeout, supports_images

    @property
    def model_name(self):
        return "anthropic/" + self.model

    @property
    def supports_structured_output(self):
        return True

    def generate_answer(self, system_instruction, prompt, images: Sequence[ImageInput] = ()):
        if images and not self.supports_images:
            raise ProviderError("El perfil Anthropic no tiene visión habilitada")
        content = [{"type": "text", "text": prompt}]
        for image in images:
            content += [{"type": "text", "text": "Imagen " + image.alias},
                        {"type": "image", "source": {"type": "base64", "media_type": image.mime_type,
                         "data": base64.b64encode(image.data).decode()}}]
        payload = {"model": self.model, "max_tokens": 4096, "system": system_instruction,
                   "messages": [{"role": "user", "content": content}],
                   "output_config": {"format": {"type": "json_schema", "schema": portable_output_schema()}}}
        def invoke():
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"}, json=payload)
                    response.raise_for_status()
                    data = response.json()
                if data.get("stop_reason") == "max_tokens":
                    raise ProviderContractError("truncated")
                text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
                usage = data.get("usage", {})
                return parse_structured_answer(text, usage.get("input_tokens"), usage.get("output_tokens"))
            except Exception as error:
                raise _translate_http_error(error, "Anthropic") from error
        return self.executor.call(invoke)

    def describe_image(self, image, context):
        raise ProviderError("Use una consulta con evidencia visual explícita")
