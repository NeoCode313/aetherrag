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
