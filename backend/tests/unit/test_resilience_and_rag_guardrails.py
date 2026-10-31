"""Unit tests for Resilience (CircuitBreaker + Exponential Backoff) and RAG Guardrails with Mocks."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.application.services.hybrid_ranker import HybridRanker
from app.application.services.prompt_builder import DefensivePromptBuilder
from app.application.use_cases.query_rag import QueryRAGUseCase
from app.domain.entities import Chunk, EvidenceReference, GeneratedAnswer, GeneratedClaim
from app.domain.exceptions import (
    ProviderUnavailableError,
    RateLimitError,
    TransientProviderError,
)
from app.infrastructure.resilience import CircuitBreaker, CircuitState, ResilientExecutor
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.vector_store.sparse_bm25 import BM25SparseEncoder
from tests.conftest import MockEmbeddingProvider, MockLLMProvider, MockVectorStore


def test_circuit_breaker_and_retry_with_exponential_backoff() -> None:
    fake_time = [100.0]
    sleeps: list[float] = []

    breaker = CircuitBreaker(
        name="test_llm",
        failure_threshold=2,
        recovery_timeout=15.0,
        clock=lambda: fake_time[0],
    )
    executor = ResilientExecutor(
        breaker=breaker,
        max_attempts=3,
        base_delay=0.5,
        max_delay=4.0,
        sleeper=lambda s: sleeps.append(s),
    )

    # Case 1: Transient RateLimitError twice, then succeeds on 3rd attempt
    attempts = 0

    def flaky_call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitError("HTTP 429 rate limit")
        return "ok"

    assert executor.call(flaky_call) == "ok"
    assert attempts == 3
    assert len(sleeps) == 2
    assert breaker.state == CircuitState.CLOSED

    # Case 2: Persistent failure trips circuit breaker to OPEN after 2 exhausted calls
    def always_down() -> str:
        raise TransientProviderError("Connection reset")

    with pytest.raises(TransientProviderError):
        executor.call(always_down)
    assert breaker.state == CircuitState.CLOSED  # 1st failed call

    with pytest.raises(TransientProviderError):
        executor.call(always_down)
    assert breaker.state == CircuitState.OPEN  # 2nd failed call -> trips OPEN

    # While OPEN, calls fail fast with ProviderUnavailableError without invoking fn
    called_while_open = False

    def should_not_run() -> str:
        nonlocal called_while_open
        called_while_open = True
        return "nope"

    with pytest.raises(ProviderUnavailableError):
        executor.call(should_not_run)
    assert not called_while_open

    # Advance fake clock past recovery_timeout -> transitions to HALF_OPEN -> success closes it
    fake_time[0] += 20.0
    assert breaker.state == CircuitState.HALF_OPEN
    assert executor.call(lambda: "recovered") == "recovered"
    assert breaker.state == CircuitState.CLOSED


def test_query_rag_rejects_hallucinated_citations(
    tmp_path: Path,
    sample_chunk_with_visual: tuple[Chunk, object],
) -> None:
    chunk, _asset = sample_chunk_with_visual
    storage = LocalFileStorage(tmp_path)
    # Write a dummy PNG for the visual asset
    img_path = storage.asset_path(chunk.document_id, "fig_p2_1.png")
    img_path.parent.mkdir(parents=True, exist_ok=True)
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    mock_embedder = MockEmbeddingProvider(dim=8)
    mock_store = MockVectorStore()
    mock_store.chunks = [chunk]

    # LLM cites valid E1 plus hallucinated E99 and valid F1 plus hallucinated F99
    mock_llm = MockLLMProvider(
        scripted_answer=GeneratedAnswer(
            answer="El sensor RTD ST-104 dispara a **85 °C** [E1] [E99].",
            abstained=False,
            cited_ids=["E1", "E99"],
            used_visual_ids=["F1", "F99"],
            evidence_coverage="full",
            abstention_reason="",
        )
    )

    use_case = QueryRAGUseCase(
        embedder=mock_embedder,
        sparse_encoder=BM25SparseEncoder(),
        vector_store=mock_store,
        ranker=HybridRanker(),
        prompt_builder=DefensivePromptBuilder(),
        llm=mock_llm,
        storage=storage,
    )

    result = use_case.execute("¿A qué temperatura dispara el sensor ST-104?")

    assert result.abstained is True
    assert result.citations == []
    assert result.primary_visual is None
    assert "[E99]" not in result.answer


def test_query_rag_defensive_abstention_when_llm_abstains(
    tmp_path: Path,
    sample_chunk_with_visual: tuple[Chunk, object],
) -> None:
    chunk, _ = sample_chunk_with_visual
    storage = LocalFileStorage(tmp_path)
    mock_store = MockVectorStore()
    mock_store.chunks = [chunk]

    mock_llm = MockLLMProvider(
        scripted_answer=GeneratedAnswer(
            answer="No dispongo de información suficiente en los manuales sobre reactores nucleares.",
            abstained=True,
            cited_ids=[],
            used_visual_ids=[],
            evidence_coverage="none",
            abstention_reason="El manual solo cubre la turbina hidráulica HX-900.",
        )
    )

    use_case = QueryRAGUseCase(
        embedder=MockEmbeddingProvider(dim=8),
        sparse_encoder=BM25SparseEncoder(),
        vector_store=mock_store,
        ranker=HybridRanker(),
        prompt_builder=DefensivePromptBuilder(),
        llm=mock_llm,
        storage=storage,
    )

    result = use_case.execute("¿Cómo calibrar un reactor de fusión?")
    assert result.abstained is True
    assert result.citations == []
    assert result.primary_visual is None
    assert result.abstention_reason
    assert "reactores nucleares" not in result.answer
