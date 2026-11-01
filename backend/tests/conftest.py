"""Shared pytest fixtures and mocks isolating third-party LLM and Vector DB components."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from app.domain.entities import (
    BoundingBox,
    Chunk,
    ChunkKind,
    GeneratedAnswer,
    GeneratedClaim,
    EvidenceReference,
    ImageInput,
    LinkedVisual,
    LinkReason,
    SparseVector,
    VisualAsset,
    VisualKind,
)
from app.domain.ports import EmbeddingProvider, LLMProvider, VectorStore


def pytest_configure(config):
    """Keep a deployment's .env and inherited production settings out of tests.

    Preserve explicit opt-ins (CATALOG_TEST_DATABASE_URL, STACK_TEST_* and
    SEMANTIC_TEST_MODEL_DIR). Individual tests still set their own settings/env.
    This runs before collection imports app.main and the Celery composition.
    """
    import os
    from app.infrastructure.config import Settings, get_settings

    setting_names = {field.casefold() for field in Settings.model_fields}
    for name in list(os.environ):
        if name.casefold() in setting_names:
            os.environ.pop(name)
    Settings.model_config["env_file"] = None
    os.environ["AETHERRAG_ENV_FILE"] = os.devnull
    get_settings.cache_clear()


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic unit-test embedding mock (zero network calls)."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "mock-embedding-v1"

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float((i + j) % 5) / 5.0 for j in range(self._dim)] for i, _ in enumerate(texts)]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return [0.5] * self._dim


class MockLLMProvider(LLMProvider):
    """Configurable LLM mock for testing RAG grounding, citations, and abstention."""

    def __init__(self, scripted_answer: GeneratedAnswer | None = None) -> None:
        self.scripted_answer = scripted_answer or GeneratedAnswer(
            answer="La temperatura de disparo del sensor ST-104 es **85.0 °C** [E1] según la Figura 2.",
            abstained=False,
            cited_ids=["E1"],
            used_visual_ids=["F1"],
            evidence_coverage="full",
            abstention_reason="",
            claims=[GeneratedClaim(
                statement="El sensor RTD ST-104 dispara a 85 °C.",
                evidence_refs=[EvidenceReference(evidence_id="E1", quote="El sensor RTD Pt100 ST-104 activa alarma a 75 °C y disparo (Trip TRIP-TEMP-02) a 85 °C.")])],
        )
        self.prompts_received: list[str] = []
        self.images_received: list[Sequence[ImageInput]] = []

    @property
    def model_name(self) -> str:
        return "mock-llm-v1"

    @property
    def supports_images(self) -> bool:
        return True

    def generate_answer(
        self,
        system_instruction: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
    ) -> GeneratedAnswer:
        self.prompts_received.append(prompt)
        self.images_received.append(images)
        return self.scripted_answer

    def describe_image(self, image: ImageInput, context: str) -> str:
        return f"Mock diagram description for {image.alias} ({context})"


class MockVectorStore(VectorStore):
    """In-memory mock of the VectorStore port for isolated unit testing."""

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.dense_score_override: float = 0.88

    def ensure_collection(self, dimension: int) -> None:
        return None

    def upsert(
        self,
        chunks: Sequence[Chunk],
        dense: Sequence[list[float]],
        sparse: Sequence[SparseVector],
    ) -> None:
        self.chunks.extend(chunks)

    def dense_search(
        self,
        vector: list[float],
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        items = [
            c for c in self.chunks if not document_ids or c.document_id in document_ids
        ][:limit]
        return [(c, self.dense_score_override - idx * 0.05) for idx, c in enumerate(items)]

    def sparse_search(
        self,
        vector: SparseVector,
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        items = [
            c for c in self.chunks if not document_ids or c.document_id in document_ids
        ][:limit]
        return [(c, 0.75 - idx * 0.05) for idx, c in enumerate(items)]

    def delete_document(self, document_id: str) -> None:
        self.chunks = [c for c in self.chunks if c.document_id != document_id]

    def healthy(self) -> bool:
        return True


@pytest.fixture()
def sample_chunk_with_visual(tmp_path: Path) -> tuple[Chunk, VisualAsset]:
    asset = VisualAsset(
        asset_id="fig_p2_1",
        document_id="doc_test",
        page=2,
        bbox=BoundingBox(0.10, 0.20, 0.85, 0.50),
        kind=VisualKind.RASTER,
        file_name="fig_p2_1.png",
        caption="Figura 2: Sección del cojinete guía y sensor RTD ST-104.",
        figure_number=2,
        width_px=640,
        height_px=400,
    )
    chunk = Chunk(
        chunk_id="doc_test:p2:c1",
        document_id="doc_test",
        document_name="Manual_Turbina_HX900.pdf",
        page=2,
        kind=ChunkKind.TEXT,
        text="El sensor RTD Pt100 ST-104 activa alarma a 75 °C y disparo (Trip TRIP-TEMP-02) a 85 °C.",
        bbox=BoundingBox(0.10, 0.52, 0.85, 0.65),
        section_path=("2. Cojinete Guía", "2.1 Umbrales Térmicos"),
        visuals=[LinkedVisual(asset=asset, distance=0.02, reason=LinkReason.SPATIAL_PROXIMITY)],
    )
    return chunk, asset


@pytest.fixture(autouse=True)
def prohibit_accidental_external_http(monkeypatch):
    """Unit/integration tests never consume an ambient cloud API credential."""
    import httpx
    import os
    from urllib.parse import urlsplit
    original = httpx.Client.send

    def guarded(client, request, *args, **kwargs):
        allowed = {"testserver", "localhost", "127.0.0.1", "::1", "qdrant", "api"}
        stack_host = urlsplit(os.getenv("STACK_TEST_URL", "")).hostname
        if stack_host:
            allowed.add(stack_host)
        if urlsplit(str(request.url)).hostname not in allowed:
            raise AssertionError("External HTTP is prohibited in automated tests; use a mock")
        return original(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded)
