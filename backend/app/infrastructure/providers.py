"""Explicit, secret-free provider profiles and reproducible embedding identities."""
from __future__ import annotations

import hashlib
import json
import ipaddress
from urllib.parse import urlsplit

from app.domain.exceptions import ValidationError
from app.infrastructure.llm.gemini_adapter import GeminiEmbeddingProvider, GeminiLLMProvider
from app.infrastructure.llm.local_fallback import GroundedExtractiveLLMProvider, LocalSemanticEmbeddingProvider
from app.infrastructure.llm.ollama_adapter import OllamaEmbeddingProvider, OllamaLLMProvider
from app.infrastructure.llm.anthropic_adapter import AnthropicLLMProvider
from app.infrastructure.llm.openai_adapter import OpenAIEmbeddingProvider, OpenAILLMProvider


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_endpoint(value: str, allowed_hosts: list[str]) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.hostname
            or parsed.hostname.lower() not in {h.lower() for h in allowed_hosts}):
        raise ValidationError("URL de proveedor fuera de los destinos autorizados")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "ollama", "host.docker.internal", "vllm", "lmstudio"}:
        raise ValidationError("El proveedor remoto requiere HTTPS")
    return value.rstrip("/")


def approved_hosts(endpoint, existing, approve=False):
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if host in existing:
        return existing
    if not approve or parsed.scheme != "https" or host in {"metadata.google.internal"}:
        raise ValidationError("Autoriza explícitamente el destino o configura PROVIDER_ALLOWED_HOSTS")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValidationError("No se permiten destinos privados arbitrarios ni metadata cloud")
    if not host or "." not in host:
        raise ValidationError("Destino no válido")
    return [*existing, host]


def embedding_profile(settings) -> dict:
    provider = settings.embedding_provider
    if provider == "auto" and settings.environment == "test":
        provider = "local"
    if provider == "auto":
        provider = ("gemini" if settings.gemini_api_key else
                    "openai" if settings.openai_api_key and "api.openai.com" in settings.openai_base_url
                    else "local")
    models = {"gemini": settings.gemini_embedding_model, "openai": settings.openai_embedding_model,
              "ollama": settings.ollama_embedding_model, "local": "local-semantic-hash-v2"}
    base = settings.ollama_base_url if provider == "ollama" else settings.openai_base_url if provider == "openai" else ""
    if base:
        base = validate_endpoint(base, settings.provider_allowed_hosts)
    result = {"provider": provider, "model": models[provider], "dimension": settings.embedding_dimension,
              "base_url": base, "normalization": "l2", "pipeline_version": settings.pipeline_version}
    result["pipeline_config"] = {name: getattr(settings, name) for name in (
        "chunk_max_tokens", "chunk_overlap_ratio", "spatial_max_distance", "ocr_enabled", "ocr_language")}
    result["profile_id"] = "emb_" + digest(result)[:24]
    return result


def make_embedder(profile: dict, settings, executor):
    provider = profile["provider"]
    common = {"dimension": profile["dimension"]}
    if provider == "local":
        return LocalSemanticEmbeddingProvider(**common)
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ValidationError("GEMINI_API_KEY no está configurada para este perfil")
        return GeminiEmbeddingProvider(settings.gemini_api_key, executor, model=profile["model"], **common)
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValidationError("OPENAI_API_KEY no está configurada para este perfil")
        return OpenAIEmbeddingProvider(settings.openai_api_key, profile["model"], executor,
                                       base_url=validate_endpoint(profile["base_url"], settings.provider_allowed_hosts),
                                       **common)
    if provider == "ollama":
        return OllamaEmbeddingProvider(validate_endpoint(profile["base_url"], settings.provider_allowed_hosts),
                                       profile["model"], executor, **common)
    raise ValidationError("Proveedor de embeddings no soportado")


def make_llm(settings, executor, *, provider=None, model=None):
    choice = provider or settings.llm_provider
    if choice == "auto":
        choice = "gemini" if settings.gemini_api_key else "openai" if settings.openai_api_key or settings.groq_api_key else "local"
    if choice == "gemini":
        if not settings.gemini_api_key:
            raise ValidationError("Configure GEMINI_API_KEY en el servidor")
        result = GeminiLLMProvider(settings.gemini_api_key, executor, model=model or settings.gemini_model, timeout=settings.provider_timeout_seconds, supports_images=settings.llm_supports_images)
    elif choice == "openai":
        base_url = validate_endpoint(settings.openai_base_url, settings.provider_allowed_hosts)
        if urlsplit(base_url).hostname == "api.groq.com":
            api_key = settings.groq_api_key or (settings.openai_api_key if settings.openai_api_key.startswith("gsk_") else "")
            if not api_key:
                raise ValidationError("Configure GROQ_API_KEY en el servidor para el endpoint de Groq")
        else:
            api_key = settings.openai_api_key
            if api_key.startswith("gsk_") or (settings.groq_api_key and not api_key and urlsplit(base_url).hostname == "api.openai.com"):
                raise ValidationError("La clave Groq requiere OPENAI_BASE_URL=https://api.groq.com/openai/v1")
        if not api_key and urlsplit(base_url).hostname not in {"localhost", "127.0.0.1", "host.docker.internal", "vllm", "lmstudio"}:
            raise ValidationError("Configure la clave del proveedor en la interfaz")
        result = OpenAILLMProvider(api_key, model or settings.openai_model, executor,
                   base_url=base_url,
                   timeout=settings.provider_timeout_seconds, supports_images=settings.llm_supports_images,
                   response_format=settings.openai_response_format)
    elif choice == "anthropic":
        if not settings.anthropic_api_key or not (model or settings.anthropic_model):
            raise ValidationError("Configura la clave y el modelo de Claude/Anthropic")
        result = AnthropicLLMProvider(settings.anthropic_api_key, model or settings.anthropic_model, executor,
            settings.provider_timeout_seconds, settings.llm_supports_images)
    elif choice == "ollama":
        result = OllamaLLMProvider(validate_endpoint(settings.ollama_base_url, settings.provider_allowed_hosts),
                                  model or settings.ollama_model, executor,
                                  timeout=settings.provider_timeout_seconds, supports_images=settings.llm_supports_images)
    elif choice == "local":
        result = GroundedExtractiveLLMProvider()
    else:
        raise ValidationError("Proveedor no soportado")
    return choice, result


class UnconfiguredLLMProvider:
    """Explicit unavailable state, allowing the owner to repair configuration through the UI."""
    supports_images = False
    supports_structured_output = False

    def __init__(self, choice, model, message):
        self.model_name = (choice or "auto") + "/" + (model or "configuration-required")
        self.message = message

    def generate_answer(self, *args, **kwargs):
        from app.domain.exceptions import ProviderError
        raise ProviderError(self.message)


def configured_llm(settings, executor, *, provider=None, model=None):
    try:
        return make_llm(settings, executor, provider=provider, model=model)
    except ValidationError as error:
        choice = provider or settings.llm_provider
        return choice, UnconfiguredLLMProvider(choice, model, str(error))

