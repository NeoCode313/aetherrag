"""Domain-level exception hierarchy.

Adapters translate vendor-specific errors (HTTP 429, timeouts, SDK errors) into these
types so the application layer and the resilience policies never depend on a vendor SDK.
"""


class DomainError(Exception):
    """Base class for every error raised intentionally by the system."""


class ValidationError(DomainError):
    """Input rejected by a business rule (e.g. not a PDF, file too large)."""


class NotFoundError(DomainError):
    """Requested aggregate (document, job, asset) does not exist."""


class ProviderError(DomainError):
    """An external AI provider failed in a non-recoverable way (bad request, auth)."""


class ProviderContractError(ProviderError):
    """Untrusted model output failed validation; diagnostic contains no input data."""

    def __init__(self, reason: str = "schema", fields: tuple[str, ...] = ()):
        self.reason, self.fields = reason, fields
        detail = {"invalid_json": "JSON inválido o vacío", "truncated": "respuesta truncada por el límite de salida",
                  "schema": "campos ausentes, adicionales o con tipos incorrectos"}[reason]
        suffix = "; revisar " + ", ".join(fields) if fields else ""
        super().__init__("El proveedor devolvió una respuesta que incumple el contrato estructurado: " + detail + suffix + ".")


class TransientProviderError(ProviderError):
    """Recoverable failure (network glitch, timeout, HTTP 5xx). Safe to retry."""


class RateLimitError(TransientProviderError):
    """Provider quota / rate limit exceeded (HTTP 429). Retry with backoff."""


class ProviderUnavailableError(ProviderError):
    """Circuit breaker is open: fail fast instead of hammering a dependency that is down."""


class IngestionError(DomainError):
    """The ingestion pipeline could not process a document."""


class ConflictError(DomainError):
    """Operation conflicts with an active version or existing identity."""


class DependencyUnavailableError(DomainError):
    """A required durable backend is unavailable."""


class CapacityError(DomainError):
    """Admission limit reached before a job was accepted."""
