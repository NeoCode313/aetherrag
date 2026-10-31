"""Resilience primitives: Circuit Breaker + Retry with Exponential Backoff and Jitter.

Designed for external LLM and Vector DB calls:
* Retries only transient failures (``TransientProviderError``, ``RateLimitError``) using
  exponential backoff with full jitter:
      sleep = min(max_delay, base_delay * 2**attempt) * uniform(0.5, 1.0)
* Tracks consecutive failures in a state machine (``CLOSED -> OPEN -> HALF_OPEN``).
  When the circuit is ``OPEN``, calls fail immediately with ``ProviderUnavailableError``
  instead of blocking worker threads on a dead upstream.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from app.domain.exceptions import (
    ProviderUnavailableError,
    RateLimitError,
    TransientProviderError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Thread-safe Circuit Breaker state machine."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_recovery_unlocked()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def _check_recovery_unlocked(self) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = self._clock() - self._opened_at
            if elapsed >= self.recovery_timeout:
                logger.info("CircuitBreaker[%s] transitioning OPEN -> HALF_OPEN", self.name)
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False

    def before_call(self) -> None:
        with self._lock:
            self._check_recovery_unlocked()
            if self._state == CircuitState.OPEN:
                remaining = max(0.0, self.recovery_timeout - (self._clock() - self._opened_at))
                raise ProviderUnavailableError(
                    f"Circuit breaker '{self.name}' abierto por fallos consecutivos. "
                    f"Reintento permitido en {remaining:.1f}s."
                )

            if self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise ProviderUnavailableError("Ya existe una llamada de recuperación en curso")
                self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            if self._state != CircuitState.CLOSED or self._failures > 0:
                logger.info("CircuitBreaker[%s] reset to CLOSED", self.name)
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._probe_in_flight = False

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                logger.warning(
                    "CircuitBreaker[%s] tripped to OPEN after %d failure(s): %s",
                    self.name,
                    self._failures,
                    exc,
                )


class ResilientExecutor:
    """Combines retry with exponential backoff + jitter and a CircuitBreaker."""

    def __init__(
        self,
        breaker: CircuitBreaker,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 5.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.breaker = breaker
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleeper

    def call(self, fn: Callable[[], T]) -> T:
        self.breaker.before_call()
        last_exc: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                result = fn()
                self.breaker.record_success()
                return result
            except (TransientProviderError, RateLimitError) as exc:
                last_exc = exc
                if attempt >= self.max_attempts:
                    self.breaker.record_failure(exc)
                    raise
                raw_delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
                if isinstance(exc, RateLimitError):
                    raw_delay = min(self.max_delay, raw_delay * 1.5)
                jittered = raw_delay * random.uniform(0.6, 1.0)
                logger.warning(
                    "Transient error in %s (attempt %d/%d): %s — retrying in %.2fs",
                    self.breaker.name,
                    attempt,
                    self.max_attempts,
                    exc,
                    jittered,
                )
                self._sleep(jittered)
            except Exception as exc:
                self.breaker.record_failure(exc)
                raise

        assert last_exc is not None  # pragma: no cover
        raise last_exc
